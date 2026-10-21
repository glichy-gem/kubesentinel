from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.correlator import Correlator
from app.db import Database
from app.main import create_app
from app.models import AlertmanagerPayload

T0 = datetime(2026, 10, 21, 18, 0, 0, tzinfo=UTC)


def am_alert(
    name: str = "PodCrashLooping",
    *,
    status: str = "firing",
    service: str = "orders-api",
    namespace: str = "demo",
    severity: str = "critical",
    fingerprint: str | None = None,
    starts_at: datetime = T0,
    **labels: str,
) -> dict:
    """One alert exactly as Alertmanager's webhook sends it."""
    return {
        "status": status,
        "labels": {
            "alertname": name,
            "namespace": namespace,
            "service": service,
            "severity": severity,
            **labels,
        },
        "annotations": {"summary": f"{service} {name}"},
        "startsAt": starts_at.isoformat().replace("+00:00", "Z"),
        "endsAt": "0001-01-01T00:00:00Z"
        if status == "firing"
        else (starts_at + timedelta(minutes=5)).isoformat(),
        "generatorURL": "http://prometheus/graph",
        # Alertmanager hashes the whole label set; namespace and service are enough here.
        "fingerprint": fingerprint or f"fp-{name}-{namespace}-{service}",
    }


def am_payload(*alerts: dict) -> dict:
    return {
        "version": "4",
        "groupKey": '{}:{namespace="demo", service="orders-api"}',
        "status": "firing"
        if any(a["status"] == "firing" for a in alerts)
        else "resolved",
        "receiver": "kubesentinel",
        "groupLabels": {"namespace": "demo", "service": "orders-api"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager",
        "truncatedAlerts": 0,
        "alerts": list(alerts),
    }


def parse(*alerts: dict) -> AlertmanagerPayload:
    return AlertmanagerPayload.model_validate(am_payload(*alerts))


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def correlator(db: Database) -> Correlator:
    return Correlator(db, quiet_minutes=10)


@pytest.fixture
def client(tmp_path) -> TestClient:
    settings = Settings(db_path=str(tmp_path / "api.db"), investigate=False)
    return TestClient(create_app(settings))
