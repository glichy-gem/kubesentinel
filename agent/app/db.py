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

# Remediation: every proposed action, its policy decision, who decided, what ran
# and whether the service recovered. The audit log of everything the agent did.
SCHEMA_V3 = """
ALTER TABLE incidents ADD COLUMN telegram_message_id INTEGER;

CREATE TABLE actions (
    id            INTEGER PRIMARY KEY,
    incident_id   INTEGER NOT NULL REFERENCES incidents (id),
    diagnosis_id  INTEGER NOT NULL REFERENCES diagnoses (id),
    created_at    TEXT NOT NULL,
    service       TEXT NOT NULL,  -- the incident's service (rate limits are per service)
    namespace     TEXT NOT NULL,
    type          TEXT NOT NULL,
    target        TEXT NOT NULL,  -- the deployment the action changes
    via           TEXT NOT NULL,  -- git | kubernetes | argocd | none
    params_json   TEXT NOT NULL,
    description   TEXT NOT NULL,  -- the exact change, shown before it runs
    risk          TEXT NOT NULL,
    mode          TEXT NOT NULL,  -- auto | approval | blocked | none
    reason        TEXT NOT NULL,  -- why the policy chose that mode
    status        TEXT NOT NULL,
    token_hash    TEXT,           -- approval token (sha256); the token itself is never stored
    expires_at    TEXT,
    decided_at    TEXT,
    decided_by    TEXT,
    executed_at   TEXT,
    result        TEXT,
    commit_sha    TEXT,
    verified_at   TEXT,
    verification  TEXT,
    telegram_message_id INTEGER  -- the card this action was proposed on
);
CREATE INDEX actions_by_incident ON actions (incident_id);

CREATE TABLE automation_pauses (
    service      TEXT PRIMARY KEY,
    paused_until TEXT NOT NULL,
    reason       TEXT NOT NULL
);
"""

# Applied in order; PRAGMA user_version records how many have run.
# Version 1 uses IF NOT EXISTS because databases created before migrations existed already have it.
MIGRATIONS = [SCHEMA, SCHEMA_V2, SCHEMA_V3]

# Statuses that count as "this action happened" for rate limits.
RAN = ("approved", "executing", "executed", "recovered", "not_recovered", "failed")

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _decode_diagnosis(row: sqlite3.Row) -> dict:
    out = dict(row)
    for key in ("diagnosis_json", "candidates_json", "signals_json", "notes_json"):
        out[key.removesuffix("_json")] = json.loads(out.pop(key))
    return out


def _decode_action(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["params"] = json.loads(out.pop("params_json"))
    out.pop("token_hash")  # never leaves the database
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

    # ------------------------------------------------------------- actions

    def insert_action(self, record: dict) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                f"INSERT INTO actions ({', '.join(record)})"
                f" VALUES ({', '.join('?' for _ in record)})",
                [record[k] for k in record],
            )
            return cursor.lastrowid

    def get_action(self, action_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        return _decode_action(row) if row else None

    def list_actions(
        self, incident_id: int | None = None, limit: int = 50
    ) -> list[dict]:
        query, params = "SELECT * FROM actions", []
        if incident_id is not None:
            query += " WHERE incident_id = ?"
            params.append(incident_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_decode_action(r) for r in rows]

    def transition(
        self, action_id: int, from_statuses: tuple[str, ...], to_status: str, **fields
    ) -> bool:
        """Move an action between statuses atomically; False if it was not in from_statuses.

        This is what makes a double tap on Approve, or an approval racing an
        expiry, harmless: only one transition can win.
        """
        sets = ", ".join(["status = ?", *(f"{k} = ?" for k in fields)])
        marks = ", ".join("?" for _ in from_statuses)
        with self.transaction() as conn:
            return bool(
                conn.execute(
                    f"UPDATE actions SET {sets} WHERE id = ? AND status IN ({marks})",
                    [to_status, *fields.values(), action_id, *from_statuses],
                ).rowcount
            )

    def decide_action(
        self, action_id: int, token_hash: str, approve: bool, by: str, now: datetime
    ) -> bool:
        """Approve or reject a proposed action if the token matches and has not expired."""
        with self.transaction() as conn:
            return bool(
                conn.execute(
                    "UPDATE actions SET status = ?, decided_at = ?, decided_by = ?"
                    " WHERE id = ? AND status = 'proposed' AND token_hash = ? AND expires_at > ?",
                    (
                        "approved" if approve else "rejected",
                        iso(now),
                        by,
                        action_id,
                        token_hash,
                        iso(now),
                    ),
                ).rowcount
            )

    def expire_actions(self, now: datetime) -> list[int]:
        with self.transaction() as conn:
            rows = conn.execute(
                "UPDATE actions SET status = 'expired' WHERE status = 'proposed'"
                " AND expires_at <= ? RETURNING id",
                (iso(now),),
            ).fetchall()
        return [r[0] for r in rows]

    def supersede_pending(self, incident_id: int) -> int:
        """A newer diagnosis replaces any action still waiting for approval."""
        with self.transaction() as conn:
            return conn.execute(
                "UPDATE actions SET status = 'superseded'"
                " WHERE incident_id = ? AND status = 'proposed'",
                (incident_id,),
            ).rowcount

    def count_actions(
        self,
        action_type: str,
        *,
        incident_id: int | None = None,
        service: str | None = None,
        since: datetime | None = None,
    ) -> int:
        query = f"SELECT COUNT(*) FROM actions WHERE type = ? AND status IN ({', '.join('?' for _ in RAN)})"
        params: list = [action_type, *RAN]
        if incident_id is not None:
            query += " AND incident_id = ?"
            params.append(incident_id)
        if service is not None:
            query += " AND service = ?"
            params.append(service)
        if since is not None:
            query += " AND created_at >= ?"
            params.append(iso(since))
        with self._lock:
            return self._conn.execute(query, params).fetchone()[0]

    def set_action_message(self, action_id: int, message_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE actions SET telegram_message_id = ? WHERE id = ?",
                (message_id, action_id),
            )

    def set_telegram_message(self, incident_id: int, message_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE incidents SET telegram_message_id = ? WHERE id = ?",
                (message_id, incident_id),
            )

    def pause_automation(self, service: str, until: datetime, reason: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO automation_pauses (service, paused_until, reason) VALUES (?, ?, ?)"
                " ON CONFLICT (service) DO UPDATE SET paused_until = excluded.paused_until,"
                " reason = excluded.reason",
                (service, iso(until), reason),
            )

    def automation_paused(self, service: str, now: datetime) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM automation_pauses WHERE service = ? AND paused_until > ?",
                (service, iso(now)),
            ).fetchone()
        return row is not None
