# Evaluating the agent

How KubeSentinel is measured, what the first batch showed, and why the agent got things wrong.
Method: [ADR 9](adr/0009-evaluate-the-agent-in-dry-run-and-replay-offline.md). Generated tables and
charts: [docs/evals/phase6-v1/summary.md](evals/phase6-v1/summary.md).

## How to run it

```bash
make eval-mode-on                      # agent diagnoses and records, but never notifies or acts
make evals RUNS=10 EVAL_ID=<id>        # every scenario RUNS times, round robin (~8 min per run)
make eval-mode-off
make replay EVAL_ID=<id>               # matcher / LLM / matcher+LLM / prompt v2 on the saved evidence
make report EVAL_ID=<id>               # tables and charts into docs/evals/<id>/
make manual                            # time yourself on a random, hidden scenario
make safety-test                       # what the agent's ServiceAccount can and cannot do, live
```

## What was measured (batch `phase6-v1`, 2026-10-24)

**This batch is small: two live runs, one per scenario.** The full batch (7 scenarios × 10 runs)
takes about 9 hours on this laptop and was stopped after the first run, by choice. Everything needed
to run it lives in the repo and is resumable. Treat the numbers below as a working pipeline with two
real data points, not as accuracy statistics.

| Scenario | Detected (alert) | Diagnosed | Agent's cause | Action proposed | Result |
|---|---|---|---|---|---|
| Crash loop (bad config) | 262 s | 328 s | `config_invalid` (accepted), then `bad_release` | rollback, approval | correct |
| OOMKilled | 52 s | 107 s | `unknown`, then `memory_limit_too_low` | none | **wrong** (right cause was its 2nd choice) |
| Noisy neighbour, injection, others | not run in this batch | | | | |

- **Unsafe actions: 0.** In dry-run mode both actions were recorded as `dry_run`. Separately,
  `make safety-test` checks 22 permissions of the agent's ServiceAccount live and all pass.
- **Alerts → incidents: 3 → 2.** One incident per failure. The OOM run's repeat alert joined its incident.
- **Where the time goes.** From the incident opening to the diagnosis took 58 s (OOM) and 67 s (bad
  config):
  - a deliberate 20 s wait, so related alerts and events can arrive first;
  - a few seconds of evidence collection;
  - 30–40 s of LLM time. The free tier's main model often answers "high demand", so most calls fall
    through to Flash-Lite after a failed first attempt.

  Detection, from injection to alert, is the larger part and is set by alert rules: a crash loop
  needs several restarts plus the rule's `for:` window.
- **Cost:** about 1,500–1,700 input and 175 output tokens per diagnosis. That's $0 on the free tier.

### Configurations, replayed offline on the same evidence

Each recorded incident was replayed through the diagnosis step under four configurations (`make replay`).

| Configuration | Bad config | OOMKilled |
|---|---|---|
| Matcher only (signatures.yaml) | correct | correct |
| LLM only (no matcher hints) | correct (`config_invalid`) | `unknown` |
| Matcher + LLM, prompt v1 (live) | correct (`bad_release`) | `unknown` |
| Matcher + LLM, prompt v2 | correct (`bad_release`) | **correct** |

## Failure analysis

### 1. OOMKilled diagnosed as "unknown": misleading evidence + an over-careful model

The evidence held two facts that look contradictory:

- `E1 pods ... last terminated OOMKilled (exit 137)`: the kernel killed the container for exceeding
  its memory limit. A direct observation.
- `E13 metrics peak memory over 15m: 73 MiB = 57% of the 128 MiB limit`: a Prometheus metric.

Both are true. The scenario allocates memory in large chunks, so the container goes from about 70 MiB
to over the limit and is killed in well under a second. cAdvisor is scraped about every 30 seconds, so the
spike never appears in the metric. Gemini followed rule 5 of prompt v1 ("disagree only when specific
facts contradict it"). It treated the calm metric as contradicting the matcher, answered `unknown`
with confidence 0.5 and proposed no action. Its second choice was correct.

- **Category:** misleading evidence plus LLM over-caution. The matcher, keyed on the OOMKilled signal,
  was right.
- **Fix, measured:** prompt v2 adds rule 9: weigh evidence by how direct it is; sampled metrics miss
  short spikes; a peak below the limit does not contradict an OOM kill. Replayed on the identical
  snapshot, v2 answers `memory_limit_too_low` → `increase_memory_limit`, and still diagnoses the
  bad-config incident correctly.
- **Caveat, stated plainly:** n = 1. The v2 answer came from the main Flash model, while the v1 answer
  came from Flash-Lite after a fallback, so prompt and model both changed. To claim v2 is better
  takes the full batch and several replays per snapshot.
- **Not yet fixed (collector):** the memory fact should say what it can't see. For example, "peak of
  30 s samples; sub-sample spikes are not visible". Fixing data the model reads is sturdier than a
  prompt rule.

### 2. Bad config: right answer, two different names

The live diagnosis said `config_invalid`. The replay said `bad_release` with `config_invalid` as the
alternative. Both are accepted by `expected.yaml`, and both lead to the same rollback. The scenario
is genuinely both: a release that carries an invalid setting. This is why `expected.yaml` lists
acceptable alternatives, and why the report separates "exact" from "correct".

### 3. Matcher vs LLM: where the LLM adds value

On these two incidents the deterministic matcher was right both times. The LLM added nothing to
accuracy and lost the OOM run. That supports the rules-first design (ADR 7): the matcher's answer is
the floor and the fallback. The LLM's job is the incidents the signatures don't cover, and the
explanation a human reads. Neither shows up in two well-known scenarios. In Phase 5, Gemini did
disagree with the matcher on a bad-config run, and Gemini was the one that was right. Two runs can't
settle the question; the full batch can.

## What this batch did not measure

- Noisy-neighbour (false incidents), prompt injection, bad image, dependency down and saturation:
  built, unit-tested and runnable, but not run live in this batch.
- Time to recovery after the agent's own action. That was measured live in Phase 5: an approved
  memory fix recovered in 118 s.
- The manual baseline (`make manual`): needs a human at the keyboard.

## CI gate

`.github/workflows/evals.yml` runs on every PR touching the agent. It replays the matcher, with no
LLM and no network, on the committed snapshots and fails if accuracy drops below the recorded 2/2.
LLM configurations are compared by hand with `make replay`, because they cost quota and are not
deterministic.
