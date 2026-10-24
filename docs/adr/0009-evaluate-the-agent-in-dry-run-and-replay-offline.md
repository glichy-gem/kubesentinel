# 9. Evaluate the agent live in dry-run mode, then replay offline

Date: 2026-10-24
Status: Accepted

## Context

A single successful demo proves little: LLM output varies, and the cluster is noisy. The design doc asks for every scenario to be run 10 times, with the agent's root cause and proposed action compared with the expected answer, and for configurations to be compared (matcher only, LLM only, both, two prompt versions). On this laptop a live run takes 5–10 minutes, and the free Gemini tier allows a limited number of calls per day. A fully live agent would also send about 70 Telegram cards, and its automatic actions would fight the chaos runner's own revert.

## Decision

- **Live runs grade the real pipeline, in dry-run mode.** `make eval-mode-on` sets `KS_DRY_RUN` through an optional ConfigMap that is not stored in Git. The agent still collects evidence, diagnoses, plans the exact change, and asks the real policy for a decision. It records the outcome as `dry_run` and does nothing else: no card, no commit, no pod deleted. The runner refuses to start unless `/version` reports dry-run. Approving and executing were proven separately in Phase 5, and stay covered by unit tests.
- **The first diagnosis is graded**, because it is what a human sees first. Later re-investigations are recorded but don't count.
- **A run is correct when** the cause matches `expected.yaml`, where listed alternatives count (for example `config_invalid` for a bad config release), and the action type matches. Unsafe actions (anything that ran) must be zero.
- **Every diagnosis saves a snapshot** of exactly what it saw: incident, facts, signals and which fact supports each signal. `make replay` re-runs only the diagnosis step on those snapshots under each configuration. That is seconds per run instead of minutes, needs no cluster, and is the only fair comparison: every configuration sees identical evidence. Replies are cached, so quota errors are retried rather than counted.
- **The prompt-injection run only counts if the hostile line reached the evidence.** An injection test that never reaches the model proves nothing. The first version (a hostile config value) would have failed this check.
- **Scenarios run round robin** (a b c a b c), so drift over the evening (quota, laptop load, Gemini's "high demand" periods) spreads across all scenarios.
- **Results and snapshots are committed** (`evals/`): the README's numbers can be recomputed by anyone, and CI replays the matcher on them to stop regressions.
- **The manual baseline is blind:** `make manual` injects a random, hidden scenario.

## Alternatives considered

- **Fully live agent with automatic approval:** measures time to recovery too, but needs a test-only approval bypass in the agent, the riskiest code path to add for a test. It also doubles run time and causes Git churn. Recovery was demonstrated live in Phase 5 instead.
- **Only offline evaluation:** cheap, but snapshots of synthetic incidents would test the model, not the system. Live runs catch collector and timing problems; the very first one did.
- **A notebook for the analysis:** the design doc suggests one. A script (`make report`) is rerunnable, reviewable in diffs and gives the same tables and charts.

## Consequences

- Time to recovery after the agent's action is not measured in the batch. The Phase 5 live run (118 s) is the evidence for it.
- Dry-run decisions don't count toward rate limits. The policy's rate-limit paths are covered by unit tests, not by the batch.
