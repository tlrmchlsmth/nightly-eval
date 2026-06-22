#!/usr/bin/env bash
#
# Self-contained nightly eval orchestrator.
# Runs entirely in-cluster via CronJob — no external repo dependencies.
#
# Each config is a directory under configs/ containing fully self-contained
# K8s manifests (decode.yaml, prefill.yaml, serviceAccount.yaml) and a
# config.yaml with sweep parameters.
#
set -euo pipefail

# === Configuration ===
NAMESPACE="${NAMESPACE:-vllm}"
DEPLOY_NAME="${DEPLOY_NAME:-nightly-wide-ep}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:nightly}"
LUSTRE_PREFIX="${LUSTRE_PREFIX:-/mnt/lustre/nightly}"
NIGHTLY_DIR="${NIGHTLY_DIR:-.}"

INFPOOL_CHART="oci://registry.k8s.io/gateway-api-inference-extension/charts/inferencepool"
INFPOOL_VERSION="v1.5.0"
NYANN_BENCH_IMAGE="${NYANN_BENCH_IMAGE:-ghcr.io/neuralmagic/nyann-bench:pr-55}"
BENCH_ARCH="${BENCH_ARCH:-arm64}"

KN="kubectl -n $NAMESPACE"

RUN_DIR="$LUSTRE_PREFIX/results/$(date +%Y-%m-%d)"
mkdir -p "$RUN_DIR"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# === Data Prep: ensure benchmark datasets exist on Lustre ===
ensure_datasets() {
  local corpus_dir="$LUSTRE_PREFIX/corpus"
  local gsm8k_test="$LUSTRE_PREFIX/gsm8k_test.jsonl"
  local gsm8k_train="$LUSTRE_PREFIX/gsm8k_train.jsonl"
  local sharegpt="$corpus_dir/sharegpt.txt"

  mkdir -p "$corpus_dir"

  if [ ! -f "$gsm8k_test" ]; then
    log "Downloading GSM8K test split..."
    curl -fSL -o "$gsm8k_test" \
      "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl"
  fi

  if [ ! -f "$gsm8k_train" ]; then
    log "Downloading GSM8K train split..."
    curl -fSL -o "$gsm8k_train" \
      "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/train.jsonl"
  fi

  if [ ! -f "$sharegpt" ]; then
    log "Downloading ShareGPT corpus (this may take a minute)..."
    local sharegpt_json
    sharegpt_json=$(mktemp)
    curl -fSL -o "$sharegpt_json" \
      "https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/resolve/main/ShareGPT_V3_unfiltered_cleaned_split.json"
    jq -r '.[].conversations[]?.value // empty' "$sharegpt_json" > "$sharegpt"
    rm -f "$sharegpt_json"
  fi

  log "Datasets ready:"
  log "  $(wc -l < "$gsm8k_test") GSM8K test problems"
  log "  $(wc -l < "$gsm8k_train") GSM8K train problems"
  log "  $(wc -c < "$sharegpt" | tr -d ' ') bytes ShareGPT corpus"
}

# === Helper: Deploy serving stack for one config ===
deploy_serving() {
  local config_dir="$1"
  local config_name
  config_name=$(yq '.name' "$config_dir/config.yaml")

  log "Deploying $config_name"
  log "  Image: $VLLM_IMAGE"

  # Apply K8s manifests with image substitution (skip config.yaml)
  { cat "$config_dir"/serviceAccount.yaml; echo "---"; cat "$config_dir"/decode.yaml; echo "---"; cat "$config_dir"/prefill.yaml; } \
    | sed "s|VLLM_IMAGE_PLACEHOLDER|$VLLM_IMAGE|g" \
    | $KN apply -f -

  # Gateway + HTTPRoute
  export DEPLOY_NAME
  envsubst '${DEPLOY_NAME}' < "$NIGHTLY_DIR/serving/gateway.yaml" | $KN apply -f -

  # InferencePool via Helm
  local owner="nightly"
  export OWNER="$owner"
  envsubst '${DEPLOY_NAME} ${OWNER}' < "$NIGHTLY_DIR/serving/inferencepool-pd.values.yaml" > /tmp/nightly-infpool-values.yaml
  helm upgrade --install "${DEPLOY_NAME}-infpool" "$INFPOOL_CHART" \
    --version "$INFPOOL_VERSION" \
    -f /tmp/nightly-infpool-values.yaml \
    -n "$NAMESPACE"
  $KN delete pod -l "inferencepool=${DEPLOY_NAME}-infpool-epp" --ignore-not-found=true

  # DestinationRule for infpool-ip service (prevents envoy connection OOM)
  local infpool_ip_svc=""
  for i in $(seq 1 30); do
    infpool_ip_svc=$($KN get svc -l "istio.io/inferencepool-name=${DEPLOY_NAME}-infpool" \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) && [ -n "$infpool_ip_svc" ] && break
    log "Waiting for infpool-ip service... ($i/30)"
    sleep 2
  done
  if [ -n "$infpool_ip_svc" ]; then
    export INFPOOL_IP_SVC="$infpool_ip_svc"
    envsubst '${DEPLOY_NAME} ${INFPOOL_IP_SVC}' < "$NIGHTLY_DIR/serving/infpool-backend-dr.yaml" | $KN apply -f -
  else
    log "WARNING: infpool-ip service not found -- skipping DestinationRule"
  fi

  log "Deployed $config_name"
}

# === Helper: Find and fix pods stuck in ContainerCreating (DRA issues) ===
# Cordons the bad node, deletes the stuck pod, and lets LWS reschedule.
# Uncordons the node at teardown.
CORDONED_NODES=()

fix_stuck_pods() {
  local stuck_pods
  stuck_pods=$($KN get events --field-selector reason=FailedPrepareDynamicResources \
    -o jsonpath='{range .items[*]}{.involvedObject.name}{"\n"}{end}' 2>/dev/null \
    | sort -u)

  [ -z "$stuck_pods" ] && return 1

  for pod in $stuck_pods; do
    local node
    node=$($KN get pod "$pod" -o jsonpath='{.spec.nodeName}' 2>/dev/null) || continue
    [ -z "$node" ] && continue

    log "Pod $pod hit FailedPrepareDynamicResources on $node — cordoning node and deleting pod"
    kubectl cordon "$node" 2>/dev/null || true
    CORDONED_NODES+=("$node")
    $KN delete pod "$pod" --grace-period=0 --force 2>/dev/null || true
  done
  return 0
}

uncordon_nodes() {
  for node in "${CORDONED_NODES[@]}"; do
    log "Uncordoning $node"
    kubectl uncordon "$node" 2>/dev/null || true
  done
  CORDONED_NODES=()
}

# === Helper: Wait for serving readiness ===
wait_for_ready() {
  local config_dir="$1"
  local timeout="${2:-3600}"
  local check_interval=30
  local stuck_timeout=300
  local max_retries=3
  local elapsed=0

  log "Waiting for LWS readiness (timeout=${timeout}s)..."

  while [ $elapsed -lt $timeout ]; do
    # Check if both LWS are ready
    local decode_ready=false prefill_ready=false

    if $KN wait --for=jsonpath='{.status.conditions[?(@.type=="Available")].status}'=True \
      "lws/${DEPLOY_NAME}-decode" --timeout="1s" 2>/dev/null; then
      decode_ready=true
    fi

    if ! $KN get lws "${DEPLOY_NAME}-prefill" &>/dev/null; then
      prefill_ready=true
    elif $KN wait --for=jsonpath='{.status.conditions[?(@.type=="Available")].status}'=True \
      "lws/${DEPLOY_NAME}-prefill" --timeout="1s" 2>/dev/null; then
      prefill_ready=true
    fi

    if $decode_ready && $prefill_ready; then
      log "Serving stack ready."
      return 0
    fi

    sleep $check_interval
    elapsed=$((elapsed + check_interval))

    # Check for stuck pods and fix them
    if fix_stuck_pods $stuck_timeout; then
      log "Fixed stuck pods, waiting for LWS to reschedule..."
    fi
  done

  log "FAIL: LWS not ready after ${timeout}s"
  return 1
}

# === Helper: Wait for gateway to serve models ===
wait_for_gateway() {
  local timeout="${1:-600}"
  local check_interval=10
  local elapsed=0
  local gateway_url="http://${DEPLOY_NAME}-inference-gateway-istio.${NAMESPACE}.svc.cluster.local/v1/models"

  log "Waiting for gateway to serve models (timeout=${timeout}s)..."

  while [ $elapsed -lt $timeout ]; do
    local model_count
    model_count=$(curl -s --max-time 5 "$gateway_url" 2>/dev/null \
      | jq '.data | length' 2>/dev/null) || model_count=0

    if [ "$model_count" -gt 0 ] 2>/dev/null; then
      local model_id
      model_id=$(curl -s --max-time 5 "$gateway_url" 2>/dev/null \
        | jq -r '.data[0].id' 2>/dev/null) || model_id="unknown"
      log "Gateway ready: $model_count model(s) available ($model_id)"
      return 0
    fi

    sleep $check_interval
    elapsed=$((elapsed + check_interval))
  done

  log "FAIL: Gateway not serving models after ${timeout}s"
  return 1
}

# === Helper: Run a nyann-bench Job via the CLI ===
run_bench() {
  local job_name="$1" target="$2" config_json="$3" n_workers="$4"

  log "Deploying benchmark: $job_name (workers=$n_workers)"
  nyann-bench generate \
    --json \
    --kube \
    --kube.name "$job_name" \
    --kube.namespace "$NAMESPACE" \
    --kube.image "$NYANN_BENCH_IMAGE" \
    --kube.arch "$BENCH_ARCH" \
    --kube.volume lustre \
    --workers "$n_workers" \
    --target "$target" \
    --config "$config_json"
}

# === Helper: Collect JSON results from a completed Job ===
collect_results() {
  local job_name="$1"
  local pods
  pods=$($KN get pods -l "app=$job_name" -o jsonpath='{.items[*].metadata.name}')
  for pod in $pods; do
    $KN logs "$pod" -c nyann-bench 2>/dev/null | jq -c '.' 2>/dev/null
  done
}

# === Helper: Clean up benchmark resources ===
cleanup_bench() {
  local job_name="$1"
  $KN delete job "$job_name" --ignore-not-found=true
  $KN delete service "$job_name" --ignore-not-found=true
}

# === Helper: Teardown serving stack ===
teardown_serving() {
  log "Tearing down serving stack..."
  $KN delete lws "${DEPLOY_NAME}-decode" --ignore-not-found=true --grace-period=0 --force &
  $KN delete lws "${DEPLOY_NAME}-prefill" --ignore-not-found=true --grace-period=0 --force &
  helm uninstall "${DEPLOY_NAME}-infpool" -n "$NAMESPACE" 2>/dev/null || true &
  export DEPLOY_NAME
  envsubst '${DEPLOY_NAME}' < "$NIGHTLY_DIR/serving/gateway.yaml" | $KN delete -f - --ignore-not-found=true &
  $KN delete destinationrule "${DEPLOY_NAME}-infpool-backend" --ignore-not-found=true &
  wait
  uncordon_nodes
  log "Teardown complete."
}

# === Helper: Run staircase benchmark ===
run_staircase() {
  local config_dir="$1" run_dir="$2"
  local config_name
  config_name=$(yq '.name' "$config_dir/config.yaml")
  local base_url="http://${DEPLOY_NAME}-inference-gateway-istio.${NAMESPACE}.svc.cluster.local/v1"

  local sweep_min sweep_max sweep_steps step_duration
  sweep_min=$(yq '.sweep.min // 128' "$config_dir/config.yaml")
  sweep_max=$(yq '.sweep.max // 2048' "$config_dir/config.yaml")
  sweep_steps=$(yq '.sweep.steps // 5' "$config_dir/config.yaml")
  step_duration=$(yq '.sweep.step_duration // "45s"' "$config_dir/config.yaml")

  local bench_config
  bench_config="{\"warmup\":{\"duration\":\"15s\",\"stagger\":true},\"sweep\":{\"min\":$sweep_min,\"max\":$sweep_max,\"steps\":$sweep_steps,\"step_duration\":\"$step_duration\"},\"workload\":{\"type\":\"corpus\",\"isl\":500,\"osl\":1500,\"turns\":1,\"corpus_path\":\"$LUSTRE_PREFIX/corpus/sharegpt.txt\"}}"

  local job_name="nightly-${config_name}-staircase"
  local output_dir="$run_dir/$config_name/staircase"
  mkdir -p "$output_dir"

  log "Staircase: $job_name (c=$sweep_min-$sweep_max, $sweep_steps steps)"
  run_bench "$job_name" "$base_url" "$bench_config" 8
  $KN wait --for=condition=Complete "job/$job_name" --timeout=1800s
  collect_results "$job_name" > "$output_dir/summary.json" 2>/dev/null || true
  cleanup_bench "$job_name"
}

# === Helper: Run GSM8K eval ===
run_gsm8k() {
  local name="$1" run_dir="$2"
  local base_url
  base_url="http://${DEPLOY_NAME}-inference-gateway-istio.${NAMESPACE}.svc.cluster.local/v1"

  local gsm8k_config
  gsm8k_config=$(cat "$NIGHTLY_DIR/benchmarks/gsm8k-eval.json")

  local job_name="nightly-${name}"
  local output_dir="$run_dir/$name"
  mkdir -p "$output_dir"

  log "GSM8K eval: $job_name"
  run_bench "$job_name" "$base_url" "$gsm8k_config" 1
  $KN wait --for=condition=Complete "job/$job_name" --timeout=300s
  collect_results "$job_name" > "$output_dir/summary.json" 2>/dev/null || true
  cleanup_bench "$job_name"
}

# ===========================================================================
# Main loop
# ===========================================================================

log "=== Nightly eval starting ==="
log "  vLLM image: $VLLM_IMAGE"
log "  Results:    $RUN_DIR"

ensure_datasets

echo "{\"vllm_image\": \"$VLLM_IMAGE\", \"start\": \"$(date -Iseconds)\", \"configs\": []}" \
  > "$RUN_DIR/metadata.json"

FAILED=0
for config_dir in "$NIGHTLY_DIR"/configs/*/; do
  [ ! -f "$config_dir/config.yaml" ] && continue

  config_name=$(yq '.name' "$config_dir/config.yaml")
  config_start=$(date -Iseconds)

  log ""
  log "=========================================="
  log "  Config: $config_name"
  log "=========================================="

  # Deploy
  if ! deploy_serving "$config_dir"; then
    log "FAIL: deploy $config_name"
    FAILED=$((FAILED + 1))
    teardown_serving || true
    continue
  fi

  if ! wait_for_ready "$config_dir"; then
    log "FAIL: readiness $config_name (timeout)"
    FAILED=$((FAILED + 1))
    teardown_serving || true
    continue
  fi

  if ! wait_for_gateway; then
    log "FAIL: gateway not serving $config_name"
    FAILED=$((FAILED + 1))
    teardown_serving || true
    continue
  fi

  # GSM8K pre-eval
  run_gsm8k "${config_name}-gsm8k-pre" "$RUN_DIR" \
    || log "WARN: gsm8k-pre failed for $config_name"

  # Staircase
  run_staircase "$config_dir" "$RUN_DIR" \
    || log "WARN: staircase failed for $config_name"

  # GSM8K post-eval
  run_gsm8k "${config_name}-gsm8k-post" "$RUN_DIR" \
    || log "WARN: gsm8k-post failed for $config_name"

  # Record config result
  # Count GPUs from LWS manifests
  decode_gpus=$(yq '.spec.leaderWorkerTemplate.size * 4' "$config_dir/decode.yaml")
  prefill_gpus=0
  if [ -f "$config_dir/prefill.yaml" ]; then
    prefill_gpus=$(yq '.spec.replicas * .spec.leaderWorkerTemplate.size * 4' "$config_dir/prefill.yaml")
  fi
  num_gpus=$((decode_gpus + prefill_gpus))

  config_end=$(date -Iseconds)
  jq --arg name "$config_name" --argjson gpus "$num_gpus" \
     --arg start "$config_start" --arg end "$config_end" \
     '.configs += [{name: $name, num_gpus: $gpus, start: $start, end: $end}]' \
     "$RUN_DIR/metadata.json" > "$RUN_DIR/metadata.tmp" && mv "$RUN_DIR/metadata.tmp" "$RUN_DIR/metadata.json"

  # Teardown
  teardown_serving || true
  log "Waiting 30s for GPU release..."
  sleep 30
done

# Finalize
jq --arg end "$(date -Iseconds)" --argjson failed "$FAILED" \
   '. + {end: $end, failed: $failed}' \
   "$RUN_DIR/metadata.json" > "$RUN_DIR/metadata.tmp" && mv "$RUN_DIR/metadata.tmp" "$RUN_DIR/metadata.json"

log ""
log "=== Nightly eval complete ==="
log "  Configs failed: $FAILED"
log "  Results: $RUN_DIR"

# Post-processing
if [ -f "$NIGHTLY_DIR/postprocess/pareto.py" ]; then
  log "Running post-processing..."
  python3 "$NIGHTLY_DIR/postprocess/pareto.py" "$RUN_DIR" || log "WARN: pareto.py failed"
  python3 "$NIGHTLY_DIR/postprocess/regression.py" "$RUN_DIR" || log "WARN: regression.py failed"
fi

# Dashboard
if [ -f "$NIGHTLY_DIR/postprocess/dashboard.py" ]; then
  log "Generating dashboard..."
  python3 "$NIGHTLY_DIR/postprocess/dashboard.py" "$RUN_DIR" || log "WARN: dashboard.py failed"
fi

if [ -n "${GITHUB_TOKEN:-}" ] && [ -f "$RUN_DIR/dashboard.html" ]; then
  log "Publishing dashboard..."
  bash "$NIGHTLY_DIR/postprocess/publish-dashboard.sh" "$RUN_DIR" || log "WARN: dashboard publish failed"
else
  log "Skipping dashboard publish (no GITHUB_TOKEN or no dashboard.html)"
fi

exit $FAILED
