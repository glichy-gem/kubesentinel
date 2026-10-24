"""From a diagnosis to a concrete, policy-checked action (design doc section 6.6).

The planner turns "increase_memory_limit on orders-api" into an exact change
("128Mi -> 256Mi in apps/orders-api/deployment.yaml") using the live cluster
state, so the human approves precisely what will run (dry run first). Then the
policy decides auto, approval or blocked, and the responder records it,
notifies, and hands approved work to the executor.
"""

import hashlib
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from app.clients import KubeClient
from app.db import Database, iso
from app.evidence import Signals
from app.policy import Decision, History, Policy, decide

logger = logging.getLogger("kubesentinel.remediation")

ARGO_APPS = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications"

QUANTITY = re.compile(r"^(\d+)(Mi|Gi|M|G)?$")
TO_MI = {
    "Mi": 1,
    "Gi": 1024,
    "M": 1000 / 1048.576,
    "G": 1000_000 / 1048.576,
    None: 1 / 1048576,
}


def memory_mi(quantity: str) -> float:
    match = QUANTITY.match(quantity.strip())
    if not match:
        raise ValueError(f"unsupported memory quantity {quantity!r}")
    return int(match.group(1)) * TO_MI[match.group(2)]


@dataclass(frozen=True)
class Plan:
    action: str
    target: str  # the deployment the action changes
    namespace: str
    via: str  # git | kubernetes | argocd | none
    description: str  # the exact change, shown before anything runs
    params: dict = field(default_factory=dict)
    error: str | None = None  # why it cannot be planned; the policy then blocks it


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Planner:
    def __init__(self, kube: KubeClient, policy: Policy, gitops_repo: str) -> None:
        self.kube = kube
        self.policy = policy
        self.gitops_repo = gitops_repo

    def plan(self, action: str, target: str, namespace: str, signals: Signals) -> Plan:
        builders: dict[str, Callable[[str, str, Signals], Plan]] = {
            "increase_memory_limit": self._memory,
            "scale_up": self._scale,
            "rollback": self._rollback,
            "restart_pod": self._restart_pod,
            "restart_dependency": self._restart_dependency,
        }
        if action == "none":
            return Plan("none", target, namespace, "none", "no action")
        if action not in builders:
            return Plan(
                action,
                target,
                namespace,
                "none",
                action,
                error=f"no planner for {action}",
            )
        try:
            return builders[action](target, namespace, signals)
        except Exception as exc:  # noqa: BLE001 - a plan that cannot be built is blocked, not a crash
            return Plan(
                action, target, namespace, "none", action, error=f"cannot plan: {exc}"
            )

    def _deployment(self, namespace: str, name: str) -> dict:
        return self.kube.get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")

    def _memory(self, target: str, namespace: str, signals: Signals) -> Plan:
        rule = self.policy.rule("increase_memory_limit")
        container = self._deployment(namespace, target)["spec"]["template"]["spec"][
            "containers"
        ][0]
        current = container.get("resources", {}).get("limits", {}).get("memory")
        if not current:
            raise ValueError(f"{target} has no memory limit to raise")
        new_mi = int(min(memory_mi(current) * rule.factor, rule.max_memory_mi))
        if new_mi <= memory_mi(current):
            return Plan(
                "increase_memory_limit",
                target,
                namespace,
                "git",
                "",
                error=f"memory limit {current} is already at the policy maximum {rule.max_memory_mi}Mi",
            )
        path = f"apps/{target}/deployment.yaml"
        return Plan(
            "increase_memory_limit",
            target,
            namespace,
            "git",
            f"commit to {self.gitops_repo}: {path} resources.limits.memory {current} -> {new_mi}Mi",
            {
                "path": path,
                "container": container["name"],
                "from": current,
                "to": f"{new_mi}Mi",
            },
        )

    def _scale(self, target: str, namespace: str, signals: Signals) -> Plan:
        rule = self.policy.rule("scale_up")
        current = self._deployment(namespace, target)["spec"].get("replicas", 1)
        new = current + rule.step
        if new > rule.max_replicas:
            return Plan(
                "scale_up",
                target,
                namespace,
                "git",
                "",
                error=f"{target} has {current} replicas; the policy maximum is {rule.max_replicas}",
            )
        path = f"apps/{target}/deployment.yaml"
        return Plan(
            "scale_up",
            target,
            namespace,
            "git",
            f"commit to {self.gitops_repo}: {path} replicas {current} -> {new}",
            {"path": path, "from": current, "to": new},
        )

    def _rollback(self, target: str, namespace: str, signals: Signals) -> Plan:
        rule = self.policy.rule("rollback")
        revision, minutes = signals.suspect_revision, signals.minutes_since_deploy
        if not revision or minutes is None:
            return Plan(
                "rollback",
                target,
                namespace,
                "git",
                "",
                error="no recent deploy to roll back",
            )
        if minutes > rule.max_minutes_before_symptoms:
            return Plan(
                "rollback",
                target,
                namespace,
                "git",
                "",
                error=f"the last deploy was {minutes:.0f} min before the symptoms; too old to blame",
            )
        # Only revert what is still running. Found live: a stale diagnosis proposed
        # reverting a commit that had already been reverted half an hour earlier.
        app = self.kube.get(f"{ARGO_APPS}/{target}")
        history = app.get("status", {}).get("history", [])
        latest = max(history, key=lambda h: h.get("deployedAt", ""), default={})
        if latest.get("revision") != revision:
            return Plan(
                "rollback",
                target,
                namespace,
                "git",
                "",
                error=f"{revision[:7]} is no longer the latest deploy of {target}"
                f" (now {latest.get('revision', '')[:7] or 'unknown'}); nothing to roll back",
            )
        return Plan(
            "rollback",
            target,
            namespace,
            "git",
            f"git revert {revision[:7]} in {self.gitops_repo}"
            f" ('{signals.suspect_message or ''}', deployed {minutes:.1f} min before the symptoms)",
            {"revision": revision, "message": signals.suspect_message or ""},
        )

    def _restart_pod(self, target: str, namespace: str, signals: Signals) -> Plan:
        pods = self.kube.get(
            f"/api/v1/namespaces/{namespace}/pods", labelSelector=f"app={target}"
        )["items"]

        def badness(pod: dict) -> tuple:
            statuses = pod.get("status", {}).get("containerStatuses", [{}])
            ready = all(s.get("ready") for s in statuses)
            return (not ready, sum(s.get("restartCount", 0) for s in statuses))

        if not pods:
            raise ValueError(f"no pods for {target}")
        worst = max(pods, key=badness)["metadata"]["name"]
        return Plan(
            "restart_pod",
            target,
            namespace,
            "kubernetes",
            f"delete pod {worst} in {namespace} (its Deployment starts a fresh one)",
            {"pod": worst},
        )

    def _restart_dependency(
        self, target: str, namespace: str, signals: Signals
    ) -> Plan:
        dep = self._deployment(namespace, target)
        desired = dep["spec"].get("replicas", 1)
        if desired == 0:
            # Scaled down outside Git: put back what Git says, through Argo CD.
            return Plan(
                "restart_dependency",
                target,
                namespace,
                "argocd",
                f"sync Argo CD application {target} from Git (live replicas 0, Git is the source of truth)",
                {"application": target},
            )
        return Plan(
            "restart_dependency",
            target,
            namespace,
            "kubernetes",
            f"delete the pods of {target} in {namespace} (its Deployment starts fresh ones)",
            {"selector": f"app={target}"},
        )


class Responder:
    """Plan, decide, record, notify; hand approved or auto actions to the executor."""

    def __init__(
        self,
        db: Database,
        planner: Planner,
        policy: Policy,
        notifier,  # app.telegram.Notifier or None
        execute: Callable[[int], None],  # queue an action id for execution
        dry_run: bool = False,  # evals: record the decision, change nothing
    ) -> None:
        self.db = db
        self.planner = planner
        self.policy = policy
        self.notifier = notifier
        self.execute = execute
        self.dry_run = dry_run

    def respond(
        self, incident: dict, diagnosis_id: int, diagnosis, signals: Signals, evidence
    ) -> dict:
        now = datetime.now(UTC)
        proposed = diagnosis.proposed_action
        plan = self.planner.plan(
            proposed.type, proposed.target, incident["namespace"], signals
        )
        history = History(
            actions_this_incident=self.db.count_actions(
                plan.action, incident_id=incident["id"]
            ),
            actions_last_hour_for_service=self.db.count_actions(
                plan.action, service=incident["service"], since=now - timedelta(hours=1)
            ),
            automation_paused=self.db.automation_paused(incident["service"], now),
        )
        decision = decide(
            self.policy,
            plan.action,
            plan.namespace,
            diagnosis.confidence,
            history,
            plan.error,
        )
        # A model that asked for a human gets one, even for a low-risk action.
        if decision.mode == "auto" and diagnosis.needs_human:
            decision = Decision(
                "approval", decision.risk, "the diagnosis asked for a human"
            )

        self.db.supersede_pending(incident["id"])
        # The mode is still decided and recorded, so evals measure the real policy outcome.
        token = (
            secrets.token_urlsafe(12)
            if decision.mode == "approval" and not self.dry_run
            else None
        )
        status = {
            "auto": "approved",
            "approval": "proposed",
            "blocked": "blocked",
            "none": "none",
        }[decision.mode]
        if self.dry_run and status in ("approved", "proposed"):
            status = "dry_run"
        action_id = self.db.insert_action(
            {
                "incident_id": incident["id"],
                "diagnosis_id": diagnosis_id,
                "created_at": iso(now),
                "service": incident["service"],
                "namespace": plan.namespace,
                "type": plan.action,
                "target": plan.target,
                "via": plan.via,
                "params_json": json.dumps(plan.params),
                "description": plan.description or plan.error or plan.action,
                "risk": decision.risk,
                "mode": decision.mode,
                "reason": decision.reason,
                "status": status,
                "token_hash": token_hash(token) if token else None,
                "expires_at": iso(
                    now + timedelta(minutes=self.policy.approval_ttl_minutes)
                )
                if token
                else None,
                "decided_at": iso(now) if status == "approved" else None,
                "decided_by": "policy (auto)" if status == "approved" else None,
            }
        )
        action = self.db.get_action(action_id)
        logger.info(
            "action %s",
            decision.mode,
            extra={
                "fields": {
                    "incident_id": incident["id"],
                    "action_id": action_id,
                    "plan": asdict(plan),
                    "reason": decision.reason,
                }
            },
        )
        if self.dry_run:
            return action
        if self.notifier is not None:
            self.notifier.incident_card(
                self.db.get_incident(incident["id"]), action, token
            )
        if decision.mode == "auto":
            self.execute(action_id)
        return action
