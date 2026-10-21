# 5. Observability stack sized for an 8 GB laptop

Date: 2026-10-21
Status: Accepted

## Context

The agent can only diagnose what the stack measures: metrics, logs, Kubernetes state and alerts. The whole platform runs in a 4 GB WSL2 VM on a 7.7 GB laptop that also runs a browser and an editor. The first install attempt failed: the load average reached 41, Grafana was killed repeatedly, and the control plane pods restarted.

## Decision

- **kube-prometheus-stack**, trimmed: no default rules, no node-exporter, no control-plane scrapers, admission webhooks off, explicit limits on every component, 2-day retention, no persistence.
- **Loki** as a single binary on filesystem storage, caches, gateway and canary off, 48-hour retention.
- **Grafana Alloy** to ship logs (Promtail reached end of life in 2026), reading pod logs through the Kubernetes API for the `demo` namespace only.
- **Grafana pinned to 11.6**, the bundled plugin installer and update checks turned off, one sidecar for dashboards, datasources provisioned from a file.
- A **PodMonitor**, not a ServiceMonitor, for `orders-api`, so pods are still scraped while NotReady.
- The stack is installed with **Helm from `make stack`**, not by Argo CD. Argo CD deploys the application; the platform is installed once per cluster.
- Ports are bound to `127.0.0.1` in the kind config.

## Alternatives considered

- **ELK / OpenSearch for logs**: far heavier than Loki.
- **Grafana 13 (the chart default)**: on this machine it needed 3 to 4 minutes to start, used its whole 256 MiB limit, and hung after start. Rejected for now.
- **Promtail**: end of life.
- **Argo CD managing the stack**: possible with multi-source Applications, but adds CRD ordering problems for little gain in a single-cluster lab.

## Consequences

- The full stack runs, but only with the browser and other large apps closed. Measured headroom after install: about 1.2 GiB available inside WSL.
- Grafana 11.6 is an older release line. Acceptable because Grafana is localhost-only and stores nothing we must keep; revisit on a bigger machine.
- Everything is ephemeral: a cluster rebuild loses metrics and logs. Fine for a lab, wrong for production.

## Lessons recorded while building it

- A tight CPU limit can kill a service through its liveness probe: slow startup looks like a hang.
- Helm merges maps but replaces lists. Replacing the Alertmanager receivers without `routes: []` left a default route pointing at a receiver that no longer existed, and the operator silently refused to create Alertmanager.
- With persistence off, the Loki chart mounts nothing at `/var/loki`; with a read-only root filesystem Loki cannot start until a scratch volume is added.
- **Argo CD self-heal only reverts fields that Git manages.** Adding a field that is not in the manifest (for example a `command`) is not drift in its diff. Chaos scenarios therefore need an explicit `revert.sh`.
