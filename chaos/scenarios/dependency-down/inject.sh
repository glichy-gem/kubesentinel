#!/usr/bin/env bash
# No release: Redis disappears. Argo CD would restore it within seconds (replicas
# is in Git), so auto-sync is paused for Redis first, like a real outage that
# GitOps cannot fix on its own.
. "$(dirname "$0")/../../lib.sh"

argocd_pause redis
kubectl -n demo scale deployment/redis --replicas=0 >/dev/null
log "redis scaled to 0 (Argo CD auto-sync for redis paused)"
