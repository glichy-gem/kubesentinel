# 6. Failure scenarios as data, delivered the way real failures arrive

Date: 2026-10-21
Status: Accepted

## Context

The agent (Phase 5) and its evaluation (Phase 6) need failures that can be injected on demand, repeated many times, and scored against a known answer. Hand-run `kubectl` commands are not repeatable. Chaos tools such as Chaos Mesh or Litmus need several hundred MiB and their own CRDs, and the WSL2 VM has about 1 GiB of headroom.

Bad releases in real clusters do not arrive as `kubectl edit`. They arrive as a merged commit that a GitOps controller rolls out. A scenario that skips that path would hide the most useful evidence an agent can find: "this started right after commit X".

## Decision

- Each scenario is a folder with `inject.sh`, `revert.sh` and `expected.yaml`. The YAML is the answer key: the alerts that must fire, the alerts that may fire, the root cause, the expected action and its approval mode.
- `chaos/run_scenario.py` runs one scenario end to end: it waits for a healthy baseline with no firing alerts, injects the fault, watches the alerts that reach the Alertmanager webhook, reverts in a `finally` block, waits for recovery, and writes a JSON result for Phase 6.
- **Faults that are releases go through Git.** `bad-config` and `bad-image` push a commit with a realistic message to `kubesentinel-gitops`, and revert it with `git revert`. Argo CD does the rollout.
- **Faults that are not releases** (OOM, saturation, a dependency outage) use the app's chaos endpoints, a k6 Job, or `kubectl scale`. When Argo CD would undo the fault, its auto-sync is paused for that one application and resumed on revert.
- Two alert rules were added for rollouts: **DeploymentRolloutStuck** and **PodImagePullBackOff**. With two replicas and a rolling update, a bad release leaves the old pods serving, so `DeploymentReplicasMismatch` never fires.
- `expected.yaml` is loaded strictly: unknown or missing keys fail, and a unit test checks that every alert a scenario names exists in the rule file.

## Alternatives considered

- **Chaos Mesh / Litmus**: realistic and well known, but too heavy for this machine. They also cannot express "a bad commit was merged".
- **Mutating the cluster directly for every scenario**: simpler, but Argo CD self-heal reverts some changes and silently ignores others (see ADR 5). The scenarios would not exercise the delivery path.
- **Reading alerts from the Prometheus API instead of the webhook**: easier, but it skips Alertmanager grouping and routing, which is exactly what the agent will receive.

## Consequences

- Release scenarios leave two commits in the GitOps history (the fault and its revert). That is accepted: it is what a real incident looks like.
- A scenario cannot start until the previous one's alerts have resolved. Some rules look back 10 minutes, so back-to-back runs take longer.
- Detection time includes Prometheus scrape and evaluation intervals, the rule's `for:` duration and Alertmanager `group_wait`. The project goal of 60 s from the first symptom therefore needs tuning in Phase 6.
