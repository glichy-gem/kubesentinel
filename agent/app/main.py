import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.clients import GitHubClient, KubeClient, LokiClient, PrometheusClient
from app.collectors import Collector
from app.config import Settings
from app.correlator import Correlator
from app.db import Database
from app.investigator import Investigator
from app.llm import GeminiProvider
from app.logging_config import configure_logging
from app.matcher import load_signatures
from app.metrics import ALERTS_RECEIVED, INCIDENT_EVENTS, INCIDENTS
from app.models import AlertmanagerPayload

logger = logging.getLogger("kubesentinel")

IncidentStatus = Literal["open", "resolved", "closed"]


def now_utc() -> datetime:
    return datetime.now(UTC)


def build_investigator(settings: Settings, db: Database) -> Investigator:
    provider = (
        GeminiProvider(
            settings.gemini_api_key.get_secret_value(),
            settings.gemini_models,
            settings.gemini_thinking_level,
        )
        if settings.gemini_api_key
        else None
    )
    collector = Collector(
        KubeClient(settings.k8s_url),
        PrometheusClient(settings.prometheus_url),
        LokiClient(settings.loki_url),
        GitHubClient(),
        settings.gitops_repo,
        settings.dependencies,
    )
    return Investigator(
        db,
        collector,
        load_signatures(),
        provider,
        settings.dependencies,
        settings.investigate_delay_seconds,
    )


def create_app(
    settings: Settings | None = None, investigator: Investigator | None = None
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="kubesentinel-agent")
    db = Database(settings.db_path)
    correlator = Correlator(db, settings.quiet_minutes_to_close)
    if investigator is None and settings.investigate:
        investigator = build_investigator(settings, db)
    app.state.db = db
    app.state.investigator = investigator
    logger.info(
        "agent started",
        extra={
            "fields": {
                "version": settings.version,
                "investigate": investigator is not None,
                "llm": settings.gemini_models if settings.gemini_api_key else None,
            }
        },
    )

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
            if change.event == "opened" and investigator is not None:
                investigator.submit(change.incident_id)
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

    @app.post("/incidents/{incident_id}/investigate", status_code=202)
    def reinvestigate(incident_id: int) -> dict:
        """Run the investigation again now, e.g. after a new prompt version."""
        if investigator is None:
            raise HTTPException(status_code=503, detail="investigation is disabled")
        if db.get_incident(incident_id) is None:
            raise HTTPException(status_code=404, detail="incident not found")
        investigator.submit(incident_id, delay_seconds=0)
        return {"incident_id": incident_id, "status": "queued"}

    return app
