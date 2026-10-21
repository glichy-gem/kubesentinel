"""The policy gate: given a concrete plan, decide auto, approval or blocked.

Deliberately boring code. Every rule here is a guardrail against the LLM, a bad
signature or a flapping incident, so each one is explicit and unit tested.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

POLICY_FILE = Path(__file__).with_name("policy.yaml")

Mode = Literal["auto", "approval", "blocked", "none"]


class ActionRule(BaseModel):
    risk: Literal["none", "low", "medium"]
    mode: Literal["auto", "approval", "none"]
    max_per_incident: int | None = None
    max_per_hour_per_service: int | None = None
    step: int = 1
    max_replicas: int | None = None
    factor: float = 2
    max_memory_mi: int | None = None
    max_minutes_before_symptoms: float | None = None


class CircuitBreaker(BaseModel):
    verify_minutes: float = 5
    pause_minutes: float = 60


class Policy(BaseModel):
    namespaces: list[str] = Field(min_length=1)
    min_auto_confidence: float = Field(ge=0, le=1)
    approval_ttl_minutes: float = 15
    circuit_breaker: CircuitBreaker = CircuitBreaker()
    actions: dict[str, ActionRule]

    def rule(self, action: str) -> ActionRule | None:
        return self.actions.get(action)


def load_policy(path: Path = POLICY_FILE) -> Policy:
    return Policy.model_validate(yaml.safe_load(path.read_text()))


@dataclass(frozen=True)
class History:
    """What has already happened, from the database."""

    actions_this_incident: int  # executed or approved actions of the same type
    actions_last_hour_for_service: int  # executed actions of the same type
    automation_paused: bool  # the circuit breaker tripped for this service


@dataclass(frozen=True)
class Decision:
    mode: Mode
    risk: str
    reason: str


def decide(
    policy: Policy,
    action: str,
    namespace: str,
    confidence: float,
    history: History,
    plan_error: str | None = None,
) -> Decision:
    rule = policy.rule(action)
    if rule is None:
        return Decision("blocked", "high", f"action {action!r} is not in the policy")
    if rule.mode == "none":
        return Decision("none", rule.risk, "no action proposed")
    if namespace not in policy.namespaces:
        return Decision("blocked", rule.risk, f"namespace {namespace!r} is off limits")
    if plan_error:
        return Decision("blocked", rule.risk, plan_error)
    if (
        rule.max_per_incident is not None
        and history.actions_this_incident >= rule.max_per_incident
    ):
        return Decision("blocked", rule.risk, f"already ran {action} for this incident")
    if (
        rule.max_per_hour_per_service is not None
        and history.actions_last_hour_for_service >= rule.max_per_hour_per_service
    ):
        return Decision(
            "blocked",
            rule.risk,
            f"{action} ran {history.actions_last_hour_for_service} times in the last hour",
        )

    if rule.mode == "approval":
        return Decision("approval", rule.risk, f"{rule.risk} risk: a human approves")
    # Auto, unless something says a human should look first.
    if history.automation_paused:
        return Decision(
            "approval", rule.risk, "automation paused after a fix that did not help"
        )
    if confidence < policy.min_auto_confidence:
        return Decision(
            "approval",
            rule.risk,
            f"confidence {confidence:.2f} is below {policy.min_auto_confidence}",
        )
    return Decision("auto", rule.risk, f"{rule.risk} risk, within limits")
