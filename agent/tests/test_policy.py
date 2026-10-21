import pytest

from app.policy import History, decide, load_policy

POLICY = load_policy()
CLEAN = History(
    actions_this_incident=0, actions_last_hour_for_service=0, automation_paused=False
)


def run(action, *, namespace="demo", confidence=0.9, history=CLEAN, plan_error=None):
    return decide(POLICY, action, namespace, confidence, history, plan_error)


@pytest.mark.parametrize(
    ("action", "mode"),
    [
        ("restart_pod", "auto"),
        ("scale_up", "auto"),
        ("increase_memory_limit", "approval"),
        ("rollback", "approval"),
        ("restart_dependency", "approval"),
        ("none", "none"),
    ],
)
def test_default_modes_follow_the_design_table(action, mode):
    assert run(action).mode == mode


def test_unknown_actions_are_blocked():
    decision = run("delete_namespace")
    assert decision.mode == "blocked"
    assert decision.risk == "high"


def test_other_namespaces_are_off_limits():
    assert run("restart_pod", namespace="kube-system").mode == "blocked"
    assert run("rollback", namespace="argocd").mode == "blocked"


def test_a_plan_that_could_not_be_built_is_blocked_with_its_reason():
    decision = run(
        "scale_up", plan_error="orders-api has 4 replicas; the policy maximum is 4"
    )
    assert decision.mode == "blocked"
    assert "maximum is 4" in decision.reason


def test_low_confidence_needs_a_human():
    decision = run("scale_up", confidence=0.5)
    assert decision.mode == "approval"
    assert "below" in decision.reason


def test_tripped_circuit_breaker_turns_auto_into_approval():
    paused = History(0, 0, automation_paused=True)
    assert run("restart_pod", history=paused).mode == "approval"
    # Approval-mode actions are unaffected: a human decides anyway.
    assert run("rollback", history=paused).mode == "approval"


def test_rate_limits():
    once = History(
        actions_this_incident=1,
        actions_last_hour_for_service=1,
        automation_paused=False,
    )
    assert run("restart_pod", history=once).mode == "blocked"  # max 1 per incident
    busy = History(
        actions_this_incident=0,
        actions_last_hour_for_service=3,
        automation_paused=False,
    )
    decision = run("restart_pod", history=busy)
    assert decision.mode == "blocked" and "3 times in the last hour" in decision.reason
    assert run("scale_up", history=History(0, 2, False)).mode == "blocked"


def test_policy_file_lists_no_high_risk_action():
    assert {rule.risk for rule in POLICY.actions.values()} <= {"none", "low", "medium"}
    assert POLICY.namespaces == ["demo"]
