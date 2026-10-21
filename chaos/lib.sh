#!/usr/bin/env bash
# Shared helpers for chaos scenario scripts.
# Each scenario sources it: . "$(dirname "$0")/../../lib.sh"
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GITOPS_DIR="${GITOPS_DIR:-$REPO_ROOT/../kubesentinel-gitops}"
STATE_DIR="$REPO_ROOT/.tmp/chaos"
YQ_IMAGE="${YQ_IMAGE:-mikefarah/yq:4.47.1}"
APP_URL="${APP_URL:-http://localhost:8080}"
mkdir -p "$STATE_DIR"

log() { echo "[$(date -u +%H:%M:%S)] $*" >&2; }

# --- GitOps changes: the realistic way a bad release reaches the cluster -------

gitops_sync() {
  git -C "$GITOPS_DIR" pull -q --rebase origin main
}

# yq_gitops '<expression>' <path inside the gitops repo>
yq_gitops() {
  docker run --rm --user "$(id -u):$(id -g)" -v "$GITOPS_DIR:/w" "$YQ_IMAGE" -i "$1" "/w/$2"
}

push_with_retry() {
  for _ in 1 2 3; do
    if git -C "$GITOPS_DIR" pull -q --rebase origin main && git -C "$GITOPS_DIR" push -q origin HEAD:main; then
      return 0
    fi
    sleep 3
  done
  log "could not push to the gitops repo"
  return 1
}

# Commits with a realistic message (no "chaos" in it, so the agent is not tipped
# off by the commit history) and prints the new commit SHA.
gitops_commit_push() {
  git -C "$GITOPS_DIR" add -A
  git -C "$GITOPS_DIR" commit -q -m "$1"
  push_with_retry
  git -C "$GITOPS_DIR" rev-parse HEAD
}

gitops_revert_push() {
  gitops_sync
  git -C "$GITOPS_DIR" revert --no-edit "$1" >/dev/null
  push_with_retry
}

# --- Argo CD ---------------------------------------------------------------------

# Ask Argo CD to look at Git now instead of on its 3-minute poll (a Git webhook
# does this in real setups).
argocd_refresh() {
  kubectl -n argocd annotate application "$1" argocd.argoproj.io/refresh=normal --overwrite >/dev/null
}

# Self-heal would undo a manual change to fields Git manages (for example replicas),
# so non-Git faults pause auto-sync and their revert turns it back on.
argocd_pause() {
  kubectl -n argocd patch application "$1" --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
}

argocd_resume() {
  kubectl -n argocd patch application "$1" --type merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}' >/dev/null
}

# --- Cluster helpers ---------------------------------------------------------------

orders_api_restarts() {
  kubectl -n demo get pods -l app=orders-api \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' |
    awk '{s += $1} END {print s + 0}'
}
