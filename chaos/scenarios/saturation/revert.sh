#!/usr/bin/env bash
# Stop the load. Saturation is caused by traffic, so removing it is the revert.
. "$(dirname "$0")/../../lib.sh"

kubectl delete namespace chaos --ignore-not-found --wait=true --timeout=120s >/dev/null
kubectl -n demo rollout status deployment/orders-api --timeout=180s >/dev/null
log "load generator removed, orders-api ready"
