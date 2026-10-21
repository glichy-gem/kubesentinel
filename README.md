# KubeSentinel

AIOps for Kubernetes: turns a flood of alerts into **one incident** with a likely root cause, the evidence behind it, and a **safe, human-approved fix**.

> Status: Phase 4 (failure injection) complete. The agent does not exist yet; this README describes the goals the project is built against.

## Problem

Cloud-native apps produce large volumes of metrics, logs, events and alerts. When something breaks, an engineer correlates them by hand across Grafana, `kubectl`, deployment history and Git. That raises mean time to detect (MTTD) and mean time to resolve (MTTR). Alerting tools say *that* something is wrong, not *why*, and rarely link a symptom to the change that caused it. Small teams without a dedicated SRE feel this most.

| Pain point | What it looks like |
|---|---|
| Alert fatigue | One failure fires many related alerts |
| Slow diagnosis | Logs, metrics, events and deploys are checked one by one |
| Lost context | Nobody links "errors started at 14:02" to "a deploy went out at 14:00" |
| Repeated incidents | Known problems (OOMKilled, crash loop) are debugged from scratch every time |
| Unsafe automation | Blind auto-fixes can make things worse; no automation wastes time |

## Goals

- Detect each injected failure within 60 seconds of its first symptom.
- Group related alerts into a single incident.
- Produce a root-cause summary that names the cause and cites evidence (log lines, metric values, events, recent deploys).
- Auto-remediate only an allowlist of safe actions; everything else needs human approval.
- Measure the result against manual debugging, with numbers.

## Non-goals

- Replacing engineers or running fully autonomous operations.
- Covering every failure type. Version 1 covers five, more come later.
- Multi-cluster, production-grade SaaS in the first version.

## Success criteria for v1

These are starting goals, not industry benchmarks. They will be adjusted after the first baseline run.

| Metric | Target |
|---|---|
| Detection time (p50) | Under 60 s after the first symptom |
| Root-cause accuracy | Correct top cause in at least 8 of 10 runs per scenario |
| Alert reduction | At least 5 raw alerts grouped into 1 incident per scenario |
| Unsafe actions executed without approval | 0 |
| Time to diagnosis vs manual | At least 3x faster |

## How it works

1. **Delivery**: GitHub Actions builds images; Argo CD deploys from a GitOps repo, so every deploy is a commit.
2. **Observability**: Prometheus, Loki, Grafana and Alertmanager watch the demo app.
3. **Agent**: receives alerts, groups them into incidents, collects read-only evidence, matches known failure signatures, and asks an LLM to rank causes with cited evidence.
4. **Policy gate**: code (not the LLM) decides which actions run automatically, which need human approval, and which are never allowed.

Design principle: **rules first, LLM second**. Known failures are matched deterministically; the LLM explains, ranks and handles what the rules miss, and may only cite evidence it was actually given.

## Tech stack

kind (1 node) - Python / FastAPI / Pydantic - Prometheus, Loki, Grafana, Alertmanager - Argo CD - GitHub Actions - SQLite - Gemini (behind a provider interface) - Telegram for approvals.

## Quick start (current state)

Requires WSL2 with Docker Engine, `kind`, `kubectl`, `uv` and `make`, plus a clone of
[`kubesentinel-gitops`](https://github.com/glichy-gem/kubesentinel-gitops) next to this repo.

```bash
make cluster        # create the local kind cluster
make argocd         # install Argo CD (core) and point it at the GitOps repo
make argocd-status  # show Argo CD applications and the demo pods
make stack          # install Prometheus, Alertmanager, Grafana, Loki, Alloy, rules, dashboards
make grafana-password  # print the generated Grafana admin password
make test           # unit tests for orders-api (no cluster needed)
make agent-test     # unit tests for the agent (no cluster needed)
make rules-test     # promtool check + unit tests for the alert rules
make chaos S=oom    # inject one failure scenario, check its alerts, revert (see Failure scenarios)
make chaos-test     # unit tests for the chaos runner + shellcheck (no cluster needed)
make status         # show nodes and pods
make down           # delete the cluster
```

`make stack` needs about 1 GB of free memory on top of the cluster: close the browser and other
large apps first on an 8 GB machine (see [ADR 5](docs/adr/0005-observability-stack-on-8gb.md)).

After `make argocd`, Argo CD pulls the pinned image from GHCR and the demo app appears on its own
(allow a minute for the first sync).

Try the demo service once Argo CD reports `Synced` and `Healthy`:

```bash
curl localhost:8080/health
curl -X POST localhost:8080/orders -H 'content-type: application/json' -d '{"item":"book","quantity":2}'
curl localhost:8080/orders
curl localhost:8080/metrics
```

Ports mapped from your laptop into the cluster: `8080` (app), `3000` (Grafana), `9090` (Prometheus).

## Demo service: orders-api

A small FastAPI service backed by Redis. It exists so there is something realistic to observe and break.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness: the process is up. Never touches Redis |
| `GET /ready` | Readiness: returns 503 while Redis is unreachable |
| `GET /metrics` | Prometheus metrics: request count, latency histogram, 5xx count |
| `POST /orders`, `GET /orders`, `GET /orders/{id}` | Create and read orders |
| `GET /chaos/cpu`, `GET /chaos/memory` | Create CPU and memory pressure for failure scenarios (demo only) |

Logs are one JSON object per line with a request id, so they are easy to query in Loki.
When Redis goes down, the pods become `NotReady` and stop receiving traffic but are not restarted.

## Repository layout

```
apps/        demo services (orders-api, frontend, load generator)
agent/       the KubeSentinel agent
chaos/       failure-injection scenarios
evals/       evaluation runner and analysis
infra/       kind config, Helm values, alert rules, dashboards
docs/        threat model, ADRs, runbooks
```

A second repository, `kubesentinel-gitops`, holds the manifests that Argo CD watches.

## CI/CD

```
push to main -> CI: ruff + pytest -> build image -> hadolint + Trivy -> push ghcr.io/.../orders-api:sha-<commit>
             -> commit the new tag to kubesentinel-gitops -> Argo CD syncs the cluster
```

- Pull requests run lint, tests, build and scan only. Nothing is published and no secrets are used.
- Every deploy is a commit in the GitOps repo that records the source commit, author and run URL.
- Argo CD self-heals: a manual `kubectl` change that disagrees with Git is reverted within seconds.
- Rollback is a `git revert` in the GitOps repo.

See [ADR 4](docs/adr/0004-gitops-with-two-repos-and-argocd-core.md) for the reasoning and trade-offs.

## Observability

| URL (localhost only) | What |
|---|---|
| http://localhost:3000 | Grafana (user `admin`, password from `make grafana-password`) |
| http://localhost:9090 | Prometheus (targets, rules, alerts) |

Dashboards: **Service health (RED)**, **Cluster health**, **Incidents**. Logs from the `demo`
namespace are in Loki and queryable as JSON, for example `{app="orders-api"} | json | level="ERROR"`.

| Alert | Fires when | Severity |
|---|---|---|
| PodCrashLooping | more than 3 restarts in 5 minutes | critical |
| PodOOMKilled | last termination was OOMKilled and the pod restarted | critical |
| HighErrorRate | 5xx above 5% for 2 minutes | critical |
| HighLatencyP95 | p95 above 500 ms for 3 minutes | warning |
| DeploymentReplicasMismatch | fewer available replicas than desired for 5 minutes | warning |
| DeploymentRolloutStuck | not all replicas updated to the new version for 5 minutes | warning |
| PodImagePullBackOff | a container cannot pull its image for 1 minute | critical |
| PVCAlmostFull | volume above 85% for 5 minutes | warning |

Alertmanager groups alerts by `namespace` and `service` and posts them to the agent and to a dummy
receiver (`kubectl -n monitoring logs deploy/webhook-sink`).
Verified end to end: crashing `orders-api` by hand produced a `PodCrashLooping` notification at the
webhook 78 seconds later.

## The agent (in progress)

`agent/` is a FastAPI service that Argo CD deploys to the `kubesentinel` namespace. Alertmanager
posts every alert to it (and to the dummy sink, which the chaos runner uses as an independent clock).

Built so far (Phase 5a):

- **Intake**: `POST /alerts` validates the Alertmanager payload with Pydantic and stores every alert
  in SQLite. Repeat notifications of the same alert update it instead of creating new work.
- **Correlation**: alerts with the same namespace and service join one incident. An incident resolves
  when none of its alerts is firing, reopens if they return, and closes after 10 quiet minutes.
  `raw_alert_count` per incident is the alert-reduction metric.
- **API**: `GET /incidents`, `GET /incidents/{id}` (with evidence and diagnosis),
  `POST /incidents/{id}/investigate`, plus `/metrics` for Prometheus.

Phase 5b: every new incident is investigated in the background
(see [ADR 7](docs/adr/0007-agent-rules-first-evidence-ids.md)):

1. **Collect** read-only evidence as short facts with ids: pod status and last termination reason,
   warning events, error log lines from Loki, metrics from Prometheus (error ratio, p95, memory and
   CPU against limits, restarts), dependency health, and recent deploys from Argo CD with the
   commit message and diff from GitHub. Secrets are redacted; env var values are never read.
2. **Match** known failure signatures ([`signatures.yaml`](agent/app/signatures.yaml)) for a
   candidate cause and action that cite the facts behind them.
3. **Diagnose** with Gemini, which must return schema-valid JSON that cites fact ids. Invented
   ids are dropped, risky actions always need a human, and if Gemini is busy or wrong the agent falls
   back to a lighter model, then to the matcher.

Live result on an injected OOM: 13 facts collected in 1.3 s; matcher and Gemini both concluded
`memory_limit_too_low`, citing the two `OOMKilled (exit 137)` pods and the 128 Mi limit, and
proposed `increase_memory_limit` with approval required.

```bash
make agent-secret   # copy GEMINI_API_KEY etc. from .env into a Kubernetes Secret
kubectl -n kubesentinel port-forward svc/kubesentinel-agent 8000:8000
curl localhost:8000/incidents/1
```

Next (5c): the policy gate, Telegram incident cards with Approve / Reject, and the actions.

## Failure scenarios

Each scenario in [`chaos/scenarios/`](chaos/scenarios/) has an `inject.sh`, a `revert.sh` and an
`expected.yaml` answer key (alerts that must fire, root cause, expected action and approval mode).
`make chaos S=<name>` waits for a healthy baseline, injects the fault, watches what reaches the
Alertmanager webhook, always reverts, waits for recovery and writes a JSON result to `chaos/results/`.

Release faults are delivered like real ones: a commit to `kubesentinel-gitops` that Argo CD rolls
out, undone with `git revert`. See [ADR 6](docs/adr/0006-chaos-scenarios-as-data.md).

| Scenario | Fault | Expected alert | Root cause / action | Detected | Recovered |
|---|---|---|---|---|---|
| bad-config | commit adds an invalid env var; new pods crash at startup | PodCrashLooping | bad release / rollback (approval) | 138 s | 21 s |
| bad-image | commit deploys an image tag that does not exist | PodImagePullBackOff | image pull error / rollback (approval) | 101 s | 20 s |
| oom | requests allocate memory past the 128 Mi limit | PodOOMKilled | memory limit too low / raise limit (approval) | 74 s | 1 s |
| dependency-down | Redis scaled to 0 (Argo CD sync paused) | HighErrorRate | dependency down / restart it (approval) | 166 s | 7 s |
| saturation | k6 Job, 20 users on a CPU-heavy endpoint | HighLatencyP95 | saturation / scale up (auto) | 273 s | 11 s |

Measured on 2026-10-21, one run each, on the 8 GB laptop. Detection time is from injection to the
alert reaching the webhook, so it includes rollout time, scrape and rule intervals, each rule's `for:`
duration and Alertmanager `group_wait`. Recovery is from the start of the revert to every deployment
ready, `/ready` returning 200 and Argo CD synced and healthy.

## Roadmap

| Phase | Weeks | Content |
|---|---|---|
| 0 | 1 | Planning and setup (done) |
| 1 | 2 | Demo application (done) |
| 2 | 3 | CI/CD with GitOps (done) |
| 3 | 4-5 | Observability and alert rules (done) |
| 4 | 6 | Failure injection framework (done) |
| 5 | 7-8 | The agent |
| 6 | 9 | Testing and evaluation |
| 7 | 10 | Documentation, demo, v1.0 |

## Documentation

- [Threat model](docs/threat-model.md)
- [Architecture decision records](docs/adr/)

## License

MIT, see [LICENSE](LICENSE).
