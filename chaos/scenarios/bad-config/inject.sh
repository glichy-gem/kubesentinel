#!/usr/bin/env bash
# A release ships an invalid setting ("1.5s" where a number is expected). The app
# validates its settings at startup, so every new pod exits and crash loops while
# the old pods keep serving. Delivered through Git, like a real release.
. "$(dirname "$0")/../../lib.sh"

gitops_sync
yq_gitops '.spec.template.spec.containers[0].env += [{"name": "ORDERS_REDIS_TIMEOUT_SECONDS", "value": "1.5s"}]' \
  apps/orders-api/deployment.yaml
SHA=$(gitops_commit_push "chore(orders-api): tune redis client timeout")
echo "$SHA" > "$STATE_DIR/bad-config.sha"
argocd_refresh orders-api
log "bad config released in gitops commit ${SHA:0:7}"
