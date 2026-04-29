#!/usr/bin/env bash
set -euo pipefail

VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:nightly}"
NAMESPACE="${NAMESPACE:-vllm}"
NIGHTLY_DIR="${NIGHTLY_DIR:-/workspace/nightly-eval}"
LUSTRE_PREFIX="${LUSTRE_PREFIX:-/mnt/lustre/nightly}"
RUN_DIR="$LUSTRE_PREFIX/results/$(date +%Y-%m-%d)"
mkdir -p "$RUN_DIR"

echo "=== Nightly eval starting at $(date -Iseconds) ==="
echo "  vLLM image: $VLLM_IMAGE"
echo "  Results:    $RUN_DIR"

cat > "$RUN_DIR/metadata.json" <<EOF
{
  "vllm_image": "$VLLM_IMAGE",
  "start": "$(date -Iseconds)",
  "configs": []
}
EOF

FAILED=0
CONFIGS_RUN=0

for config_file in "$NIGHTLY_DIR"/configs/*.yaml; do
  CONFIG_NAME=$(yq '.name' "$config_file")
  CONFIG_DESC=$(yq '.description // ""' "$config_file")
  echo ""
  echo "=========================================="
  echo "  Config: $CONFIG_NAME"
  echo "  $CONFIG_DESC"
  echo "=========================================="

  CONFIG_START=$(date -Iseconds)

  # Deploy serving stack
  echo "[$(date +%H:%M:%S)] Deploying serving stack..."
  if ! VLLM_IMAGE="$VLLM_IMAGE" just -f "$NIGHTLY_DIR/Justfile" deploy "$config_file"; then
    echo "FAIL: deploy $CONFIG_NAME"
    FAILED=$((FAILED + 1))
    just -f "$NIGHTLY_DIR/Justfile" teardown || true
    continue
  fi

  # Wait for readiness
  echo "[$(date +%H:%M:%S)] Waiting for readiness..."
  if ! just -f "$NIGHTLY_DIR/Justfile" wait-ready; then
    echo "FAIL: readiness $CONFIG_NAME (timeout)"
    FAILED=$((FAILED + 1))
    just -f "$NIGHTLY_DIR/Justfile" teardown || true
    continue
  fi

  echo "[$(date +%H:%M:%S)] Serving stack ready."

  # GSM8K pre-eval
  echo "[$(date +%H:%M:%S)] Running GSM8K pre-eval..."
  just -f "$NIGHTLY_DIR/Justfile" bench-gsm8k "${CONFIG_NAME}-gsm8k-pre" "$RUN_DIR" \
    || echo "WARN: gsm8k-pre failed for $CONFIG_NAME"

  # Staircase concurrency sweep
  echo "[$(date +%H:%M:%S)] Running staircase sweep..."
  just -f "$NIGHTLY_DIR/Justfile" bench-staircase "$CONFIG_NAME" "$RUN_DIR" \
    || echo "WARN: staircase failed for $CONFIG_NAME"

  # GSM8K post-eval
  echo "[$(date +%H:%M:%S)] Running GSM8K post-eval..."
  just -f "$NIGHTLY_DIR/Justfile" bench-gsm8k "${CONFIG_NAME}-gsm8k-post" "$RUN_DIR" \
    || echo "WARN: gsm8k-post failed for $CONFIG_NAME"

  CONFIG_END=$(date -Iseconds)
  CONFIGS_RUN=$((CONFIGS_RUN + 1))

  # Append config result to metadata
  NUM_GPUS=$(yq '(.decode.lws_size * 4) + (.prefill.replicas * 4)' "$config_file")
  python3 -c "
import json, sys
with open('$RUN_DIR/metadata.json') as f:
    meta = json.load(f)
meta['configs'].append({
    'name': '$CONFIG_NAME',
    'num_gpus': $NUM_GPUS,
    'start': '$CONFIG_START',
    'end': '$CONFIG_END',
})
with open('$RUN_DIR/metadata.json', 'w') as f:
    json.dump(meta, f, indent=2)
"

  # Teardown
  echo "[$(date +%H:%M:%S)] Tearing down..."
  just -f "$NIGHTLY_DIR/Justfile" teardown || true

  echo "[$(date +%H:%M:%S)] Waiting 30s for GPU release..."
  sleep 30
done

# Update metadata with end time
python3 -c "
import json
with open('$RUN_DIR/metadata.json') as f:
    meta = json.load(f)
meta['end'] = '$(date -Iseconds)'
meta['configs_run'] = $CONFIGS_RUN
meta['configs_failed'] = $FAILED
with open('$RUN_DIR/metadata.json', 'w') as f:
    json.dump(meta, f, indent=2)
"

echo ""
echo "=== Nightly eval complete ==="
echo "  Configs run: $CONFIGS_RUN"
echo "  Configs failed: $FAILED"
echo "  Results: $RUN_DIR"

# Run post-processing if available
if [ -f "$NIGHTLY_DIR/postprocess/pareto.py" ]; then
  echo ""
  echo "Running post-processing..."
  python3 "$NIGHTLY_DIR/postprocess/pareto.py" "$RUN_DIR" || echo "WARN: pareto.py failed"
  python3 "$NIGHTLY_DIR/postprocess/regression.py" "$RUN_DIR" || echo "WARN: regression.py failed"
fi

exit $FAILED
