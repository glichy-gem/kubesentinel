# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Time yourself debugging a failure without the agent (design doc section 9).

Blind on purpose: a random v1 scenario is injected and its name is not shown,
so you debug it like a real page. Use anything except the agent: kubectl,
Grafana, Prometheus, Loki, Argo CD. Press Enter the moment you know the root
cause, then name it and the fix. The fault is reverted either way.

Rows go to evals/manual-baseline.csv; `make report` puts the median per
scenario next to the agent's time.

Usage: make manual   (or: uv run evals/manual_baseline.py [--scenario oom])
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chaos"))
import run_scenario as rs

V1 = ["bad-config", "bad-image", "dependency-down", "oom", "saturation"]
CAUSES = [
    "bad_release",
    "config_invalid",
    "image_pull_error",
    "memory_limit_too_low",
    "saturation",
    "dependency_down",
    "unknown",
]
ACTIONS = [
    "rollback",
    "increase_memory_limit",
    "scale_up",
    "restart_dependency",
    "restart_pod",
    "none",
]
OUT = Path(__file__).resolve().parent / "manual-baseline.csv"
FIELDS = [
    "scenario",
    "started_at",
    "seconds_to_diagnose",
    "root_cause",
    "action",
    "cause_correct",
    "action_correct",
]


def choose(prompt: str, options: list[str]) -> str:
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}")
    while True:
        answer = input(f"{prompt} [1-{len(options)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=V1, help="default: a random one, hidden")
    args = parser.parse_args()
    name = args.scenario or random.choice(V1)
    expected = rs.load_expected(rs.SCENARIOS_DIR / name / "expected.yaml")

    print("Waiting for a healthy baseline...")
    ok, _, problems = rs.wait_until_clear(rs.baseline_problems, 900, "baseline")
    if not ok:
        print(f"baseline never became healthy: {problems}")
        return 2

    print("\nInjecting a failure. Start debugging once the first alert fires")
    print(
        "(Grafana http://localhost:3000, Prometheus alerts http://localhost:9090/alerts)."
    )
    rs.run_script(rs.SCENARIOS_DIR / name / "inject.sh")
    started = time.monotonic()
    started_at = datetime.now(UTC)
    try:
        input("\n>>> Press Enter the moment you know the root cause <<<")
        seconds = round(time.monotonic() - started)
        print(f"\n{seconds} s. What was it?")
        cause = choose("Root cause", CAUSES)
        action = choose("Fix you would apply", ACTIONS)
    finally:
        print(f"\nIt was: {name} ({expected.description}). Reverting...")
        rs.run_script(rs.SCENARIOS_DIR / name / "revert.sh")
        rs.wait_until_clear(rs.recovery_problems, 300, "recovery")

    accepted = {expected.root_cause, *expected.acceptable_root_causes}
    row = {
        "scenario": name,
        "started_at": started_at.isoformat(),
        "seconds_to_diagnose": seconds,
        "root_cause": cause,
        "action": action,
        "cause_correct": cause in accepted,
        "action_correct": action == expected.expected_action,
    }
    new = not OUT.exists()
    with OUT.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)
    verdict = "correct" if row["cause_correct"] else f"expected {expected.root_cause}"
    print(f"Recorded: {cause} ({verdict}), {seconds} s. Saved to {OUT.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
