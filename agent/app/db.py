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

# Each investigation stores the facts it collected and the diagnosis it reached,
# with the prompt version and model, so evals can compare runs later.
SCHEMA_V2 = """
ALTER TABLE incidents ADD COLUMN diagnosed_at TEXT;
ALTER TABLE incidents ADD COLUMN root_cause TEXT;
ALTER TABLE incidents ADD COLUMN proposed_action TEXT;

CREATE TABLE diagnoses (
    id                  INTEGER PRIMARY KEY,
    incident_id         INTEGER NOT NULL REFERENCES incidents (id),
    created_at          TEXT NOT NULL,
    source              TEXT NOT NULL,  -- llm | matcher
    root_cause          TEXT NOT NULL,
    confidence          REAL NOT NULL,
    action_type         TEXT NOT NULL,
    action_target       TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    model               TEXT NOT NULL,
    latency_ms          INTEGER NOT NULL,
    input_tokens        INTEGER NOT NULL,
    output_tokens       INTEGER NOT NULL,
    agrees_with_matcher INTEGER,
    collect_ms          INTEGER NOT NULL,
    diagnosis_json      TEXT NOT NULL,
    candidates_json     TEXT NOT NULL,
    signals_json        TEXT NOT NULL,
    notes_json          TEXT NOT NULL
);

CREATE TABLE evidence (
    id           INTEGER PRIMARY KEY,
    diagnosis_id INTEGER NOT NULL REFERENCES diagnoses (id),
    incident_id  INTEGER NOT NULL REFERENCES incidents (id),
    fact_id      TEXT NOT NULL,
    source       TEXT NOT NULL,
    content      TEXT NOT NULL
);
"""

# Applied in order; PRAGMA user_version records how many have run.
# Version 1 uses IF NOT EXISTS because databases created before migrations existed already have it.
MIGRATIONS = [SCHEMA, SCHEMA_V2]

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _decode_diagnosis(row: sqlite3.Row) -> dict:
    out = dict(row)
    for key in ("diagnosis_json", "candidates_json", "signals_json", "notes_json"):
        out[key.removesuffix("_json")] = json.loads(out.pop(key))
    return out


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
            self._migrate()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # executescript commits on its own, so the version bump rides in the same script.
            self._conn.executescript(
                f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
            )

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
            diagnosis = self._conn.execute(
                "SELECT * FROM diagnoses WHERE incident_id = ? ORDER BY id DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
            facts = (
                self._conn.execute(
                    "SELECT fact_id, source, content FROM evidence WHERE diagnosis_id = ?"
                    " ORDER BY id",
                    (diagnosis["id"],),
                ).fetchall()
                if diagnosis
                else []
            )
        incident = dict(row)
        incident["alerts"] = [
            {
                **dict(a),
                "labels": json.loads(a["labels"]),
                "annotations": json.loads(a["annotations"]),
            }
            for a in alerts
        ]
        incident["diagnosis"] = _decode_diagnosis(diagnosis) if diagnosis else None
        incident["evidence"] = [dict(f) for f in facts]
        return incident

    def save_diagnosis(self, incident_id: int, record: dict, facts: list[dict]) -> int:
        """Store one investigation and stamp the incident with its outcome."""
        with self.transaction() as conn:
            cursor = conn.execute(
                f"INSERT INTO diagnoses ({', '.join(record)})"
                f" VALUES ({', '.join('?' for _ in record)})",
                [record[k] for k in record],
            )
            conn.executemany(
                "INSERT INTO evidence (diagnosis_id, incident_id, fact_id, source, content)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (cursor.lastrowid, incident_id, f["id"], f["source"], f["text"])
                    for f in facts
                ],
            )
            conn.execute(
                "UPDATE incidents SET diagnosed_at = ?, root_cause = ?, proposed_action = ?"
                " WHERE id = ?",
                (
                    record["created_at"],
                    record["root_cause"],
                    f"{record['action_type']} {record['action_target']}",
                    incident_id,
                ),
            )
            return cursor.lastrowid

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM incidents GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}
