import json

import pytest
from conftest import T0, am_alert, parse
from fakes import FakeKube, FakeNotifier
from test_collectors import APPS, NS, deployment, pod

from app.correlator import Correlator
from app.evidence import Signals
from app.policy import load_policy
from app.rca import Diagnosis, ProposedAction
from app.remediation import Planner, Responder, memory_mi, token_hash

POLICY = load_policy()


def kube(orders=None, redis=None, pods=()):
    return FakeKube(
        {
            f"{APPS}/deployments/orders-api": orders or deployment("orders-api"),
            f"{APPS}/deployments/redis": redis or deployment("redis", 1, 1, 1),
            f"{NS}/pods": {"items": list(pods)},
        }
    )


def planner(k=None) -> Planner:
    return Planner(k or kube(), POLICY, "owner/gitops")


@pytest.mark.parametrize(
    ("quantity", "mi"),
    [("128Mi", 128), ("1Gi", 1024), ("134217728", 128), ("256M", 244.140625)],
)
def test_memory_quantities(quantity, mi):
    assert memory_mi(quantity) == pytest.approx(mi)


def test_memory_plan_doubles_the_limit_up_to_the_policy_maximum():
    plan = planner().plan("increase_memory_limit", "orders-api", "demo", Signals())
    assert plan.via == "git" and plan.error is None
    assert plan.params == {
        "path": "apps/orders-api/deployment.yaml",
        "container": "orders-api",
        "from": "128Mi",
        "to": "256Mi",
    }
    assert "128Mi -> 256Mi" in plan.description

    at_max = deployment("orders-api")
    at_max["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"][
        "memory"
    ] = "512Mi"
    blocked = planner(kube(orders=at_max)).plan(
        "increase_memory_limit", "orders-api", "demo", Signals()
    )
    assert "policy maximum" in blocked.error


def test_scale_plan_respects_max_replicas():
    plan = planner().plan("scale_up", "orders-api", "demo", Signals())
    assert plan.params["from"] == 2 and plan.params["to"] == 3
    full = planner(
        kube(orders=deployment("orders-api", replicas=4, ready=4, updated=4))
    )
    assert (
        "maximum is 4" in full.plan("scale_up", "orders-api", "demo", Signals()).error
    )


def test_rollback_needs_a_recent_suspect_deploy():
    recent = Signals(
        suspect_revision="abc1234567",
        suspect_message="chore: tune",
        minutes_since_deploy=2.0,
    )
    plan = planner().plan("rollback", "orders-api", "demo", recent)
    assert plan.params["revision"] == "abc1234567"
    assert "git revert abc1234" in plan.description

    assert (
        planner().plan("rollback", "orders-api", "demo", Signals()).error
        == "no recent deploy to roll back"
    )
    old = Signals(suspect_revision="abc1234567", minutes_since_deploy=45.0)
    assert (
        "too old to blame"
        in planner().plan("rollback", "orders-api", "demo", old).error
    )


def test_restart_pod_picks_the_worst_pod():
    k = kube(
        pods=[
            pod("orders-api-ok", ready=True, restarts=0),
            pod("orders-api-bad", ready=False, restarts=5),
            pod("orders-api-meh", ready=True, restarts=9),
        ]
    )
    plan = planner(k).plan("restart_pod", "orders-api", "demo", Signals())
    assert plan.params == {"pod": "orders-api-bad"}


def test_dependency_scaled_to_zero_is_resynced_from_git():
    k = kube(redis=deployment("redis", replicas=0, ready=0, updated=0))
    plan = planner(k).plan("restart_dependency", "redis", "demo", Signals())
    assert (plan.via, plan.params) == ("argocd", {"application": "redis"})
    running = planner().plan("restart_dependency", "redis", "demo", Signals())
    assert (running.via, running.params) == ("kubernetes", {"selector": "app=redis"})


def test_planning_errors_never_raise():
    plan = planner(FakeKube({})).plan("scale_up", "orders-api", "demo", Signals())
    assert plan.error.startswith("cannot plan")


# ------------------------------------------------------------- responder


def diagnosis(
    action="increase_memory_limit",
    confidence=0.85,
    needs_human=True,
    target="orders-api",
):
    return Diagnosis(
        summary="s",
        root_cause="memory_limit_too_low",
        confidence=confidence,
        evidence=["E1"],
        proposed_action=ProposedAction(type=action, target=target),
        needs_human=needs_human,
    )


@pytest.fixture
def incident(db):
    Correlator(db, 10).ingest(parse(am_alert("PodOOMKilled")), T0)
    diag_id = db.save_diagnosis(
        1,
        {
            "incident_id": 1,
            "created_at": "2026-10-21T18:00:01.000+00:00",
            "source": "llm",
            "root_cause": "memory_limit_too_low",
            "confidence": 0.85,
            "action_type": "x",
            "action_target": "orders-api",
            "prompt_version": "v",
            "model": "m",
            "latency_ms": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "agrees_with_matcher": 1,
            "collect_ms": 1,
            "diagnosis_json": "{}",
            "candidates_json": "[]",
            "signals_json": "{}",
            "notes_json": "[]",
        },
        [],
    )
    return db.get_incident(1), diag_id


def responder(db, executed):
    notifier = FakeNotifier()
    return Responder(db, planner(), POLICY, notifier, executed.append), notifier


def test_approval_action_gets_a_token_a_card_and_waits(db, incident):
    inc, diag_id = incident
    executed = []
    r, notifier = responder(db, executed)
    action = r.respond(inc, diag_id, diagnosis(), Signals(), None)
    assert (action["mode"], action["status"]) == ("approval", "proposed")
    assert executed == []
    ((_, action_id, mode, token),) = notifier.cards
    assert mode == "approval" and len(token) >= 16
    assert "token_hash" not in action  # never exposed
    assert db.decide_action(action_id, token_hash(token), True, "me", T0)


def test_auto_action_runs_without_waiting(db, incident):
    inc, diag_id = incident
    executed = []
    r, notifier = responder(db, executed)
    action = r.respond(
        inc, diag_id, diagnosis("scale_up", needs_human=False), Signals(), None
    )
    assert (action["mode"], action["status"], action["decided_by"]) == (
        "auto",
        "approved",
        "policy (auto)",
    )
    assert executed == [action["id"]]
    assert notifier.cards[0][3] is None  # no buttons for auto actions


def test_model_asking_for_a_human_downgrades_auto(db, incident):
    inc, diag_id = incident
    executed = []
    r, _ = responder(db, executed)
    action = r.respond(
        inc, diag_id, diagnosis("scale_up", needs_human=True), Signals(), None
    )
    assert action["mode"] == "approval" and executed == []


def test_newer_diagnosis_supersedes_a_pending_proposal(db, incident):
    inc, diag_id = incident
    r, _ = responder(db, [])
    first = r.respond(inc, diag_id, diagnosis(), Signals(), None)
    r.respond(inc, diag_id, diagnosis(), Signals(), None)
    assert db.get_action(first["id"])["status"] == "superseded"


def test_blocked_plan_is_recorded_with_its_reason(db, incident):
    inc, diag_id = incident
    r, _ = responder(db, [])
    action = r.respond(inc, diag_id, diagnosis("rollback"), Signals(), None)
    assert (action["mode"], action["status"]) == ("blocked", "blocked")
    assert action["reason"] == "no recent deploy to roll back"
    assert json.loads(json.dumps(action["params"])) == {}
