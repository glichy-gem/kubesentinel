# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Run chaos scenarios end to end and record what the alerting pipeline and the agent did.

Steps: wait for a healthy baseline, inject the fault, watch the alerts that reach
the Alertmanager webhook, compare them with expected.yaml, revert, and wait for
recovery. The revert always runs, even if detection fails or the runner crashes.
Each run writes a JSON result to chaos/results/.

With --agent (Phase 6 evals), it also waits for the agent's incident, diagnosis
and proposed action, grades them against expected.yaml, saves every diagnosis
snapshot to evals/snapshots/ for offline replay, and appends a row to
evals/results/<eval id>.csv. It refuses to start unless the agent is in dry-run
mode (make eval-mode-on), so a batch of runs never notifies anyone or acts.

Usage: uv run chaos/run_scenario.py <scenario>... [--agent] [--runs N] [--keep-fault]
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

import agent_eval
import yaml

CHAOS_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = CHAOS_DIR / "scenarios"
RESULTS_DIR = CHAOS_DIR / "results"
EVALS_DIR = CHAOS_DIR.parent / "evals"
PROMETHEUS_URL = "http://localhost:9090"
APP_URL = "http://localhost:8080"
NAMESPACE = "demo"
SERVICE = "orders-api"  # the service every scenario is about
DEPLOYMENTS = ("orders-api", "redis")
# The agent's API through the Kubernetes API server: no port-forward to babysit.
AGENT_API = "/api/v1/namespaces/kubesentinel/services/kubesentinel-agent:http/proxy"
AGENT_TIMEOUT_S = 240  # after the alerts arrive, how long the agent gets to diagnose


# ---------------------------------------------------------------- expectations


@dataclass(frozen=True)
class Expected:
    name: str
    description: str
    expected_alerts: list[str]
    acceptable_alerts: list[str]
    root_cause: str
    expected_action: str
    action_mode: str
    timeout_seconds: int
    acceptable_root_causes: list[str] = field(default_factory=list)
    delivered_by: str = ""
    # Text that must reach the agent's evidence, or the run proves nothing
    # (the prompt-injection scenario checks its hostile log line got through).
    evidence_must_contain: str = ""


def load_expected(path: Path) -> Expected:
    """Load expected.yaml strictly: missing or unknown keys are errors, not typos to ignore."""
    data = yaml.safe_load(path.read_text())
    known = {f.name for f in fields(Expected)}
    required = {
        f.name
        for f in fields(Expected)
        if f.default is MISSING and f.default_factory is MISSING
    }
    missing = required - data.keys()
    unknown = data.keys() - known
    if missing or unknown:
        raise ValueError(
            f"{path}: missing keys {sorted(missing)}, unknown keys {sorted(unknown)}"
        )
    return Expected(**data)


# ---------------------------------------------------------------- alert events


@dataclass(frozen=True)
class AlertEvent:
    received_at: datetime
    status: str
    alertname: str
    namespace: str | None
    service: str | None


def parse_sink_lines(lines: Iterable[str]) -> list[AlertEvent]:
    """Turn webhook-sink log lines into alert events; anything else is skipped."""
    events = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("event") != "alert" or "received_at" not in data:
            continue
        events.append(
            AlertEvent(
                received_at=datetime.fromisoformat(data["received_at"]),
                status=data.get("status", ""),
                alertname=data.get("alertname", ""),
                namespace=data.get("namespace"),
                service=data.get("service"),
            )
        )
    return events


@dataclass
class Evaluation:
    first_seen: dict[str, float]  # alertname -> seconds after injection
    missing: list[str]
    unexpected: list[str]
    detected: bool
    time_to_detect_s: float | None
    passed: bool


def evaluate(
    expected: Expected, events: Iterable[AlertEvent], injected_at: datetime
) -> Evaluation:
    """Compare the firing alerts that arrived after injection with expected.yaml."""
    first_seen: dict[str, float] = {}
    for event in sorted(events, key=lambda e: e.received_at):
        if event.status != "firing" or event.received_at < injected_at:
            continue
        if event.namespace not in (None, NAMESPACE):
            continue
        delay = round((event.received_at - injected_at).total_seconds(), 1)
        first_seen.setdefault(event.alertname, delay)

    missing = [a for a in expected.expected_alerts if a not in first_seen]
    allowed = set(expected.expected_alerts) | set(expected.acceptable_alerts)
    unexpected = sorted(a for a in first_seen if a not in allowed)
    detected = not missing
    time_to_detect = min(
        (first_seen[a] for a in expected.expected_alerts if a in first_seen),
        default=None,
    )
    return Evaluation(
        first_seen=first_seen,
        missing=missing,
        unexpected=unexpected,
        detected=detected,
        time_to_detect_s=time_to_detect,
        passed=detected and not unexpected,
    )


# ---------------------------------------------------------------- cluster checks


def log(message: str) -> None:
    print(f"[{datetime.now(UTC):%H:%M:%S}] {message}", file=sys.stderr, flush=True)


def kubectl(*args: str) -> str:
    # Callers treat "no output" as "not ready yet", so a failing kubectl is not an error here.
    result = subprocess.run(
        ["kubectl", *args], check=False, capture_output=True, text=True, timeout=60
    )
    return result.stdout if result.returncode == 0 else ""


def http_get(url: str, timeout: float = 10) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, b""


def deployment_ready(name: str) -> bool:
    out = kubectl(
        "-n",
        NAMESPACE,
        "get",
        "deployment",
        name,
        "-o",
        "jsonpath={.spec.replicas} {.status.readyReplicas} {.status.updatedReplicas}",
    ).split()
    return len(out) == 3 and out[0] == out[1] == out[2] and out[0] != "0"


def argocd_problems() -> list[str]:
    apps = kubectl(
        "-n",
        "argocd",
        "get",
        "applications",
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}={.status.sync.status}/{.status.health.status} {end}",
    ).split()
    bad = [a for a in apps if not a.endswith("=Synced/Healthy")]
    if not apps:
        return ["argo cd applications not found"]
    return [f"argo cd not synced/healthy: {' '.join(bad)}"] if bad else []


def firing_alerts() -> list[str] | None:
    status, body = http_get(f"{PROMETHEUS_URL}/api/v1/alerts")
    if status != 200:
        return None
    alerts = json.loads(body)["data"]["alerts"]
    return sorted(
        {
            a["labels"]["alertname"]
            for a in alerts
            if a["state"] == "firing" and a["labels"].get("namespace") == NAMESPACE
        }
    )


def recovery_problems() -> list[str]:
    problems = [
        f"deployment {d} not ready" for d in DEPLOYMENTS if not deployment_ready(d)
    ]
    if http_get(f"{APP_URL}/ready")[0] != 200:
        problems.append("orders-api /ready is not 200")
    return problems + argocd_problems()


def baseline_problems() -> list[str]:
    problems = recovery_problems()
    firing = firing_alerts()
    if firing is None:
        problems.append("prometheus is unreachable")
    elif firing:
        problems.append(
            f"alerts still firing from an earlier fault: {', '.join(firing)}"
        )
    return problems


def wait_until_clear(
    check: Callable[[], list[str]], timeout: float, label: str
) -> tuple[bool, float, list[str]]:
    start = time.monotonic()
    while True:
        problems = check()
        elapsed = time.monotonic() - start
        if not problems:
            return True, elapsed, []
        if elapsed > timeout:
            return False, elapsed, problems
        log(f"{label}: waiting ({'; '.join(problems)})")
        time.sleep(15)


def run_script(path: Path) -> None:
    subprocess.run(["bash", str(path)], check=True, timeout=300)


def sink_events(since_seconds: int) -> list[AlertEvent]:
    out = kubectl(
        "-n", "monitoring", "logs", "deploy/webhook-sink", f"--since={since_seconds}s"
    )
    return parse_sink_lines(out.splitlines())


# ---------------------------------------------------------------- the agent


def agent_get(path: str):
    out = kubectl("get", "--raw", f"{AGENT_API}{path}")
    return json.loads(out) if out else None


def agent_problems() -> list[str]:
    """The agent must be up, in dry-run mode, and done with earlier incidents."""
    version = agent_get("/version")
    if version is None:
        return ["the agent API is unreachable"]
    if not version.get("dry_run"):
        return ["the agent is not in dry-run mode (run make eval-mode-on)"]
    leftover = [
        i["id"]
        for status in ("open", "resolved")
        for i in agent_get(f"/incidents?status={status}") or []
        if i["namespace"] == NAMESPACE
    ]
    return [f"agent incidents not closed yet: {leftover}"] if leftover else []


def agent_records(injected_at: datetime) -> list[agent_eval.IncidentRecord]:
    incidents = agent_eval.opened_since(
        agent_get("/incidents?limit=50") or [], injected_at, NAMESPACE
    )
    return [
        agent_eval.IncidentRecord(
            incident=agent_get(f"/incidents/{i['id']}") or i,
            diagnoses=agent_get(f"/incidents/{i['id']}/diagnoses") or [],
            actions=agent_get(f"/actions?incident_id={i['id']}") or [],
        )
        for i in incidents
    ]


def wait_for_agent(injected_at: datetime, timeout: float) -> None:
    """Until the service's incident has a diagnosis and an action is recorded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = [
            r for r in agent_records(injected_at) if r.incident["service"] == SERVICE
        ]
        if records and records[0].diagnoses and records[0].actions:
            return
        state = (
            "no incident yet"
            if not records
            else "diagnosing"
            if not records[0].diagnoses
            else "planning"
        )
        log(f"agent: {state}")
        time.sleep(10)
    log("agent: gave up waiting")


# ---------------------------------------------------------------- the run


def run(
    name: str,
    *,
    skip_baseline: bool,
    keep_fault: bool,
    baseline_timeout: float,
    agent: bool = False,
    eval_id: str = "",
) -> int:
    scenario_dir = SCENARIOS_DIR / name
    expected = load_expected(scenario_dir / "expected.yaml")
    result: dict = {"scenario": name, "expected": asdict(expected)}

    def baseline() -> list[str]:
        return baseline_problems() + (agent_problems() if agent else [])

    if not skip_baseline:
        log("checking for a healthy baseline")
        ok, waited, problems = wait_until_clear(baseline, baseline_timeout, "baseline")
        if not ok:
            log(f"baseline never became healthy: {'; '.join(problems)}")
            return 2
        result["baseline_wait_s"] = round(waited, 1)

    injected_at = datetime.now(UTC)
    result["injected_at"] = injected_at.isoformat()
    evaluation = evaluate(expected, [], injected_at)
    log(f"injecting {name}: {expected.description}")
    try:
        run_script(scenario_dir / "inject.sh")
        deadline = time.monotonic() + expected.timeout_seconds
        while time.monotonic() < deadline:
            elapsed = int((datetime.now(UTC) - injected_at).total_seconds())
            evaluation = evaluate(expected, sink_events(elapsed + 60), injected_at)
            log(f"t+{elapsed}s alerts seen: {evaluation.first_seen or 'none yet'}")
            if expected.expected_alerts and evaluation.detected:
                break
            time.sleep(10)
        if agent and expected.root_cause != agent_eval.NO_INCIDENT:
            wait_for_agent(injected_at, AGENT_TIMEOUT_S)
    finally:
        if keep_fault:
            log("--keep-fault: leaving the fault in place, run revert.sh yourself")
        else:
            log(f"reverting {name}")
            revert_start = time.monotonic()
            run_script(scenario_dir / "revert.sh")
            ok, _, problems = wait_until_clear(recovery_problems, 300, "recovery")
            result["recovered"] = ok
            result["time_to_recover_s"] = round(time.monotonic() - revert_start, 1)
            if not ok:
                log(f"did not recover: {'; '.join(problems)}")

    result["evaluation"] = asdict(evaluation)
    passed = evaluation.passed
    if agent:
        # Read after recovery, so the alert counts cover the whole run.
        records = agent_records(injected_at)
        outcome = agent_eval.summarize(
            records,
            injected_at,
            service=SERVICE,
            dry_run=True,
            evidence_marker=expected.evidence_must_contain,
        )
        result_grade = agent_eval.grade(expected, outcome)
        result["agent"] = asdict(outcome)
        result["grade"] = asdict(result_grade)
        passed = result_grade.passed
        save_eval(
            eval_id,
            name,
            injected_at,
            expected,
            evaluation,
            outcome,
            result_grade,
            result,
            records,
        )

    RESULTS_DIR.mkdir(exist_ok=True)
    out_file = RESULTS_DIR / f"{name}-{injected_at:%Y%m%dT%H%M%SZ}.json"
    out_file.write_text(json.dumps(result, indent=2) + "\n")
    print_summary(result, evaluation, out_file)
    return 0 if passed and result.get("recovered", True) else 1


def save_eval(
    eval_id,
    name,
    injected_at,
    expected,
    evaluation,
    outcome,
    result_grade,
    result,
    records,
) -> None:
    stamp = f"{name}-{injected_at:%Y%m%dT%H%M%SZ}"
    snapshots = EVALS_DIR / "snapshots" / eval_id
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"{stamp}.json").write_text(
        json.dumps(
            {
                "scenario": name,
                "injected_at": injected_at.isoformat(),
                "expected": asdict(expected),
                "incidents": [asdict(r) for r in records],
            },
            indent=1,
        )
        + "\n"
    )
    csv_file = EVALS_DIR / "results" / f"{eval_id}.csv"
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    new = not csv_file.exists()
    with csv_file.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=agent_eval.CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(
            agent_eval.csv_row(
                eval_id,
                name,
                injected_at,
                expected,
                evaluation,
                outcome,
                result_grade,
                result,
            )
        )


def print_summary(result: dict, evaluation: Evaluation, out_file: Path) -> None:
    passed = result.get("grade", {}).get("passed", evaluation.passed)
    verdict = "PASS" if passed and result.get("recovered", True) else "FAIL"
    print(f"\n{verdict}  {result['scenario']}")
    print(f"  time to detect : {evaluation.time_to_detect_s} s")
    print(f"  alerts seen    : {evaluation.first_seen or 'none'}")
    if evaluation.missing:
        print(f"  missing        : {evaluation.missing}")
    if evaluation.unexpected:
        print(f"  unexpected     : {evaluation.unexpected}")
    if "agent" in result:
        a, exp = result["agent"], result["expected"]
        print(
            f"  agent          : {a['incidents']} incident(s) from {a['raw_alerts']} alerts;"
            f" diagnosed in {a['time_to_diagnose_s']} s"
        )
        print(
            f"  root cause     : {a['root_cause']} (expected {exp['root_cause']}),"
            f" confidence {a['confidence']}, by {a['source']} {a['model'] or ''}"
        )
        print(
            f"  action         : {a['action_type']} {a['action_target'] or ''}"
            f" [{a['action_mode']}] (expected {exp['expected_action']} [{exp['action_mode']}])"
        )
        if a["marker_in_evidence"] is not None:
            print(f"  marker in evidence: {a['marker_in_evidence']}")
        if a["unsafe_actions"]:
            print(f"  UNSAFE ACTIONS : {a['unsafe_actions']}")
    if "recovered" in result:
        print(
            f"  recovered      : {result['recovered']} in {result['time_to_recover_s']} s"
        )
    print(f"  result file    : {out_file.relative_to(CHAOS_DIR.parent)}")


def main() -> int:
    scenarios = sorted(
        p.name for p in SCENARIOS_DIR.iterdir() if (p / "expected.yaml").exists()
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", nargs="+", choices=scenarios)
    parser.add_argument(
        "--agent", action="store_true", help="also grade the agent (needs dry-run mode)"
    )
    parser.add_argument("--runs", type=int, default=1, help="runs per scenario")
    parser.add_argument("--eval-id", default=f"{datetime.now(UTC):%Y%m%dT%H%M}")
    parser.add_argument(
        "--keep-fault", action="store_true", help="do not revert (debugging)"
    )
    parser.add_argument(
        "--skip-baseline", action="store_true", help="skip the health check"
    )
    parser.add_argument("--baseline-timeout", type=float, default=900)
    args = parser.parse_args()

    # Round robin (a b c a b c), not a a a b b b: drift over the evening, such
    # as LLM quota or laptop load, then spreads evenly over the scenarios.
    plan = [s for _ in range(args.runs) for s in args.scenario]
    failures = 0
    for number, name in enumerate(plan, start=1):
        log(f"=== run {number}/{len(plan)}: {name}")
        code = run(
            name,
            skip_baseline=args.skip_baseline,
            keep_fault=args.keep_fault,
            baseline_timeout=args.baseline_timeout,
            agent=args.agent,
            eval_id=args.eval_id,
        )
        if code == 2:
            log("stopping: the cluster did not return to a healthy baseline")
            return 2
        failures += code != 0
    if len(plan) > 1:
        log(f"done: {len(plan) - failures}/{len(plan)} runs passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
