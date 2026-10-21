"""Runs one investigation per new incident: collect evidence, match, diagnose, store.

Investigations run on a single background worker so the webhook answers
Alertmanager immediately, and so two incidents never hit the free-tier LLM at once.
"""

import json
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime

from app.collectors import Collector
from app.db import Database, iso
from app.llm import LLMProvider
from app.matcher import Signature, match
from app.metrics import DIAGNOSES, INVESTIGATION_SECONDS
from app.rca import RCAResult, diagnose

logger = logging.getLogger("kubesentinel.investigator")


class Investigator:
    def __init__(
        self,
        db: Database,
        collector: Collector,
        signatures: list[Signature],
        provider: LLMProvider | None,
        dependencies: dict[str, list[str]],
        delay_seconds: float,
    ) -> None:
        self.db = db
        self.collector = collector
        self.signatures = signatures
        self.provider = provider
        self.dependencies = dependencies
        self.delay_seconds = delay_seconds
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="investigate")

    def submit(self, incident_id: int, delay_seconds: float | None = None) -> Future:
        delay = self.delay_seconds if delay_seconds is None else delay_seconds
        return self._pool.submit(self._run_safely, incident_id, delay)

    def _run_safely(self, incident_id: int, delay: float) -> RCAResult | None:
        # Give related alerts and events a moment to arrive before looking.
        time.sleep(delay)
        try:
            return self.investigate(incident_id)
        except Exception:
            logger.exception(
                "investigation failed", extra={"fields": {"incident_id": incident_id}}
            )
            return None

    def investigate(self, incident_id: int) -> RCAResult:
        incident = self.db.get_incident(incident_id)
        if incident is None:
            raise ValueError(f"incident {incident_id} not found")
        started = time.perf_counter()
        first_alert_at = datetime.fromisoformat(incident["first_alert_at"])
        evidence = self.collector.collect(
            incident["namespace"], incident["service"], first_alert_at
        )
        collect_ms = int((time.perf_counter() - started) * 1000)

        candidates = match(evidence, self.signatures)
        dependencies = self.dependencies.get(incident["service"], [])
        result = diagnose(incident, evidence, candidates, self.provider, dependencies)
        d = result.diagnosis

        self.db.save_diagnosis(
            incident_id,
            {
                "incident_id": incident_id,
                "created_at": iso(datetime.now(UTC)),
                "source": result.source,
                "root_cause": d.root_cause,
                "confidence": d.confidence,
                "action_type": d.proposed_action.type,
                "action_target": d.proposed_action.target,
                "prompt_version": result.prompt_version,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "agrees_with_matcher": result.agrees_with_matcher,
                "collect_ms": collect_ms,
                "diagnosis_json": d.model_dump_json(),
                "candidates_json": json.dumps([c.model_dump() for c in candidates]),
                "signals_json": evidence.signals.model_dump_json(),
                "notes_json": json.dumps(
                    result.notes + [f"collector error: {e}" for e in evidence.errors]
                ),
            },
            [f.model_dump() for f in evidence.facts],
        )
        DIAGNOSES.labels(result.source, d.root_cause).inc()
        INVESTIGATION_SECONDS.observe(time.perf_counter() - started)
        logger.info(
            "incident diagnosed",
            extra={
                "fields": {
                    "incident_id": incident_id,
                    "root_cause": d.root_cause,
                    "confidence": d.confidence,
                    "action": f"{d.proposed_action.type} {d.proposed_action.target}",
                    "source": result.source,
                    "model": result.model,
                    "facts": len(evidence.facts),
                    "collect_ms": collect_ms,
                    "llm_ms": result.latency_ms,
                    "notes": result.notes,
                }
            },
        )
        return result
