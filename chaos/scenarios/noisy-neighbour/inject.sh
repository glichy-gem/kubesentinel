#!/usr/bin/env bash
# Negative test (design doc section 7, scenario 8): spike CPU in an unrelated pod.
. "$(dirname "$0")/../../lib.sh"

kubectl apply -f "$(dirname "$0")/cpu-hog.yaml" >/dev/null
kubectl -n demo rollout status deployment/cpu-hog --timeout=180s >/dev/null
log "cpu-hog burning 1.5 cores next to orders-api"
