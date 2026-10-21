#!/usr/bin/env bash
# OOM-killed containers restart empty. A replica that survived may still hold
# memory, so ask both to release it (the Service picks a pod at random).
. "$(dirname "$0")/../../lib.sh"

for _ in 1 2 3 4 5 6; do
  curl -s -m 5 -o /dev/null -X POST "$APP_URL/chaos/memory/release" || true
done
kubectl -n demo rollout status deployment/orders-api --timeout=180s >/dev/null
log "memory released, orders-api ready"
