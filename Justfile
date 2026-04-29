set dotenv-load
set dotenv-required

NAMESPACE := "vllm"
KN := "kubectl -n " + NAMESPACE

DEPLOY_NAME := "nightly-wide-ep"
OWNER := "nightly"
LUSTRE_PREFIX := "/mnt/lustre/nightly"
VLLM_IMAGE := env("VLLM_IMAGE", "vllm/vllm-openai:nightly")

NYANN_BENCH_DIR := justfile_directory() / ".." / "nyann-bench"
LLMD_DIR := justfile_directory() / ".." / "j-llm-d"
SERVING_DIR := justfile_directory() / "serving"
BENCHMARKS_DIR := justfile_directory() / "benchmarks"

default:
  just --list

# === Serving Stack ===

# Deploy the serving stack for a given config
deploy CONFIG_FILE:
  #!/usr/bin/env bash
  set -euo pipefail
  export DEPLOY_NAME="{{DEPLOY_NAME}}"
  DEPLOY_TS=$(date +%Y%m%d-%H%M%S)

  CONFIG_NAME=$(yq '.name' "{{CONFIG_FILE}}")
  MODE=$(yq '.mode // "pd"' "{{CONFIG_FILE}}")
  DECODE_LWS_SIZE=$(yq '.decode.lws_size' "{{CONFIG_FILE}}")
  DECODE_TP_SIZE=$(yq '.decode.tp_size' "{{CONFIG_FILE}}")
  DECODE_MAX_TOKENS=$(yq '.decode.max_tokens' "{{CONFIG_FILE}}")
  DECODE_EXTRA_ARGS=$(yq '.decode.extra_args // ""' "{{CONFIG_FILE}}")

  echo "=== Deploying config: $CONFIG_NAME (mode=$MODE) ==="
  echo "  Decode: lws_size=$DECODE_LWS_SIZE tp=$DECODE_TP_SIZE max_tokens=$DECODE_MAX_TOKENS"
  echo "  Image: {{VLLM_IMAGE}}"

  # Render kustomize for the selected mode (pd or agg)
  RENDERED=$(mktemp /tmp/nightly-serving-XXXXXX.yaml)
  trap "rm -f $RENDERED" EXIT

  kubectl kustomize --load-restrictor=LoadRestrictionsNone "{{SERVING_DIR}}/$MODE" \
    | sed -e "s/DEPLOY_TS_PLACEHOLDER/$DEPLOY_TS/g" \
          -e "s/OWNER_PLACEHOLDER/{{OWNER}}/g" \
          -e "s|VLLM_DEV_VENV_PLACEHOLDER||g" \
          -e "s|LUSTRE_PREFIX_PLACEHOLDER|{{LUSTRE_PREFIX}}|g" \
          -e "s|VLLM_IMAGE_PLACEHOLDER|{{VLLM_IMAGE}}|g" \
          -e "s|FORK_REPO_PLACEHOLDER||g" \
          -e "s|FORK_BRANCH_PLACEHOLDER||g" \
    > "$RENDERED"

  # Decode LWS overrides
  yq -i '(select(.kind == "LeaderWorkerSet" and (.metadata.name | test("decode"))) | .spec.leaderWorkerTemplate.size) = '"$DECODE_LWS_SIZE" "$RENDERED"
  yq -i '(select(.kind == "LeaderWorkerSet" and (.metadata.name | test("decode"))) | .spec.leaderWorkerTemplate.workerTemplate.spec.containers[0].env[] | select(.name == "TP_SIZE") | .value) = "'"$DECODE_TP_SIZE"'"' "$RENDERED"
  yq -i '(select(.kind == "LeaderWorkerSet" and (.metadata.name | test("decode"))) | .spec.leaderWorkerTemplate.workerTemplate.spec.containers[0].env[] | select(.name == "MAX_TOKENS") | .value) = "'"$DECODE_MAX_TOKENS"'"' "$RENDERED"

  # Prefill LWS overrides (pd mode only)
  if [ "$MODE" = "pd" ]; then
    PREFILL_REPLICAS=$(yq '.prefill.replicas' "{{CONFIG_FILE}}")
    PREFILL_TP_SIZE=$(yq '.prefill.tp_size // 1' "{{CONFIG_FILE}}")
    echo "  Prefill: replicas=$PREFILL_REPLICAS tp=$PREFILL_TP_SIZE"
    yq -i '(select(.kind == "LeaderWorkerSet" and (.metadata.name | test("prefill"))) | .spec.replicas) = '"$PREFILL_REPLICAS" "$RENDERED"
    yq -i '(select(.kind == "LeaderWorkerSet" and (.metadata.name | test("prefill"))) | .spec.leaderWorkerTemplate.workerTemplate.spec.containers[0].env[] | select(.name == "TP_SIZE") | .value) = "'"$PREFILL_TP_SIZE"'"' "$RENDERED"
  fi

  # Extra vLLM args (e.g. MTP speculative decoding)
  if [ -n "$DECODE_EXTRA_ARGS" ]; then
    echo "  Extra args: $DECODE_EXTRA_ARGS"
    yq -i '(select(.kind == "LeaderWorkerSet" and (.metadata.name | test("decode"))) | .spec.leaderWorkerTemplate.workerTemplate.spec.containers[0].args[0]) |= sub("vllm serve"; "vllm serve '"$DECODE_EXTRA_ARGS"'")' "$RENDERED"
  fi

  {{KN}} apply -f "$RENDERED"

  # Deploy gateway + httproute
  export DEPLOY_NAME
  envsubst '${DEPLOY_NAME}' < {{SERVING_DIR}}/gateway.yaml | {{KN}} apply -f -

  # Deploy InferencePool via Helm (mode-specific values)
  export OWNER="{{OWNER}}"
  envsubst '${DEPLOY_NAME} ${OWNER}' < "{{SERVING_DIR}}/inferencepool-${MODE}.values.yaml" > /tmp/nightly-infpool-values.yaml
  helm upgrade --install {{DEPLOY_NAME}}-infpool \
    oci://registry.k8s.io/gateway-api-inference-extension/charts/inferencepool \
    --version v1.3.0 \
    -f /tmp/nightly-infpool-values.yaml \
    -n {{NAMESPACE}}
  {{KN}} delete pod -l inferencepool={{DEPLOY_NAME}}-infpool-epp --ignore-not-found=true

  # Apply DestinationRule for infpool-ip service
  INFPOOL_IP_SVC=""
  for i in $(seq 1 30); do
    INFPOOL_IP_SVC=$({{KN}} get svc -l istio.io/inferencepool-name={{DEPLOY_NAME}}-infpool \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) && [ -n "$INFPOOL_IP_SVC" ] && break
    echo "Waiting for infpool-ip service... ($i/30)"
    sleep 2
  done
  if [ -n "$INFPOOL_IP_SVC" ]; then
    export INFPOOL_IP_SVC
    envsubst '${DEPLOY_NAME} ${INFPOOL_IP_SVC}' < {{LLMD_DIR}}/gb200/infpool-backend-dr.yaml | {{KN}} apply -f -
  else
    echo "WARNING: infpool-ip service not found — skipping DestinationRule"
  fi

  echo "=== Deployed $CONFIG_NAME ($MODE) at $DEPLOY_TS ==="

# Wait for serving stack readiness
wait-ready CONFIG_FILE='' TIMEOUT='1800':
  #!/usr/bin/env bash
  set -euo pipefail
  MODE="pd"
  if [ -n "{{CONFIG_FILE}}" ]; then
    MODE=$(yq '.mode // "pd"' "{{CONFIG_FILE}}")
  fi

  echo "Waiting for decode LWS readiness..."
  {{KN}} wait --for=jsonpath='{.status.conditions[?(@.type=="Available")].status}'=True \
    lws/{{DEPLOY_NAME}}-decode --timeout={{TIMEOUT}}s

  if [ "$MODE" = "pd" ]; then
    echo "Waiting for prefill LWS readiness..."
    {{KN}} wait --for=jsonpath='{.status.conditions[?(@.type=="Available")].status}'=True \
      lws/{{DEPLOY_NAME}}-prefill --timeout={{TIMEOUT}}s
  fi

  echo "Serving stack ready."

# Tear down the serving stack
teardown:
  #!/usr/bin/env bash
  set -euo pipefail
  echo "Tearing down nightly serving stack..."
  {{KN}} delete lws {{DEPLOY_NAME}}-decode --ignore-not-found=true --grace-period=0 --force &
  {{KN}} delete lws {{DEPLOY_NAME}}-prefill --ignore-not-found=true --grace-period=0 --force &
  helm uninstall {{DEPLOY_NAME}}-infpool -n {{NAMESPACE}} 2>/dev/null || true &
  export DEPLOY_NAME="{{DEPLOY_NAME}}"
  envsubst '${DEPLOY_NAME}' < {{SERVING_DIR}}/gateway.yaml | {{KN}} delete -f - --ignore-not-found=true &
  wait
  echo "Teardown complete."

# === Benchmarks ===

# Run the staircase concurrency sweep (reads sweep params from config)
bench-staircase CONFIG_FILE RUN_DIR:
  #!/usr/bin/env bash
  set -euo pipefail
  CONFIG_NAME=$(yq '.name' "{{CONFIG_FILE}}")
  BASE_URL="http://{{DEPLOY_NAME}}-inference-gateway-istio.{{NAMESPACE}}.svc.cluster.local/v1"
  OUTPUT_DIR="{{RUN_DIR}}/$CONFIG_NAME/staircase"
  mkdir -p "$OUTPUT_DIR"

  # Build benchmark config from sweep params in config YAML
  SWEEP_MIN=$(yq '.sweep.min // 128' "{{CONFIG_FILE}}")
  SWEEP_MAX=$(yq '.sweep.max // 2048' "{{CONFIG_FILE}}")
  SWEEP_STEPS=$(yq '.sweep.steps // 5' "{{CONFIG_FILE}}")
  STEP_DURATION=$(yq '.sweep.step_duration // "45s"' "{{CONFIG_FILE}}")

  BENCH_CONFIG="{\"warmup\":{\"duration\":\"15s\",\"stagger\":true},\"sweep\":{\"min\":$SWEEP_MIN,\"max\":$SWEEP_MAX,\"steps\":$SWEEP_STEPS,\"step_duration\":\"$STEP_DURATION\"},\"workload\":{\"type\":\"corpus\",\"isl\":500,\"osl\":1500,\"turns\":1,\"corpus_path\":\"{{LUSTRE_PREFIX}}/corpus/sharegpt.txt\"}}"

  cd "{{NYANN_BENCH_DIR}}"
  just deploy "nightly-${CONFIG_NAME}-staircase" "$BASE_URL" \
    "$BENCH_CONFIG" \
    8 {{NAMESPACE}} arm64 lustre latest
  echo "Staircase job submitted: nightly-${CONFIG_NAME}-staircase (c=$SWEEP_MIN→$SWEEP_MAX, $SWEEP_STEPS steps)"
  {{KN}} wait --for=condition=Complete "job/nightly-${CONFIG_NAME}-staircase" --timeout=600s
  just collect "nightly-${CONFIG_NAME}-staircase" > "$OUTPUT_DIR/summary.json" 2>/dev/null || true

# Run GSM8K eval (accuracy validation)
bench-gsm8k NAME RUN_DIR:
  #!/usr/bin/env bash
  set -euo pipefail
  BASE_URL="http://{{DEPLOY_NAME}}-inference-gateway-istio.{{NAMESPACE}}.svc.cluster.local/v1"
  OUTPUT_DIR="{{RUN_DIR}}/{{NAME}}"
  mkdir -p "$OUTPUT_DIR"
  cd "{{NYANN_BENCH_DIR}}"
  just deploy "nightly-{{NAME}}" "$BASE_URL" \
    "{{BENCHMARKS_DIR}}/gsm8k-eval.json" \
    1 {{NAMESPACE}} arm64 lustre latest
  echo "GSM8K eval job submitted: nightly-{{NAME}}"
  {{KN}} wait --for=condition=Complete "job/nightly-{{NAME}}" --timeout=300s
  just collect "nightly-{{NAME}}" > "$OUTPUT_DIR/summary.json" 2>/dev/null || true

# Collect results from a completed nyann-bench job
collect NAME:
  cd "{{NYANN_BENCH_DIR}}" && just collect "{{NAME}}"

# Tail logs for a running nyann-bench job
logs NAME:
  {{KN}} logs -l app={{NAME}} -c nyann-bench --tail=50 -f --max-log-requests=20

# Stop benchmark jobs
stop-bench:
  {{KN}} delete jobs -l app=nightly --ignore-not-found=true

# === Full Nightly Run ===

# Run the full nightly eval loop across all configs
run-all:
  #!/usr/bin/env bash
  set -euo pipefail
  RUN_DIR="{{LUSTRE_PREFIX}}/results/$(date +%Y-%m-%d)"
  mkdir -p "$RUN_DIR"

  echo "{\"vllm_image\": \"{{VLLM_IMAGE}}\", \"start\": \"$(date -Iseconds)\", \"configs\": []}" \
    > "$RUN_DIR/metadata.json"

  FAILED=0
  for config_file in {{justfile_directory()}}/configs/*.yaml; do
    CONFIG_NAME=$(yq '.name' "$config_file")
    MODE=$(yq '.mode // "pd"' "$config_file")
    echo ""
    echo "=========================================="
    echo "  Config: $CONFIG_NAME (mode=$MODE)"
    echo "=========================================="

    CONFIG_START=$(date -Iseconds)

    # Deploy
    just deploy "$config_file" || { echo "FAIL: deploy $CONFIG_NAME"; FAILED=1; just teardown; continue; }
    just wait-ready "$config_file" || { echo "FAIL: readiness $CONFIG_NAME"; FAILED=1; just teardown; continue; }

    # GSM8K pre-eval
    just bench-gsm8k "${CONFIG_NAME}-gsm8k-pre" "$RUN_DIR" || echo "WARN: gsm8k-pre failed for $CONFIG_NAME"

    # Staircase (reads sweep params from config)
    just bench-staircase "$config_file" "$RUN_DIR" || echo "WARN: staircase failed for $CONFIG_NAME"

    # GSM8K post-eval
    just bench-gsm8k "${CONFIG_NAME}-gsm8k-post" "$RUN_DIR" || echo "WARN: gsm8k-post failed for $CONFIG_NAME"

    # Record config result
    if [ "$MODE" = "pd" ]; then
      NUM_GPUS=$(yq "(.decode.lws_size * 4) + (.prefill.replicas * 4)" "$config_file")
    else
      NUM_GPUS=$(yq ".decode.lws_size * 4" "$config_file")
    fi
    CONFIG_END=$(date -Iseconds)
    jq --arg name "$CONFIG_NAME" --arg mode "$MODE" --argjson gpus "$NUM_GPUS" \
       --arg start "$CONFIG_START" --arg end "$CONFIG_END" \
       ".configs += [{name: \$name, mode: \$mode, num_gpus: \$gpus, start: \$start, end: \$end}]" \
       "$RUN_DIR/metadata.json" > "$RUN_DIR/metadata.tmp" && mv "$RUN_DIR/metadata.tmp" "$RUN_DIR/metadata.json"

    # Teardown
    just teardown
    echo "Waiting 30s for GPU release..."
    sleep 30
  done

  # Finalize metadata
  jq --arg end "$(date -Iseconds)" --argjson failed "$FAILED" \
     ". + {end: \$end, failed: \$failed}" \
     "$RUN_DIR/metadata.json" > "$RUN_DIR/metadata.tmp" && mv "$RUN_DIR/metadata.tmp" "$RUN_DIR/metadata.json"

  echo ""
  echo "=== Nightly run complete. Results: $RUN_DIR ==="

  # Post-processing
  if [ -f "{{justfile_directory()}}/postprocess/pareto.py" ]; then
    echo "Running post-processing..."
    python3 "{{justfile_directory()}}/postprocess/pareto.py" "$RUN_DIR" || echo "WARN: pareto.py failed"
    python3 "{{justfile_directory()}}/postprocess/regression.py" "$RUN_DIR" || echo "WARN: regression.py failed"
  fi

  exit $FAILED

# === Kueue (admin) ===

# Apply Kueue resources (requires cluster-admin)
apply-kueue:
  kubectl apply -f {{justfile_directory()}}/kueue/resource-flavor.yaml
  kubectl apply -f {{justfile_directory()}}/kueue/cluster-queue.yaml
  {{KN}} apply -f {{justfile_directory()}}/kueue/local-queue.yaml

# Check Kueue status
kueue-status:
  kubectl get clusterqueue nightly-eval -o wide
  kubectl get localqueue -n {{NAMESPACE}}
  kubectl get workloads -n {{NAMESPACE}}

# === Utilities ===

# Show GPU allocation across nodes
print-gpus:
  cd "{{LLMD_DIR}}" && just print-gpus

# Port-forward Prometheus
prometheus:
  kubectl port-forward -n {{NAMESPACE}} svc/prometheus-server 9090:80 > /dev/null 2>&1 &

# Port-forward Grafana
grafana:
  kubectl port-forward -n {{NAMESPACE}} svc/grafana 3000:80 > /dev/null 2>&1 &
