#!/usr/bin/env bash
. "$(dirname "$0")/../../lib.sh"

kubectl delete -f "$(dirname "$0")/cpu-hog.yaml" --ignore-not-found --wait=true --timeout=120s >/dev/null
log "cpu-hog removed"
