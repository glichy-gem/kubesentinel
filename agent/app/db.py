"""SQLite storage for alerts and incidents.

One connection guarded by a lock: the agent handles a few requests a minute, so
simplicity beats a pool. WAL mode lets readers (the API) run while intake writes.
"""

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                 INTEGER PRIMARY KEY,
    namespace          TEXT NOT NULL,
    service            TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'closed')),
    severity           TEXT NOT NULL,
    title              TEXT NOT NULL,
    opened_at          TEXT NOT NULL,  -- when the agent received the first alert
    first_alert_at     TEXT NOT NULL,  -- earliest startsAt among its alerts
    updated_at         TEXT NOT NULL,
    resolved_at        TEXT,
    closed_at          TEXT,
    raw_alert_count    INTEGER NOT NULL DEFAULT 0,  -- distinct alerts grouped here
    notification_count INTEGER NOT NULL DEFAULT 0   -- webhook entries, repeats included
);
CREATE INDEX IF NOT EXISTS incidents_by_key ON incidents (namespace, service, status);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY,
    incident_id   INTEGER NOT NULL REFERENCES incidents (id),
    fingerprint   TEXT NOT NULL,
    starts_at     TEXT NOT NULL,
    name          TEXT NOT NULL,
    severity      TEXT NOT NULL,
    status        TEXT NOT NULL,
    labels        TEXT NOT NULL,
    annotations   TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    ends_at       TEXT,
    notifications INTEGER NOT NULL DEFAULT 1,
    -- One row per alert occurrence: the same alert firing again later is a new row.
    UNIQUE (fingerprint, starts_at)
);
"""

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def iso(dt: datetime) -> str:
    """One fixed format, so timestamps compare correctly as strings in SQL."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Everything inside commits together or not at all."""
        with self._lock, self._conn:
            yield self._conn

    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1")

    def list_incidents(self, status: str | None = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM incidents"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(query, params)]

    def get_incident(self, incident_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                return None
            alerts = self._conn.execute(
                "SELECT * FROM alerts WHERE incident_id = ? ORDER BY received_at, id",
                (incident_id,),
            ).fetchall()
        incident = dict(row)
        incident["alerts"] = [
            {
                **dict(a),
                "labels": json.loads(a["labels"]),
                "annotations": json.loads(a["annotations"]),
            }
            for a in alerts
        ]
        return incident

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM incidents GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}
