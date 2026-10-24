"""Replay the diagnosis step offline on saved eval snapshots (design doc 8.5).

Each snapshot holds exactly what one live investigation saw: the incident, the
facts, the signals and which fact supports each signal. Replaying it under a
different configuration answers "what would the agent have said" without
breaking the cluster again, in seconds instead of hours:

  matcher    signature matcher only (no LLM)
  llm        LLM only: the prompt gets no matcher candidates
  hybrid     matcher candidates + LLM with prompt v1 (what runs live)
  hybrid-v2  the same with prompt v2

Results are cached per (snapshot, configuration) in the output CSV, so a run
that hits the LLM quota can simply be repeated later to fill the gaps.

Run inside the agent's environment (it imports the agent's own code):
  make replay EVAL_ID=<id> CONFIGS=matcher,llm,hybrid,hybrid-v2
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from app.evidence import Evidence, Fact, Signals
from app.llm import GeminiProvider
from app.matcher import load_signatures, match
from app.rca import diagnose, matcher_diagnosis

CONFIGS = {
    "matcher": {"llm": False, "candidates": True, "prompt": "rca_v1"},
    "llm": {"llm": True, "candidates": False, "prompt": "rca_v1"},
    "hybrid": {"llm": True, "candidates": True, "prompt": "rca_v1"},
    "hybrid-v2": {"llm": True, "candidates": True, "prompt": "rca_v2"},
}
FIELDS = [
    "snapshot",
    "scenario",
    "config",
    "prompt_version",
    "expected_root_cause",
    "root_cause",
    "alternatives",
    "confidence",
    "action_type",
    "expected_action",
    "cause_exact",
    "cause_correct",
    "cause_top2",
    "action_correct",
    "source",
    "model",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "evidence_cited",
    "evidence_dropped",
    "notes",
]


def first_snapshot(run: dict) -> dict | None:
    """The first investigation of the run's main incident: what was graded live."""
    mine = sorted(
        (r for r in run["incidents"] if r["incident"]["service"] == "orders-api"),
        key=lambda r: r["incident"]["opened_at"],
    )
    for record in mine[:1]:
        for d in record["diagnoses"]:
            if d.get("snapshot"):
                return d["snapshot"]
    return None


def rebuild(snapshot: dict) -> Evidence:
    return Evidence(
        facts=[Fact(**f) for f in snapshot["facts"]],
        signals=Signals(**snapshot["signals"]),
        support=dict(snapshot["support"]),
        errors=list(snapshot["errors"]),
    )


def replay_one(snapshot: dict, config: str, provider, signatures) -> dict:
    spec = CONFIGS[config]
    evidence = rebuild(snapshot)
    incident = snapshot["incident"]
    deps = snapshot.get("dependencies", [])
    candidates = match(evidence, signatures)
    if not spec["llm"]:
        d = matcher_diagnosis(incident, candidates, deps)
        return {"diagnosis": d, "source": "matcher", "prompt_version": "", "notes": []}
    result = diagnose(
        incident,
        evidence,
        candidates if spec["candidates"] else [],
        provider,
        deps,
        prompt_version=spec["prompt"],
    )
    return {
        "diagnosis": result.diagnosis,
        "source": result.source,
        "prompt_version": result.prompt_version,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "evidence_dropped": len(result.dropped_evidence),
        "notes": result.notes,
    }


def grade_row(expected: dict, d) -> dict:
    accepted = {expected["root_cause"], *expected.get("acceptable_root_causes", [])}
    correct = d.root_cause in accepted
    return {
        "cause_exact": d.root_cause == expected["root_cause"],
        "cause_correct": correct,
        "cause_top2": correct or bool(accepted & set(d.alternatives[:1])),
        "action_correct": d.proposed_action.type == expected["expected_action"],
    }


def check(eval_id: str, floor: float) -> int:
    """CI gate: matcher-only replay of committed snapshots, no LLM, no network.

    A change to the signatures, the collectors' signals or the matcher that makes
    the deterministic diagnosis worse on real recorded incidents fails the PR.
    """
    signatures = load_signatures()
    results = []
    for path in sorted((ROOT / "evals" / "snapshots" / eval_id).glob("*.json")):
        run = json.loads(path.read_text())
        snapshot = first_snapshot(run)
        if snapshot is None:
            continue
        d = replay_one(snapshot, "matcher", None, signatures)["diagnosis"]
        correct = grade_row(run["expected"], d)["cause_correct"]
        results.append(correct)
        if not correct:
            expected = run["expected"]["root_cause"]
            print(f"  wrong: {path.name}: {d.root_cause} (expected {expected})")
    if not results:
        print(f"no snapshots for {eval_id}")
        return 1
    accuracy = sum(results) / len(results)
    print(
        f"matcher accuracy on {eval_id}: {sum(results)}/{len(results)}"
        f" = {accuracy:.1%} (floor {floor:.1%})"
    )
    return 0 if accuracy >= floor else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("eval_id")
    parser.add_argument(
        "--check",
        type=float,
        metavar="FLOOR",
        help="CI mode: matcher-only accuracy must be at least FLOOR (0-1)",
    )
    parser.add_argument("--configs", default="matcher,llm,hybrid,hybrid-v2")
    parser.add_argument(
        "--pause",
        type=float,
        default=6.0,
        help="seconds between LLM calls (free-tier RPM)",
    )
    args = parser.parse_args()
    if args.check is not None:
        return check(args.eval_id, args.check)
    configs = args.configs.split(",")
    unknown = set(configs) - CONFIGS.keys()
    if unknown:
        parser.error(f"unknown configs {sorted(unknown)}")

    snap_dir = ROOT / "evals" / "snapshots" / args.eval_id
    out_file = ROOT / "evals" / "replay" / f"{args.eval_id}.csv"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_file.exists():
        with out_file.open() as f:
            done = {
                (r["snapshot"], r["config"]) for r in csv.DictReader(f) if r["source"]
            }

    key = os.environ.get("KS_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    needs_llm = any(CONFIGS[c]["llm"] for c in configs)
    if needs_llm and not key:
        parser.error("LLM configs need GEMINI_API_KEY (make replay reads it from .env)")
    provider = (
        GeminiProvider(key, ["gemini-flash-latest", "gemini-flash-lite-latest"], "low")
        if key
        else None
    )
    signatures = load_signatures()

    new = not out_file.exists()
    with out_file.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        for path in sorted(snap_dir.glob("*.json")):
            run = json.loads(path.read_text())
            snapshot = first_snapshot(run)
            if snapshot is None:
                continue  # negative scenario or never diagnosed: nothing to replay
            for config in configs:
                if (path.name, config) in done:
                    continue
                out = replay_one(snapshot, config, provider, signatures)
                d = out["diagnosis"]
                row = {
                    "snapshot": path.name,
                    "scenario": run["scenario"],
                    "config": config,
                    "prompt_version": out["prompt_version"],
                    "expected_root_cause": run["expected"]["root_cause"],
                    "root_cause": d.root_cause,
                    "alternatives": " ".join(d.alternatives),
                    "confidence": d.confidence,
                    "action_type": d.proposed_action.type,
                    "expected_action": run["expected"]["expected_action"],
                    **grade_row(run["expected"], d),
                    "source": out["source"],
                    "model": out.get("model", ""),
                    "latency_ms": out.get("latency_ms", 0),
                    "input_tokens": out.get("input_tokens", 0),
                    "output_tokens": out.get("output_tokens", 0),
                    "evidence_cited": len(d.evidence),
                    "evidence_dropped": out.get("evidence_dropped", 0),
                    "notes": " | ".join(out["notes"]),
                }
                # A failed LLM call (quota, outage) is not a result: mark it so the
                # report skips it and the next replay retries it.
                if any(n.startswith("LLM call failed") for n in out["notes"]):
                    row["source"] = ""
                writer.writerow(row)
                f.flush()
                print(
                    f"{path.name:45} {config:10} {d.root_cause:22} {out['source'] or 'FAILED'}"
                )
                if CONFIGS[config]["llm"]:
                    time.sleep(args.pause)
    return 0


if __name__ == "__main__":
    sys.exit(main())
