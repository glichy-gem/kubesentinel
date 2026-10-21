#!/usr/bin/env bash
. "$(dirname "$0")/../../lib.sh"

SHA=$(cat "$STATE_DIR/bad-image.sha")
gitops_revert_push "$SHA"
argocd_refresh orders-api
rm -f "$STATE_DIR/bad-image.sha"
log "reverted gitops commit ${SHA:0:7}"
