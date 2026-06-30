#!/usr/bin/env bash
#
# Trigger a nightly eval run from any branch.
#
# Usage:
#   ./trigger.sh                          # run from main
#   ./trigger.sh deepseek-v4-nightly      # run from a branch
#   ./trigger.sh deepseek-v4-nightly my-run-name
#
# The VLLM_IMAGE env var is read from the branch's run-nightly.sh default,
# not from the in-cluster cronjob spec.
#
set -euo pipefail

BRANCH="${1:-main}"
JOB_NAME="${2:-nightly-orchestrator-$(echo "$BRANCH" | tr '/' '-' | cut -c1-40)-$(date +%H%M%S)}"
NAMESPACE="${NAMESPACE:-vllm}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VLLM_IMAGE=$(grep 'VLLM_IMAGE=.*:-' "$SCRIPT_DIR/run-nightly.sh" | head -1 | sed 's/.*:-\(.*\)}.*/\1/')

echo "Triggering nightly eval from branch: $BRANCH"
echo "  Job name: $JOB_NAME"
echo "  Image: $VLLM_IMAGE"
echo "  Namespace: $NAMESPACE"

kubectl -n "$NAMESPACE" create job "$JOB_NAME" \
  --from=cronjob/nightly-eval \
  --dry-run=client -o json \
  | jq --arg branch "$BRANCH" --arg image "$VLLM_IMAGE" \
    '(.spec.template.spec.initContainers[0].args[0]) =
      "git clone --depth=1 -b " + $branch +
      " https://github.com/elvircrn/nightly-eval.git /workspace/nightly-eval"
    | (.spec.template.spec.containers[0].env[] | select(.name == "VLLM_IMAGE") | .value) = $image' \
  | kubectl -n "$NAMESPACE" apply -f -

echo ""
echo "Job created. Watch with:"
echo "  kubectl -n $NAMESPACE logs -f job/$JOB_NAME"
