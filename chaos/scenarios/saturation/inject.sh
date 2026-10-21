#!/usr/bin/env bash
# No release: a traffic spike on a CPU-heavy endpoint saturates orders-api.
. "$(dirname "$0")/../../lib.sh"

kubectl apply -f "$(dirname "$0")/k6-job.yaml" >/dev/null
kubectl -n chaos wait --for=condition=Ready pod -l job-name=k6-saturation --timeout=180s >/dev/null
log "k6 load generator running (20 virtual users for about 7 minutes)"
