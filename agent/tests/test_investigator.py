import sqlite3

from conftest import T0, am_alert, am_payload
from fakes import FakeLLM
from fastapi.testclient import TestClient
from test_collectors import cluster, collector, pod, ts
from test_rca import reply

from app.config import Settings
from app.db import SCHEMA, Database
from app.investigator import Investigator
from app.main import create_app
from app.matcher import load_signatures


def make_client(tmp_path, llm) -> TestClient:
    settings = Settings(db_path=str(tmp_path / "agent.db"))
    kube = cluster(
        [pod("orders-api-a", restarts=2, last_terminated=("OOMKilled", ts(-1)))]
    )
    db = Database(settings.db_path)
    investigator = Investigator(
        db,
        collector(kube),
        load_signatures(),
        llm,
        {"orders-api": ["redis"]},
        delay_seconds=0,
    )
    app = create_app(settings, investigator=investigator)
    return TestClient(app), investigator


def test_new_incident_is_investigated_and_the_diagnosis_is_stored(tmp_path):
    client, investigator = make_client(tmp_path, FakeLLM(reply()))
    response = client.post(
        "/alerts", json=am_payload(am_alert("PodOOMKilled", starts_at=T0))
    )
    incident_id = response.json()["incidents"][0]["id"]
    investigator._pool.shutdown(wait=True)  # let the background investigation finish

    incident = client.get(f"/incidents/{incident_id}").json()
    assert incident["root_cause"] == "memory_limit_too_low"
    assert incident["proposed_action"] == "increase_memory_limit orders-api"
    assert incident["diagnosed_at"]
    d = incident["diagnosis"]
    assert d["source"] == "llm" and d["model"] == "fake-1"
    assert d["candidates"][0]["signature"] == "oom-killed"
    assert d["signals"]["oom_killed"] is True
    assert [e["fact_id"] for e in incident["evidence"]][:1] == ["E1"]


def test_repeat_notifications_do_not_trigger_new_investigations(tmp_path):
    llm = FakeLLM(reply())  # a second LLM call would fail: only one reply queued
    client, investigator = make_client(tmp_path, llm)
    for _ in range(3):
        client.post("/alerts", json=am_payload(am_alert("PodOOMKilled", starts_at=T0)))
    investigator._pool.shutdown(wait=True)
    assert len(llm.prompts) == 1


def test_a_new_symptom_triggers_a_second_investigation(tmp_path):
    llm = FakeLLM(reply(), reply())
    client, investigator = make_client(tmp_path, llm)
    client.post("/alerts", json=am_payload(am_alert("PodCrashLooping", starts_at=T0)))
    client.post("/alerts", json=am_payload(am_alert("PodOOMKilled", starts_at=T0)))
    investigator._pool.shutdown(wait=True)
    assert len(llm.prompts) == 2
    assert "PodOOMKilled" in llm.prompts[1]


def test_reinvestigate_endpoint(tmp_path):
    client, investigator = make_client(tmp_path, FakeLLM(reply(), reply()))
    incident_id = client.post("/alerts", json=am_payload(am_alert())).json()[
        "incidents"
    ][0]["id"]
    assert client.post(f"/incidents/{incident_id}/investigate").status_code == 202
    assert client.post("/incidents/999/investigate").status_code == 404
    investigator._pool.shutdown(wait=True)
    assert len(client.app.state.db.get_incident(incident_id)["diagnosis"]) > 0


def test_database_from_phase_5a_is_migrated_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)  # what 5a created, without a user_version
    old.execute(
        "INSERT INTO incidents (namespace, service, status, severity, title, opened_at,"
        " first_alert_at, updated_at) VALUES ('demo', 'orders-api', 'open', 'critical', 't', 'x', 'x', 'x')"
    )
    old.commit()
    old.close()

    db = Database(path)
    incident = db.get_incident(1)
    assert incident["title"] == "t" and incident["diagnosis"] is None
    assert "root_cause" in incident
    Database(path)  # reopening runs nothing twice
