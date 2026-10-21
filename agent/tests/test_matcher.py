from pathlib import Path

import pytest
import yaml

from app.evidence import Evidence
from app.matcher import Condition, load_signatures, match

SIGNATURES = load_signatures()
SCENARIOS = Path(__file__).resolve().parents[2] / "chaos" / "scenarios"


def evidence_with(**signals) -> Evidence:
    ev = Evidence()
    for name, value in signals.items():
        fact = ev.add("test", f"{name} = {value}")
        ev.signal(name, value, fact)
    return ev


# What the collectors see in each chaos scenario, reduced to its signals.
SCENARIO_SIGNALS = {
    "bad-config": {
        "crash_looping": True,
        "minutes_since_deploy": 2.0,
        "config_error_in_logs": True,
        "rollout_stuck": True,
    },
    "bad-image": {
        "image_pull_error": True,
        "minutes_since_deploy": 1.5,
        "rollout_stuck": True,
    },
    # The previous scenario's revert is a recent deploy; the OOM kill must still win.
    "oom": {
        "oom_killed": True,
        "crash_looping": True,
        "memory_peak_ratio": 0.99,
        "minutes_since_deploy": 6.0,
    },
    "dependency-down": {
        "dependency_unavailable": True,
        "connection_errors_in_logs": True,
        "error_ratio": 0.6,
    },
    "saturation": {"p95_seconds": 1.4, "cpu_ratio": 0.97, "error_ratio": 0.0},
}


@pytest.mark.parametrize("scenario", sorted(SCENARIO_SIGNALS))
def test_each_chaos_scenario_maps_to_its_expected_root_cause(scenario):
    expected = yaml.safe_load((SCENARIOS / scenario / "expected.yaml").read_text())
    top = match(evidence_with(**SCENARIO_SIGNALS[scenario]), SIGNATURES)[0]
    assert top.cause == expected["root_cause"]
    assert top.action == expected["expected_action"]
    assert top.evidence, "a rule-based diagnosis must cite the facts behind it"


def test_healthy_signals_match_nothing():
    assert (
        match(
            evidence_with(error_ratio=0.0, p95_seconds=0.05, cpu_ratio=0.1), SIGNATURES
        )
        == []
    )


def test_saturation_needs_cpu_at_the_limit():
    # Slow but not CPU bound: could be anything; do not guess "scale up".
    assert match(evidence_with(p95_seconds=2.0, cpu_ratio=0.3), SIGNATURES) == []


def test_missing_metric_never_satisfies_a_numeric_condition():
    assert match(evidence_with(cpu_ratio=0.99), SIGNATURES) == []


def test_candidates_are_ranked_and_cite_only_positive_conditions():
    ev = evidence_with(
        crash_looping=True, minutes_since_deploy=3.0, config_error_in_logs=True
    )
    candidates = match(ev, SIGNATURES)
    assert [c.cause for c in candidates] == ["bad_release", "config_invalid"]
    assert candidates[0].evidence == [
        ev.support["crash_looping"],
        ev.support["minutes_since_deploy"],
    ]


def test_signature_file_rejects_unknown_signals_and_ops():
    with pytest.raises(ValueError, match="unknown signal"):
        Condition.model_validate({"signal": "cpu_ration", "gt": 1})
    with pytest.raises(ValueError, match="exactly one of"):
        Condition.model_validate({"signal": "cpu_ratio", "above": 1})


def test_every_cause_in_the_signatures_is_one_the_scenarios_use():
    scenario_causes = set()
    for path in SCENARIOS.glob("*/expected.yaml"):
        data = yaml.safe_load(path.read_text())
        scenario_causes |= {data["root_cause"], *data.get("acceptable_root_causes", [])}
    assert {s.cause for s in SIGNATURES} <= scenario_causes
