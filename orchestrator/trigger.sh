#!/usr/bin/env bash
#
# Trigger a nightly eval run from any branch.
#
# Usage:
#   ./trigger.sh                          # run from main
#   ./trigger.sh deepseek-v4-nightly      # run from a branch
#   ./trigger.sh deepseek-v4-nightly my-run-name
#
set -euo pipefail

BRANCH="${1:-main}"
JOB_NAME="${2:-nightly-eval-$(echo "$BRANCH" | tr '/' '-' | cut -c1-40)-$(date +%H%M%S)}"
NAMESPACE="${NAMESPACE:-vllm}"

echo "Triggering nightly eval from branch: $BRANCH"
echo "  Job name: $JOB_NAME"
echo "  Namespace: $NAMESPACE"

kubectl -n "$NAMESPACE" create job "$JOB_NAME" \
  --from=cronjob/nightly-eval \
  --dry-run=client -o json \
  | jq --arg branch "$BRANCH" \
    '(.spec.template.spec.initContainers[0].args[0]) =
      "git clone --depth=1 -b " + $branch +
      " https://github.com/tlrmchlsmth/nightly-eval.git /workspace/nightly-eval"' \
  | kubectl -n "$NAMESPACE" apply -f -

echo ""
echo "Job created. Watch with:"
echo "  kubectl -n $NAMESPACE logs -f job/$JOB_NAME"
