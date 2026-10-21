"""Deterministic signature matcher: evidence signals in, ranked candidate causes out."""

import operator
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from app.evidence import Evidence, Signals

SIGNATURES_FILE = Path(__file__).with_name("signatures.yaml")

Cause = Literal[
    "bad_release",
    "config_invalid",
    "image_pull_error",
    "memory_limit_too_low",
    "saturation",
    "dependency_down",
    "unknown",
]
Action = Literal[
    "rollback",
    "increase_memory_limit",
    "scale_up",
    "restart_dependency",
    "restart_pod",
    "none",
]

OPS = {
    "is": operator.eq,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


class Condition(BaseModel):
    signal: str
    op: str
    value: bool | float

    @model_validator(mode="before")
    @classmethod
    def from_yaml(cls, data: dict) -> dict:
        """{signal: x, gt: 1} -> {signal: x, op: gt, value: 1}"""
        data = dict(data)
        signal = data.pop("signal")
        if len(data) != 1 or next(iter(data)) not in OPS:
            raise ValueError(
                f"condition on {signal} needs exactly one of {sorted(OPS)}"
            )
        ((op, value),) = data.items()
        if signal not in Signals.model_fields:
            raise ValueError(f"unknown signal {signal}")
        return {"signal": signal, "op": op, "value": value}

    def holds(self, signals: Signals) -> bool:
        actual = getattr(signals, self.signal)
        if actual is None:
            return False
        return OPS[self.op](actual, self.value)


class Signature(BaseModel):
    name: str
    cause: Cause
    action: Action
    confidence: float = Field(ge=0, le=1)
    when: list[Condition] = Field(min_length=1)
    explain: str


class Candidate(BaseModel):
    signature: str
    cause: Cause
    action: Action
    confidence: float
    explain: str
    evidence: list[str]  # fact ids that support the conditions


def load_signatures(path: Path = SIGNATURES_FILE) -> list[Signature]:
    return [Signature.model_validate(s) for s in yaml.safe_load(path.read_text())]


def match(evidence: Evidence, signatures: list[Signature]) -> list[Candidate]:
    candidates = []
    for sig in signatures:
        if not all(c.holds(evidence.signals) for c in sig.when):
            continue
        # Cite the facts behind the positive conditions ("x is false" has no fact).
        cited = [
            evidence.support[c.signal]
            for c in sig.when
            if c.value is not False and c.signal in evidence.support
        ]
        candidates.append(
            Candidate(
                signature=sig.name,
                cause=sig.cause,
                action=sig.action,
                confidence=sig.confidence,
                explain=sig.explain,
                evidence=list(dict.fromkeys(cited)),
            )
        )
    return sorted(candidates, key=lambda c: c.confidence, reverse=True)
