# 8. Remediation through Git, behind a policy gate and human approval

Date: 2026-10-22
Status: Accepted

## Context

Diagnosis alone does not reduce time to recovery. The agent must also be able to fix things, without becoming the outage. The cluster is managed by Argo CD with self-heal: a direct change to a field that Git manages (replicas, limits, image) is reverted within seconds and leaves no record. Approvals must reach a human on a phone, but the cluster runs on a laptop with no public URL.

## Decision

- **Changes to Git-managed settings are commits to the GitOps repo**, made with a deploy key that belongs to the agent and only works on that one repo: raise a memory limit, add a replica, or `git revert` the suspect deploy. Argo CD applies them like any other deploy. Each commit names the incident, the action and the approver.
- **Direct cluster writes are limited to two:** deleting a pod in `demo`, and asking Argo CD to refresh or sync the `orders-api` or `redis` Application (named in RBAC, so the agent cannot touch its own).
- **Plan, then decide.** The planner turns a proposal into an exact change using live state (`128Mi -> 256Mi in apps/orders-api/deployment.yaml`). That text is what the human approves. The executor refuses if Git no longer matches the plan.
- **`policy.yaml` decides**, in code: `auto` for low-risk actions within rate limits and a confidence threshold, `approval` for medium risk, and `blocked` for anything else. A model that asks for a human gets one.
- **Approval over Telegram with long polling** (`getUpdates`): no inbound endpoint is exposed. A button carries a random one-time token, and only its hash is stored. The press must come from the configured chat before the token expires (15 minutes). The status change is atomic, so double taps are harmless. The same path is available as `POST /actions/{id}/approve` for scripts and evals.
- **Verify, then trip the breaker.** After a Git change, recovery counts only once Argo CD reports that exact commit as synced and the affected deployments are ready twice in a row. No recovery within 5 minutes pauses automatic actions for that service for an hour, and a follow-up tells the human.

## Alternatives considered

- **`kubectl patch` / `scale` directly:** fast, but self-heal reverts it, and it bypasses review and history.
- **Disabling self-heal during a fix:** turns the safety net off exactly when things are broken.
- **A fine-grained GitHub token for the API:** cannot be created programmatically, and imitating `git revert` file by file through the contents API is fragile. A deploy key scoped to one repo is the narrowest credential that works with real git.
- **Telegram webhooks:** would need a public HTTPS endpoint into a laptop cluster.

## Consequences

- A fix takes as long as an Argo CD sync (seconds, because the agent requests a refresh), not as long as a kubectl call. That is the price of an auditable, reversible change.
- Agent fixes stay in Git until someone reverts them. Evaluation runs must undo the agent's commits after each scenario.
- The agent image includes git and ssh (387 MB; no HIGH or CRITICAL findings in Trivy).
