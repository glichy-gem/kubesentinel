#!/usr/bin/env bash
# A deploy points at an image tag that was never pushed, exactly what CI would
# commit if a build half-failed. New pods sit in ImagePullBackOff; old pods serve.
. "$(dirname "$0")/../../lib.sh"

gitops_sync
yq_gitops '.images[0].newTag = "sha-0000000"' apps/orders-api/kustomization.yaml
SHA=$(gitops_commit_push "deploy(orders-api): sha-0000000")
echo "$SHA" > "$STATE_DIR/bad-image.sha"
argocd_refresh orders-api
log "deployed a missing image tag in gitops commit ${SHA:0:7}"
