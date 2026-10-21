#!/usr/bin/env bash
# No release: traffic makes orders-api hold more memory than its 128 MiB limit,
# so the kernel OOM-kills the container. Uses the demo app's /chaos/memory endpoint.
. "$(dirname "$0")/../../lib.sh"

before=$(orders_api_restarts)
now=$before
log "allocating memory in orders-api until pods are OOMKilled (restarts before: $before)"
for _ in $(seq 1 60); do
  curl -s -m 5 -o /dev/null "$APP_URL/chaos/memory?mb=20" || true
  now=$(orders_api_restarts)
  # Two restarts: both replicas, or one replica twice.
  if [ "$now" -ge $((before + 2)) ]; then
    break
  fi
  sleep 0.5
done
log "orders-api restarts: $before -> $now"
