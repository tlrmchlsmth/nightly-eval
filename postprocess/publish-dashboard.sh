#!/usr/bin/env bash
# Publish dashboard.html to the gh-pages branch of the nightly-eval repo.
#
# Usage:
#   publish-dashboard.sh <run_dir>
#
# Environment:
#   GITHUB_TOKEN   — required, used for push authentication
#   REPO           — optional, defaults to tlrmchlsmth/nightly-eval
#   RETENTION_DAYS — optional, defaults to 30 (prune older archived runs)
set -euo pipefail

RUN_DIR="$1"
RUN_DATE=$(basename "$RUN_DIR")
REPO="${REPO:-tlrmchlsmth/nightly-eval}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "ERROR: GITHUB_TOKEN not set, skipping publish" >&2
  exit 1
fi

if [ ! -f "$RUN_DIR/dashboard.html" ]; then
  echo "ERROR: $RUN_DIR/dashboard.html not found" >&2
  exit 1
fi

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git"

# Clone or create gh-pages branch
if git ls-remote --heads "$REPO_URL" gh-pages 2>/dev/null | grep -q gh-pages; then
  git clone --depth=50 --branch gh-pages "$REPO_URL" "$WORK_DIR/pages"
else
  mkdir -p "$WORK_DIR/pages"
  git -C "$WORK_DIR/pages" init
  git -C "$WORK_DIR/pages" checkout --orphan gh-pages
  git -C "$WORK_DIR/pages" remote add origin "$REPO_URL"
fi

cd "$WORK_DIR/pages"

# Archive this run
mkdir -p "runs/$RUN_DATE"
cp "$RUN_DIR/dashboard.html" "runs/$RUN_DATE/index.html"

# Latest always at root
cp "$RUN_DIR/dashboard.html" index.html

# Prune runs older than retention window
if [ -d runs ]; then
  cutoff=$(date -d "-${RETENTION_DAYS} days" +%Y-%m-%d 2>/dev/null \
           || date -v-${RETENTION_DAYS}d +%Y-%m-%d 2>/dev/null \
           || echo "")
  if [ -n "$cutoff" ]; then
    for dir in runs/*/; do
      run_name=$(basename "$dir")
      if [[ "$run_name" < "$cutoff" ]]; then
        rm -rf "$dir"
        echo "Pruned old run: $run_name"
      fi
    done
  fi
fi

# Commit and push
git add -A
if git diff --cached --quiet; then
  echo "No changes to publish"
  exit 0
fi

git -c user.name="nightly-bot" -c user.email="nightly-bot@noreply" \
  commit -m "Dashboard update: $RUN_DATE"
git push origin gh-pages || {
  git pull --rebase origin gh-pages
  git push origin gh-pages
}

echo "Published dashboard for $RUN_DATE"
