"""Keeps stored alert state honest by comparing it with Alertmanager every minute."""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from app.clients import AlertmanagerClient
from app.correlator import Correlator
from app.db import iso

logger = logging.getLogger("kubesentinel.reconciler")


class Reconciler(threading.Thread):
    def __init__(
        self,
        alertmanager: AlertmanagerClient,
        correlator: Correlator,
        interval_seconds: float,
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__(name="reconciler", daemon=True)
        self.alertmanager = alertmanager
        self.correlator = correlator
        self.interval = interval_seconds
        self.on_change = on_change
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> list[int]:
        alerts = self.alertmanager.active_alerts()
        active = {
            (
                a["fingerprint"],
                iso(datetime.fromisoformat(a["startsAt"])),
            )
            for a in alerts
        }
        resolved = self.correlator.reconcile(active, datetime.now(UTC))
        if resolved:
            logger.info(
                "resolved incidents whose alerts Alertmanager no longer reports",
                extra={"fields": {"incidents": resolved}},
            )
            self.on_change()
        return resolved

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - never resolve anything on a failed read
                logger.warning(
                    "reconcile failed", extra={"fields": {"error": str(exc)}}
                )
