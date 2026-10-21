"""What the collectors found: short facts with ids, plus typed signals derived from them.

Facts are what a human (or the LLM) reads and cites by id. Signals are what the
signature matcher reasons over. Every signal remembers the fact that supports
it, so a rule-based diagnosis can cite evidence exactly like the LLM must.
"""

from dataclasses import dataclass, field

from pydantic import BaseModel

from app.redact import redact

MAX_FACT_CHARS = 400


class Fact(BaseModel):
    id: str
    source: str  # pods | dependencies | events | deployment | logs | metrics | deploys
    text: str


class Signals(BaseModel):
    oom_killed: bool = False  # a container was OOMKilled recently
    crash_looping: bool = False
    image_pull_error: bool = False
    rollout_stuck: bool = False
    pods_not_ready: int = 0
    dependency_unavailable: bool = False
    connection_errors_in_logs: bool = False
    config_error_in_logs: bool = False
    error_ratio: float | None = None
    p95_seconds: float | None = None
    memory_peak_ratio: float | None = None  # peak working set / limit, last 15 min
    cpu_ratio: float | None = None  # current usage / limit
    restarts_15m: float | None = None
    # Minutes between the newest deploy and the first alert (deploys after it are ignored).
    minutes_since_deploy: float | None = None
    # That deploy, for a rollback: GitOps commit sha and its first line.
    suspect_revision: str | None = None
    suspect_message: str | None = None


@dataclass
class Evidence:
    facts: list[Fact] = field(default_factory=list)
    signals: Signals = field(default_factory=Signals)
    support: dict[str, str] = field(default_factory=dict)  # signal -> fact id
    errors: list[str] = field(default_factory=list)  # sources that could not be read

    def add(self, source: str, text: str) -> str:
        """Record one fact (redacted, truncated) and return its id."""
        fact_id = f"E{len(self.facts) + 1}"
        text = redact(" ".join(text.split()))
        if len(text) > MAX_FACT_CHARS:
            text = text[: MAX_FACT_CHARS - 3] + "..."
        self.facts.append(Fact(id=fact_id, source=source, text=text))
        return fact_id

    def signal(self, name: str, value, fact_id: str) -> None:
        setattr(self.signals, name, value)
        self.support.setdefault(name, fact_id)

    @property
    def fact_ids(self) -> set[str]:
        return {f.id for f in self.facts}

    def as_text(self) -> str:
        return "\n".join(f"{f.id} [{f.source}] {f.text}" for f in self.facts)
