#!/usr/bin/env bash
# Roll back the bad release with a Git revert, the same way the agent will.
. "$(dirname "$0")/../../lib.sh"

SHA=$(cat "$STATE_DIR/bad-config.sha")
gitops_revert_push "$SHA"
argocd_refresh orders-api
rm -f "$STATE_DIR/bad-config.sha"
log "reverted gitops commit ${SHA:0:7}"
