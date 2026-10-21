#!/usr/bin/env bash
. "$(dirname "$0")/../../lib.sh"

kubectl -n demo scale deployment/redis --replicas=1 >/dev/null
kubectl -n demo rollout status deployment/redis --timeout=120s >/dev/null
argocd_resume redis
kubectl -n demo rollout status deployment/orders-api --timeout=180s >/dev/null
log "redis restored, Argo CD auto-sync for redis resumed"
