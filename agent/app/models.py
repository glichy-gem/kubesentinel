"""The Alertmanager webhook payload (version 4), validated at the door.

Only the fields the agent uses are declared; anything else Alertmanager sends is
ignored, so a newer Alertmanager does not break intake.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    # Alertmanager's stable id for one alert (its label set). Repeat
    # notifications of the same alert carry the same fingerprint.
    fingerprint: str = Field(min_length=1)

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "unknown")

    @property
    def namespace(self) -> str:
        return self.labels.get("namespace", "cluster")

    @property
    def service(self) -> str:
        # Every KubeSentinel rule sets a service label; fall back for anything else.
        return (
            self.labels.get("service")
            or self.labels.get("deployment")
            or self.labels.get("pod")
            or "unknown"
        )

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "info")


class AlertmanagerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["firing", "resolved"]
    receiver: str = ""
    alerts: list[AlertmanagerAlert]
