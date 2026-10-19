# KubeSentinel

AIOps for Kubernetes: turns a flood of alerts into **one incident** with a likely root cause, the evidence behind it, and a **safe, human-approved fix**.

> Status: Phase 0 (planning and setup). The agent does not exist yet; this README describes the goals the project is built against.

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

Requires WSL2 with Docker Engine, `kind`, `kubectl` and `make`.

```bash
make cluster   # create the local kind cluster
make status    # show nodes and pods
make down      # delete the cluster
```

Ports mapped from your laptop into the cluster: `8080` (app), `3000` (Grafana), `9090` (Prometheus).

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

## Roadmap

| Phase | Weeks | Content |
|---|---|---|
| 0 | 1 | Planning and setup (this phase) |
| 1 | 2 | Demo application |
| 2 | 3 | CI/CD with GitOps |
| 3 | 4-5 | Observability and alert rules |
| 4 | 6 | Failure injection framework |
| 5 | 7-8 | The agent |
| 6 | 9 | Testing and evaluation |
| 7 | 10 | Documentation, demo, v1.0 |

## Documentation

- [Threat model](docs/threat-model.md)
- [Architecture decision records](docs/adr/)

## License

MIT, see [LICENSE](LICENSE).
