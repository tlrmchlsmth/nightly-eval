#!/usr/bin/env bash
#
# Self-contained nightly eval orchestrator.
# Runs entirely in-cluster via CronJob — no client, no Justfile dependency.
#
# Loops over config YAMLs, deploying each serving config, running benchmarks,
# and collecting results to Lustre.
#
set -euo pipefail

# === Configuration ===
NAMESPACE="${NAMESPACE:-vllm}"
DEPLOY_NAME="${DEPLOY_NAME:-nightly-wide-ep}"
OWNER="${OWNER:-nightly}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:nightly}"
LUSTRE_PREFIX="${LUSTRE_PREFIX:-/mnt/lustre/nightly}"
NIGHTLY_DIR="${NIGHTLY_DIR:-.}"
NYANN_BENCH_DIR="${NYANN_BENCH_DIR:-../nyann-bench}"
LLMD_DIR="${LLMD_DIR:-../j-llm-d}"

INFPOOL_CHART="oci://registry.k8s.io/gateway-api-inference-extension/charts/inferencepool"
INFPOOL_VERSION="v1.3.0"
NYANN_BENCH_IMAGE="${NYANN_BENCH_IMAGE:-ghcr.io/neuralmagic/nyann-bench:latest}"
BENCH_ARCH="${BENCH_ARCH:-arm64}"

KN="kubectl -n $NAMESPACE"

RUN_DIR="$LUSTRE_PREFIX/results/$(date +%Y-%m-%d)"
mkdir -p "$RUN_DIR"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# === Helper: Deploy serving stack for one config ===
deploy_serving() {
  local config_file="$1"
  local config_name mode deploy_ts rendered
  config_name=$(yq '.name' "$config_file")
  mode=$(yq '.mode // "pd"' "$config_file")
  deploy_ts=$(date +%Y%m%d-%H%M%S)

  local decode_lws_size decode_tp_size decode_max_tokens decode_extra_args
  decode_lws_size=$(yq '.decode.lws_size' "$config_file")
  decode_tp_size=$(yq '.decode.tp_size' "$config_file")
  decode_max_tokens=$(yq '.decode.max_tokens' "$config_file")
  decode_extra_args=$(yq '.decode.extra_args // ""' "$config_file")

  log "Deploying $config_name (mode=$mode)"
  log "  Decode: lws_size=$decode_lws_size tp=$decode_tp_size max_tokens=$decode_max_tokens"
  log "  Image: $VLLM_IMAGE"

  # Render kustomize for selected mode
  rendered=$(mktemp /tmp/nightly-serving-XXXXXX.yaml)
  kubectl kustomize --load-restrictor=LoadRestrictionsNone "$NIGHTLY_DIR/serving/$mode" \
    | sed -e "s/DEPLOY_TS_PLACEHOLDER/$deploy_ts/g" \
          -e "s/OWNER_PLACEHOLDER/$OWNER/g" \
          -e "s|VLLM_DEV_VENV_PLACEHOLDER||g" \
          -e "s|LUSTRE_PREFIX_PLACEHOLDER|$LUSTRE_PREFIX|g" \
          -e "s|VLLM_IMAGE_PLACEHOLDER|$VLLM_IMAGE|g" \
          -e "s|FORK_REPO_PLACEHOLDER||g" \
          -e "s|FORK_BRANCH_PLACEHOLDER||g" \
    > "$rendered"

  # Decode LWS overrides
  yq -i "(select(.kind == \"LeaderWorkerSet\" and (.metadata.name | test(\"decode\"))) | .spec.leaderWorkerTemplate.size) = $decode_lws_size" "$rendered"
  yq -i "(select(.kind == \"LeaderWorkerSet\" and (.metadata.name | test(\"decode\"))) | .spec.leaderWorkerTemplate.workerTemplate.spec.containers[0].env[] | select(.name == \"TP_SIZE\") | .value) = \"$decode_tp_size\"" "$rendered"
  yq -i "(select(.kind == \"LeaderWorkerSet\" and (.metadata.name | test(\"decode\"))) | .spec.leaderWorkerTemplate.workerTemplate.spec.containers[0].env[] | select(.name == \"MAX_TOKENS\") | .value) = \"$decode_max_tokens\"" "$rendered"

  # Prefill LWS overrides (pd mode only)
  if [ "$mode" = "pd" ]; then
    local prefill_replicas prefill_tp_size
    prefill_replicas=$(yq '.prefill.replicas' "$config_file")
    prefill_tp_size=$(yq '.prefill.tp_size // 1' "$config_file")
    log "  Prefill: replicas=$prefill_replicas tp=$prefill_tp_size"
    yq -i "(select(.kind == \"LeaderWorkerSet\" and (.metadata.name | test(\"prefill\"))) | .spec.replicas) = $prefill_replicas" "$rendered"
    yq -i "(select(.kind == \"LeaderWorkerSet\" and (.metadata.name | test(\"prefill\"))) | .spec.leaderWorkerTemplate.workerTemplate.spec.containers[0].env[] | select(.name == \"TP_SIZE\") | .value) = \"$prefill_tp_size\"" "$rendered"
  fi

  # Extra vLLM args
  if [ -n "$decode_extra_args" ]; then
    log "  Extra args: $decode_extra_args"
    yq -i "(select(.kind == \"LeaderWorkerSet\" and (.metadata.name | test(\"decode\"))) | .spec.leaderWorkerTemplate.workerTemplate.spec.containers[0].args[0]) |= sub(\"vllm serve\"; \"vllm serve $decode_extra_args\")" "$rendered"
  fi

  $KN apply -f "$rendered"
  rm -f "$rendered"

  # Gateway + HTTPRoute
  export DEPLOY_NAME
  envsubst '${DEPLOY_NAME}' < "$NIGHTLY_DIR/serving/gateway.yaml" | $KN apply -f -

  # InferencePool via Helm
  export OWNER
  envsubst '${DEPLOY_NAME} ${OWNER}' < "$NIGHTLY_DIR/serving/inferencepool-${mode}.values.yaml" > /tmp/nightly-infpool-values.yaml
  helm upgrade --install "${DEPLOY_NAME}-infpool" "$INFPOOL_CHART" \
    --version "$INFPOOL_VERSION" \
    -f /tmp/nightly-infpool-values.yaml \
    -n "$NAMESPACE"
  $KN delete pod -l "inferencepool=${DEPLOY_NAME}-infpool-epp" --ignore-not-found=true

  log "Deployed $config_name"
}

# === Helper: Wait for serving readiness ===
wait_for_ready() {
  local config_file="$1"
  local mode timeout
  mode=$(yq '.mode // "pd"' "$config_file")
  timeout="${2:-1800}"

  log "Waiting for decode LWS readiness (timeout=${timeout}s)..."
  $KN wait --for=jsonpath='{.status.conditions[?(@.type=="Available")].status}'=True \
    "lws/${DEPLOY_NAME}-decode" --timeout="${timeout}s"

  if [ "$mode" = "pd" ]; then
    log "Waiting for prefill LWS readiness..."
    $KN wait --for=jsonpath='{.status.conditions[?(@.type=="Available")].status}'=True \
      "lws/${DEPLOY_NAME}-prefill" --timeout="${timeout}s"
  fi

  log "Serving stack ready."
}

# === Helper: Deploy a nyann-bench Job ===
deploy_bench_job() {
  local job_name="$1" target="$2" config_json="$3" n_workers="$4"

  # Clean up previous run
  $KN delete job "$job_name" --ignore-not-found=true
  $KN delete configmap "${job_name}-config" --ignore-not-found=true

  # Create ConfigMap with benchmark config
  $KN create configmap "${job_name}-config" --from-literal=config.json="$config_json"

  # Render and apply the Job
  export JOB_NAME="$job_name" N_WORKERS="$n_workers" TARGET="$target"
  export IMAGE_TAG="${NYANN_BENCH_IMAGE##*:}" ARCH="$BENCH_ARCH" LOG_LEVEL="info"
  kubectl kustomize "$NYANN_BENCH_DIR/deploy/overlays/lustre" | envsubst | $KN apply -f -
}

# === Helper: Collect results from a completed Job ===
collect_results() {
  local job_name="$1"
  local pods
  pods=$($KN get pods -l "app=$job_name" -o jsonpath='{.items[*].metadata.name}')
  for pod in $pods; do
    $KN logs "$pod" -c nyann-bench 2>/dev/null
  done
}

# === Helper: Teardown serving stack ===
teardown_serving() {
  log "Tearing down serving stack..."
  $KN delete lws "${DEPLOY_NAME}-decode" --ignore-not-found=true --grace-period=0 --force &
  $KN delete lws "${DEPLOY_NAME}-prefill" --ignore-not-found=true --grace-period=0 --force &
  helm uninstall "${DEPLOY_NAME}-infpool" -n "$NAMESPACE" 2>/dev/null || true &
  export DEPLOY_NAME
  envsubst '${DEPLOY_NAME}' < "$NIGHTLY_DIR/serving/gateway.yaml" | $KN delete -f - --ignore-not-found=true &
  wait
  log "Teardown complete."
}

# === Helper: Run staircase benchmark ===
run_staircase() {
  local config_file="$1" run_dir="$2"
  local config_name base_url
  config_name=$(yq '.name' "$config_file")
  base_url="http://${DEPLOY_NAME}-inference-gateway-istio.${NAMESPACE}.svc.cluster.local/v1"

  local sweep_min sweep_max sweep_steps step_duration
  sweep_min=$(yq '.sweep.min // 128' "$config_file")
  sweep_max=$(yq '.sweep.max // 2048' "$config_file")
  sweep_steps=$(yq '.sweep.steps // 5' "$config_file")
  step_duration=$(yq '.sweep.step_duration // "45s"' "$config_file")

  local bench_config
  bench_config="{\"warmup\":{\"duration\":\"15s\",\"stagger\":true},\"sweep\":{\"min\":$sweep_min,\"max\":$sweep_max,\"steps\":$sweep_steps,\"step_duration\":\"$step_duration\"},\"workload\":{\"type\":\"corpus\",\"isl\":500,\"osl\":1500,\"turns\":1,\"corpus_path\":\"$LUSTRE_PREFIX/corpus/sharegpt.txt\"}}"

  local job_name="nightly-${config_name}-staircase"
  local output_dir="$run_dir/$config_name/staircase"
  mkdir -p "$output_dir"

  log "Staircase: $job_name (c=$sweep_min→$sweep_max, $sweep_steps steps)"
  deploy_bench_job "$job_name" "$base_url" "$bench_config" 8
  $KN wait --for=condition=Complete "job/$job_name" --timeout=600s
  collect_results "$job_name" > "$output_dir/summary.json" 2>/dev/null || true
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
  deploy_bench_job "$job_name" "$base_url" "$gsm8k_config" 1
  $KN wait --for=condition=Complete "job/$job_name" --timeout=300s
  collect_results "$job_name" > "$output_dir/summary.json" 2>/dev/null || true
}

# ===========================================================================
# Main loop
# ===========================================================================

log "=== Nightly eval starting ==="
log "  vLLM image: $VLLM_IMAGE"
log "  Results:    $RUN_DIR"

echo "{\"vllm_image\": \"$VLLM_IMAGE\", \"start\": \"$(date -Iseconds)\", \"configs\": []}" \
  > "$RUN_DIR/metadata.json"

FAILED=0
for config_file in "$NIGHTLY_DIR"/configs/*.yaml; do
  config_name=$(yq '.name' "$config_file")
  mode=$(yq '.mode // "pd"' "$config_file")
  config_start=$(date -Iseconds)

  log ""
  log "=========================================="
  log "  Config: $config_name (mode=$mode)"
  log "=========================================="

  # Deploy
  if ! deploy_serving "$config_file"; then
    log "FAIL: deploy $config_name"
    FAILED=$((FAILED + 1))
    teardown_serving || true
    continue
  fi

  if ! wait_for_ready "$config_file"; then
    log "FAIL: readiness $config_name (timeout)"
    FAILED=$((FAILED + 1))
    teardown_serving || true
    continue
  fi

  # GSM8K pre-eval
  run_gsm8k "${config_name}-gsm8k-pre" "$RUN_DIR" \
    || log "WARN: gsm8k-pre failed for $config_name"

  # Staircase
  run_staircase "$config_file" "$RUN_DIR" \
    || log "WARN: staircase failed for $config_name"

  # GSM8K post-eval
  run_gsm8k "${config_name}-gsm8k-post" "$RUN_DIR" \
    || log "WARN: gsm8k-post failed for $config_name"

  # Record config result
  if [ "$mode" = "pd" ]; then
    num_gpus=$(yq '(.decode.lws_size * 4) + (.prefill.replicas * 4)' "$config_file")
  else
    num_gpus=$(yq '.decode.lws_size * 4' "$config_file")
  fi
  config_end=$(date -Iseconds)
  jq --arg name "$config_name" --arg mode "$mode" --argjson gpus "$num_gpus" \
     --arg start "$config_start" --arg end "$config_end" \
     '.configs += [{name: $name, mode: $mode, num_gpus: $gpus, start: $start, end: $end}]' \
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

exit $FAILED
