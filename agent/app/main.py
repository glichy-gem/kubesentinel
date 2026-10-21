import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from app.clients import (
    AlertmanagerClient,
    GitHubClient,
    KubeClient,
    LokiClient,
    PrometheusClient,
)
from app.collectors import Collector
from app.config import Settings
from app.correlator import Correlator
from app.db import Database
from app.executor import Executor, GitOpsRepo
from app.investigator import Investigator
from app.llm import GeminiProvider
from app.logging_config import configure_logging
from app.matcher import load_signatures
from app.metrics import ALERTS_RECEIVED, INCIDENT_EVENTS, INCIDENTS
from app.models import AlertmanagerPayload
from app.policy import load_policy
from app.reconciler import Reconciler
from app.remediation import Planner, Responder
from app.telegram import Approvals, Notifier, Poller, TelegramClient

logger = logging.getLogger("kubesentinel")

IncidentStatus = Literal["open", "resolved", "closed"]


class ApprovalIn(BaseModel):
    token: str = Field(min_length=8, max_length=64)
    by: str = Field(default="api", max_length=64)


def now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass
class Remediation:
    responder: Responder
    executor: Executor
    approvals: Approvals
    poller: Poller | None


def build_remediation(
    settings: Settings, db: Database, kube: KubeClient
) -> Remediation:
    policy = load_policy()
    notifier = client = None
    if settings.telegram_bot_token and settings.telegram_chat_id:
        client = TelegramClient(settings.telegram_bot_token.get_secret_value())
        notifier = Notifier(client, settings.telegram_chat_id, db)
    gitops = None
    key_dir = Path(settings.git_key_dir)
    if (key_dir / "id_ed25519").exists():
        gitops = GitOpsRepo(
            settings.gitops_ssh_url,
            key_dir,
            Path(settings.workdir),
            "KubeSentinel agent",
        )
    executor = Executor(db, kube, gitops, policy, notifier)
    approvals = Approvals(db, executor.submit, notifier)
    responder = Responder(
        db,
        Planner(kube, policy, settings.gitops_repo),
        policy,
        notifier,
        executor.submit,
    )
    poller = Poller(client, settings.telegram_chat_id, approvals) if client else None
    return Remediation(responder, executor, approvals, poller)


def build_investigator(
    settings: Settings, db: Database, kube: KubeClient, responder: Responder | None
) -> Investigator:
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
        kube,
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
        responder,
    )


def create_app(
    settings: Settings | None = None,
    investigator: Investigator | None = None,
    remediation: Remediation | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    db = Database(settings.db_path)
    correlator = Correlator(db, settings.quiet_minutes_to_close)
    if investigator is None and settings.investigate:
        kube = KubeClient(settings.k8s_url)
        if remediation is None and settings.remediate:
            remediation = build_remediation(settings, db, kube)
        responder = remediation.responder if remediation else None
        investigator = build_investigator(settings, db, kube, responder)

    def refresh_gauges() -> None:
        counts = db.count_by_status()
        for status in ("open", "resolved", "closed"):
            INCIDENTS.labels(status).set(counts.get(status, 0))

    reconciler = (
        Reconciler(
            AlertmanagerClient(settings.alertmanager_url),
            correlator,
            settings.reconcile_seconds,
            refresh_gauges,
        )
        if settings.alertmanager_url
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        background = [t for t in (reconciler, remediation and remediation.poller) if t]
        for thread in background:
            thread.start()
        yield
        for thread in background:
            thread.stop()

    app = FastAPI(title="kubesentinel-agent", lifespan=lifespan)
    app.state.db = db
    app.state.investigator = investigator
    app.state.remediation = remediation
    logger.info(
        "agent started",
        extra={
            "fields": {
                "version": settings.version,
                "investigate": investigator is not None,
                "llm": settings.gemini_models if settings.gemini_api_key else None,
                "remediate": remediation is not None,
                "telegram": bool(remediation and remediation.poller),
                "gitops_write": bool(remediation and remediation.executor.gitops),
                "reconcile": reconciler is not None,
            }
        },
    )

    def should_investigate(change) -> bool:
        """New incident, flapping, or a new kind of symptom; or a recurrence after a while.

        The last case covers an incident that stays open across separate failures:
        its diagnosis is stale, so a fresh firing of a known alert gets a fresh look.
        """
        if change.needs_investigation:
            return True
        if not change.recurrences or change.event == "resolved":
            return False
        incident = db.get_incident(change.incident_id)
        last = incident and incident.get("diagnosed_at")
        if not last:
            return True
        age = now_utc() - datetime.fromisoformat(last)
        return age.total_seconds() > settings.reinvestigate_after_minutes * 60

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
            if investigator is not None and should_investigate(change):
                investigator.submit(change.incident_id)
            logger.info(
                "incident %s",
                change.event,
                extra={
                    "fields": {
                        "incident_id": change.incident_id,
                        "event": change.event,
                        "new_symptoms": list(change.new_symptoms),
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

    @app.get("/actions")
    def list_actions(
        incident_id: int | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[dict]:
        return db.list_actions(incident_id, limit)

    @app.post("/actions/{action_id}/{decision}")
    def decide_action(
        action_id: int, decision: Literal["approve", "reject"], body: ApprovalIn
    ) -> dict:
        """The same approval path as the Telegram buttons, for scripts and evals."""
        if remediation is None:
            raise HTTPException(status_code=503, detail="remediation is disabled")
        message = remediation.approvals.decide(
            action_id, body.token, decision == "approve", body.by
        )
        if message.startswith("Not applied"):
            raise HTTPException(status_code=409, detail=message)
        return {"action_id": action_id, "result": message}

    return app
