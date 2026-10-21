"""Unit tests for the chaos runner. No cluster needed.

Run with: make chaos-test
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_scenario as rs

T0 = datetime(2026, 10, 21, 18, 0, 0, tzinfo=UTC)
RULES_FILE = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "alert-rules"
    / "kubesentinel-rules.yaml"
)


def sink_line(alertname, seconds, status="firing", namespace="demo", **extra):
    payload = {
        "event": "alert",
        "received_at": (T0 + timedelta(seconds=seconds)).isoformat(),
        "status": status,
        "alertname": alertname,
        "namespace": namespace,
        "service": "orders-api",
        **extra,
    }
    return json.dumps(payload)


def expected(**overrides):
    base = {
        "name": "t",
        "description": "test",
        "expected_alerts": ["PodCrashLooping"],
        "acceptable_alerts": ["DeploymentRolloutStuck"],
        "root_cause": "bad_release",
        "expected_action": "rollback",
        "action_mode": "approval",
        "timeout_seconds": 60,
    }
    return rs.Expected(**{**base, **overrides})


# ------------------------------------------------------------- parse_sink_lines


def test_parse_keeps_alert_events_and_skips_everything_else():
    lines = [
        "not json at all",
        '{"event": "batch", "received_at": "2026-10-21T18:00:00+00:00"}',
        '{"event": "alert"}',  # no received_at: written by an older sink
        "{broken json",
        sink_line("PodCrashLooping", 30),
    ]
    events = rs.parse_sink_lines(lines)
    assert [e.alertname for e in events] == ["PodCrashLooping"]
    assert events[0].received_at == T0 + timedelta(seconds=30)


# ------------------------------------------------------------- evaluate


def test_evaluate_passes_and_measures_time_to_detect():
    events = rs.parse_sink_lines([sink_line("PodCrashLooping", 78)])
    result = rs.evaluate(expected(), events, T0)
    assert result.passed and result.detected
    assert result.time_to_detect_s == 78.0
    assert result.first_seen == {"PodCrashLooping": 78.0}


def test_evaluate_reports_missing_expected_alerts():
    result = rs.evaluate(expected(), [], T0)
    assert not result.detected and not result.passed
    assert result.missing == ["PodCrashLooping"]
    assert result.time_to_detect_s is None


def test_evaluate_flags_alerts_that_are_neither_expected_nor_acceptable():
    lines = [sink_line("PodCrashLooping", 60), sink_line("PVCAlmostFull", 70)]
    result = rs.evaluate(expected(), rs.parse_sink_lines(lines), T0)
    assert result.detected
    assert result.unexpected == ["PVCAlmostFull"]
    assert not result.passed


def test_evaluate_allows_acceptable_alerts():
    lines = [sink_line("PodCrashLooping", 60), sink_line("DeploymentRolloutStuck", 320)]
    result = rs.evaluate(expected(), rs.parse_sink_lines(lines), T0)
    assert result.passed
    assert (
        result.time_to_detect_s == 60.0
    )  # acceptable alerts do not count as detection


def test_evaluate_ignores_old_resolved_and_foreign_alerts():
    lines = [
        sink_line("PodCrashLooping", -30),  # before injection: from an earlier fault
        sink_line("PodCrashLooping", 20, status="resolved"),
        sink_line("PodCrashLooping", 25, namespace="kube-system"),
    ]
    result = rs.evaluate(expected(), rs.parse_sink_lines(lines), T0)
    assert result.first_seen == {}
    assert not result.detected


def test_evaluate_keeps_the_first_arrival_of_each_alert():
    lines = [sink_line("PodCrashLooping", 140), sink_line("PodCrashLooping", 80)]
    result = rs.evaluate(expected(), rs.parse_sink_lines(lines), T0)
    assert result.first_seen == {"PodCrashLooping": 80.0}


def test_negative_scenario_passes_only_when_nothing_fires():
    quiet = expected(expected_alerts=[], acceptable_alerts=[])
    assert rs.evaluate(quiet, [], T0).passed
    noisy = rs.parse_sink_lines([sink_line("HighErrorRate", 40)])
    assert not rs.evaluate(quiet, noisy, T0).passed


# ------------------------------------------------------------- load_expected


def test_load_expected_rejects_typos(tmp_path):
    path = tmp_path / "expected.yaml"
    data = {**rs.asdict(expected()), "expected_alert": ["Typo"]}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="unknown keys"):
        rs.load_expected(path)


def test_load_expected_rejects_missing_keys(tmp_path):
    path = tmp_path / "expected.yaml"
    data = rs.asdict(expected())
    del data["root_cause"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="missing keys"):
        rs.load_expected(path)


# ------------------------------------------------------------- scenario folders

SCENARIO_DIRS = sorted(p for p in rs.SCENARIOS_DIR.iterdir() if p.is_dir())
RULE_NAMES = {
    rule["alert"]
    for group in yaml.safe_load(RULES_FILE.read_text())["spec"]["groups"]
    for rule in group["rules"]
    if "alert" in rule
}


@pytest.mark.parametrize("scenario", SCENARIO_DIRS, ids=lambda p: p.name)
def test_every_scenario_is_complete_and_consistent(scenario):
    exp = rs.load_expected(scenario / "expected.yaml")
    assert exp.name == scenario.name
    for script in ("inject.sh", "revert.sh"):
        path = scenario / script
        assert path.exists(), f"{scenario.name} has no {script}"
        assert os.access(path, os.X_OK), f"{scenario.name}/{script} is not executable"
    # Every alert a scenario relies on must exist in the real rule file.
    for alert in exp.expected_alerts + exp.acceptable_alerts:
        assert alert in RULE_NAMES, f"{scenario.name} expects unknown alert {alert}"
    assert exp.action_mode in ("auto", "approval", "human_only", "none")


def test_the_five_v1_scenarios_exist():
    names = {p.name for p in SCENARIO_DIRS}
    assert {"bad-config", "oom", "saturation", "bad-image", "dependency-down"} <= names
