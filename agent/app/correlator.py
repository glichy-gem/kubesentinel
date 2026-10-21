"""Turn a stream of alert notifications into a small number of incidents.

Rules (design doc section 6.2):
- Repeat notifications of the same alert (same fingerprint and startsAt) update
  that alert; they never create new work.
- Alerts with the same namespace and service join the incident for that key
  while it is open.
- A resolved incident reopens only if one of its own alerts fires again within
  the quiet period (flapping). A different alert after resolution is a new
  failure and opens a new incident.
- An incident resolves when none of its alerts is firing, and closes after the
  quiet period with no new alerts.

Each change reports the alert names that are new to the incident, so the
investigator can look again when the picture changes.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db import SEVERITY_RANK, Database, iso
from app.models import AlertmanagerAlert, AlertmanagerPayload


@dataclass(frozen=True)
class IncidentChange:
    incident_id: int
    event: str  # opened | updated | reopened | resolved
    new_symptoms: tuple[str, ...] = ()  # alert names this incident had not seen before
    recurrences: int = 0  # new firings of alert names it had already seen

    @property
    def needs_investigation(self) -> bool:
        if self.event == "resolved":
            return False
        return self.event in ("opened", "reopened") or bool(self.new_symptoms)


class Correlator:
    def __init__(self, db: Database, quiet_minutes: int) -> None:
        self.db = db
        self.quiet = timedelta(minutes=quiet_minutes)

    def ingest(
        self, payload: AlertmanagerPayload, now: datetime
    ) -> list[IncidentChange]:
        """Store one webhook payload and return what changed, one entry per incident."""
        changes: dict[int, str] = {}
        symptoms: dict[int, list[str]] = {}
        recurrences: dict[int, int] = {}
        with self.db.transaction() as conn:
            self._close_stale(conn, now)
            for alert in payload.alerts:
                incident_id, event, new_symptom, new_row = self._ingest_alert(
                    conn, alert, now
                )
                # "opened" and "reopened" are more informative than "updated".
                if changes.get(incident_id) not in ("opened", "reopened"):
                    changes[incident_id] = event
                found = symptoms.setdefault(incident_id, [])
                if new_symptom and event != "opened" and new_symptom not in found:
                    found.append(new_symptom)
                if new_row and not new_symptom and alert.status == "firing":
                    recurrences[incident_id] = recurrences.get(incident_id, 0) + 1
            for incident_id in changes:
                if self._resolve_if_quiet(conn, incident_id, now):
                    changes[incident_id] = "resolved"
        return [
            IncidentChange(i, e, tuple(symptoms.get(i, ())), recurrences.get(i, 0))
            for i, e in changes.items()
        ]

    def close_stale(self, now: datetime) -> int:
        with self.db.transaction() as conn:
            return self._close_stale(conn, now)

    def reconcile(
        self,
        active: set[tuple[str, str]],
        now: datetime,
        grace: timedelta = timedelta(minutes=2),
    ) -> list[int]:
        """Resolve stored alerts that Alertmanager no longer reports as firing.

        The webhook alone cannot be trusted: a "resolved" notification sent while
        the agent was restarting is lost, and its incident would stay open forever
        and swallow every later alert for that service (found live).
        `active` holds (fingerprint, iso(startsAt)) of Alertmanager's active alerts.
        Alerts received within `grace` are left alone to avoid racing the webhook.
        """
        resolved_incidents = []
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, incident_id, fingerprint, starts_at FROM alerts"
                " WHERE status = 'firing' AND received_at < ?",
                (iso(now - grace),),
            ).fetchall()
            stale = [
                r for r in rows if (r["fingerprint"], r["starts_at"]) not in active
            ]
            for row in stale:
                conn.execute(
                    "UPDATE alerts SET status = 'resolved', ends_at = ?, updated_at = ? WHERE id = ?",
                    (iso(now), iso(now), row["id"]),
                )
            for incident_id in sorted({r["incident_id"] for r in stale}):
                if self._resolve_if_quiet(conn, incident_id, now):
                    resolved_incidents.append(incident_id)
            self._close_stale(conn, now)
        return resolved_incidents

    # ------------------------------------------------------------- internals

    def _ingest_alert(
        self, conn: sqlite3.Connection, alert: AlertmanagerAlert, now: datetime
    ) -> tuple[int, str, str | None, bool]:
        """Store one alert.

        Returns its incident, what happened, its name if the incident had not seen
        it before, and whether this is a new occurrence (not a repeat notification).
        """
        starts_at = iso(alert.starts_at)
        new_symptom = None
        ends_at = (
            iso(alert.ends_at) if alert.status == "resolved" and alert.ends_at else None
        )
        existing = conn.execute(
            "SELECT id, incident_id FROM alerts WHERE fingerprint = ? AND starts_at = ?",
            (alert.fingerprint, starts_at),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE alerts SET status = ?, ends_at = ?, updated_at = ?,"
                " notifications = notifications + 1 WHERE id = ?",
                (alert.status, ends_at, iso(now), existing["id"]),
            )
            incident_id = existing["incident_id"]
            event = "updated"
        else:
            incident_id, event = self._incident_for(conn, alert, now)
            seen = conn.execute(
                "SELECT 1 FROM alerts WHERE incident_id = ? AND name = ?",
                (incident_id, alert.name),
            ).fetchone()
            if not seen:
                new_symptom = alert.name
            conn.execute(
                "INSERT INTO alerts (incident_id, fingerprint, starts_at, name, severity,"
                " status, labels, annotations, received_at, updated_at, ends_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    incident_id,
                    alert.fingerprint,
                    starts_at,
                    alert.name,
                    alert.severity,
                    alert.status,
                    json.dumps(alert.labels, sort_keys=True),
                    json.dumps(alert.annotations, sort_keys=True),
                    iso(now),
                    iso(now),
                    ends_at,
                ),
            )
            conn.execute(
                "UPDATE incidents SET raw_alert_count = raw_alert_count + 1,"
                " first_alert_at = MIN(first_alert_at, ?) WHERE id = ?",
                (starts_at, incident_id),
            )
            self._raise_severity(conn, incident_id, alert.severity)

        conn.execute(
            "UPDATE incidents SET notification_count = notification_count + 1,"
            " updated_at = ? WHERE id = ?",
            (iso(now), incident_id),
        )
        if alert.status == "firing" and event != "opened":
            reopened = conn.execute(
                "UPDATE incidents SET status = 'open', resolved_at = NULL"
                " WHERE id = ? AND status = 'resolved'",
                (incident_id,),
            ).rowcount
            if reopened:
                event = "reopened"
        return incident_id, event, new_symptom, existing is None

    def _incident_for(
        self, conn: sqlite3.Connection, alert: AlertmanagerAlert, now: datetime
    ) -> tuple[int, str]:
        """The incident a new alert belongs to, creating one if needed."""
        row = conn.execute(
            "SELECT id FROM incidents WHERE namespace = ? AND service = ?"
            " AND status = 'open' ORDER BY id DESC LIMIT 1",
            (alert.namespace, alert.service),
        ).fetchone()
        if row:
            return row["id"], "updated"
        # Flapping: the same alert returning shortly after its incident resolved.
        row = conn.execute(
            "SELECT i.id FROM incidents i JOIN alerts a ON a.incident_id = i.id"
            " WHERE i.namespace = ? AND i.service = ? AND i.status = 'resolved'"
            " AND i.resolved_at > ? AND a.name = ? ORDER BY i.id DESC LIMIT 1",
            (alert.namespace, alert.service, iso(now - self.quiet), alert.name),
        ).fetchone()
        if row:
            return row["id"], "updated"
        title = alert.annotations.get("summary") or f"{alert.name} on {alert.service}"
        cursor = conn.execute(
            "INSERT INTO incidents (namespace, service, status, severity, title,"
            " opened_at, first_alert_at, updated_at)"
            " VALUES (?, ?, 'open', ?, ?, ?, ?, ?)",
            (
                alert.namespace,
                alert.service,
                alert.severity,
                title,
                iso(now),
                iso(alert.starts_at),
                iso(now),
            ),
        )
        return cursor.lastrowid, "opened"

    @staticmethod
    def _raise_severity(
        conn: sqlite3.Connection, incident_id: int, severity: str
    ) -> None:
        current = conn.execute(
            "SELECT severity FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()["severity"]
        if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(current, 0):
            conn.execute(
                "UPDATE incidents SET severity = ? WHERE id = ?",
                (severity, incident_id),
            )

    @staticmethod
    def _resolve_if_quiet(
        conn: sqlite3.Connection, incident_id: int, now: datetime
    ) -> bool:
        firing = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE incident_id = ? AND status = 'firing'",
            (incident_id,),
        ).fetchone()[0]
        if firing:
            return False
        return bool(
            conn.execute(
                "UPDATE incidents SET status = 'resolved', resolved_at = ?"
                " WHERE id = ? AND status = 'open'",
                (iso(now), incident_id),
            ).rowcount
        )

    def _close_stale(self, conn: sqlite3.Connection, now: datetime) -> int:
        return conn.execute(
            "UPDATE incidents SET status = 'closed', closed_at = ?"
            " WHERE status = 'resolved' AND resolved_at <= ?",
            (iso(now), iso(now - self.quiet)),
        ).rowcount
