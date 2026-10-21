import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import Settings
from app.correlator import Correlator
from app.db import Database
from app.logging_config import configure_logging
from app.metrics import ALERTS_RECEIVED, INCIDENT_EVENTS, INCIDENTS
from app.models import AlertmanagerPayload

logger = logging.getLogger("kubesentinel")

IncidentStatus = Literal["open", "resolved", "closed"]


def now_utc() -> datetime:
    return datetime.now(UTC)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="kubesentinel-agent")
    db = Database(settings.db_path)
    correlator = Correlator(db, settings.quiet_minutes_to_close)
    app.state.db = db

    def refresh_gauges() -> None:
        counts = db.count_by_status()
        for status in ("open", "resolved", "closed"):
            INCIDENTS.labels(status).set(counts.get(status, 0))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        db.ping()
        return {"status": "ready"}

    @app.get("/version")
    def version() -> dict[str, str]:
        return {"version": settings.version}

    @app.get("/metrics")
    def metrics() -> Response:
        correlator.close_stale(now_utc())
        refresh_gauges()
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/alerts")
    def receive_alerts(payload: AlertmanagerPayload) -> dict:
        """Alertmanager webhook. A 5xx makes Alertmanager retry, so only fail on our bugs."""
        for alert in payload.alerts:
            ALERTS_RECEIVED.labels(alert.status).inc()
        changes = correlator.ingest(payload, now_utc())
        for change in changes:
            INCIDENT_EVENTS.labels(change.event).inc()
            logger.info(
                "incident %s",
                change.event,
                extra={
                    "fields": {
                        "incident_id": change.incident_id,
                        "event": change.event,
                        "alerts": [a.name for a in payload.alerts],
                    }
                },
            )
        refresh_gauges()
        return {
            "alerts": len(payload.alerts),
            "incidents": [{"id": c.incident_id, "event": c.event} for c in changes],
        }

    @app.get("/incidents")
    def list_incidents(
        status: IncidentStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[dict]:
        correlator.close_stale(now_utc())
        return db.list_incidents(status, limit)

    @app.get("/incidents/{incident_id}")
    def get_incident(incident_id: int) -> dict:
        incident = db.get_incident(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return incident

    return app
