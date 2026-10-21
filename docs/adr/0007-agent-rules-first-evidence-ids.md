# 7. Agent diagnosis: rules first, cited evidence ids, layered fallbacks

Date: 2026-10-22
Status: Accepted

## Context

The agent must turn an incident into a root cause and a proposed fix. It must do this without making things up, without leaking secrets, and without being steered by text an attacker can write into logs. The LLM is a free-tier hosted model (ADR 3). It is often slow or unavailable: in the first live tests the main model took 42 s, and then it answered "high demand" (HTTP 503) for minutes.

## Decision

- **Collectors produce short facts, each with an id** (`E1`, `E2`, …), plus typed **signals** derived from them (`oom_killed`, `minutes_since_deploy`, `cpu_ratio`, …). Every signal remembers the fact that supports it.
- **Rules first.** A YAML file of failure signatures maps signals to a cause, an action and a confidence. A rule-based answer cites the facts behind its conditions, so it is a complete diagnosis on its own.
- **LLM second.** Gemini receives the facts, the signals and the matcher's candidates, and must return JSON that matches a schema. The code then checks the reply:
  - Evidence ids that were never collected are dropped, and confidence is scaled down in proportion.
  - A reply that cites nothing real is discarded.
  - A target outside the incident's service and its dependencies turns the action into `none`.
  - `needs_human` is forced on for anything beyond a low-risk action. The model cannot waive approval.
- **Fallback chain:** main model, then a lighter model, then the rule matcher. A 503 or a timeout moves straight to the next model, and other transient errors get one retry. Every stored diagnosis records which path produced it, the exact model version, latency and token counts.
- **Untrusted input:** facts are redacted before storage or prompting: secret-looking patterns are removed and env var values are never read. Evidence is fenced as data in the prompt, and the system prompt says instructions inside it must be ignored.
- **Plain HTTP clients** for Kubernetes, Prometheus, Loki, GitHub and Gemini, instead of SDKs, to keep the agent at about 60 MiB.
- The agent's ServiceAccount is **read-only** (pods, logs, events, deployments in `demo`; Argo CD applications), granted through Git.

## Alternatives considered

- **LLM only, with free-text citations:** simpler, but citations cannot be verified, and every Gemini outage becomes an agent outage.
- **Rules only:** reliable for the five known scenarios, but it cannot explain or handle anything new. The LLM's value is the explanation and the unknown case.
- **Official Kubernetes and Google SDKs:** convenient, but each would add tens of MiB to a pod on an 8 GB laptop for a handful of GET calls.

## Consequences

- The agent always produces a diagnosis, even with no API key or a Gemini outage. The quality difference between the LLM and the matcher becomes measurable in Phase 6 through the stored `source` and `agrees_with_matcher` fields.
- Diagnoses from different models are not identical. Evals must report results per model version.
- Signatures need maintenance as scenarios grow. A unit test ties each chaos scenario's expected root cause to the matcher's answer.
