"""The offline replay rebuilds exactly what the live agent saw. Run with: make evals-test"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import replay
from app.llm import LLMReply

OOM_SNAPSHOT = {
    "incident": {
        "id": 6,
        "namespace": "demo",
        "service": "orders-api",
        "severity": "critical",
        "alerts": [
            {
                "name": "PodOOMKilled",
                "status": "firing",
                "severity": "critical",
                "starts_at": "2026-10-24T17:26:00+00:00",
                "annotations": {"summary": "orders-api was OOMKilled"},
            }
        ],
    },
    "facts": [
        {"id": "E1", "source": "pods", "text": "last terminated OOMKilled (exit 137)"},
        {"id": "E2", "source": "metrics", "text": "peak memory 57% of the limit"},
    ],
    "signals": {"oom_killed": True, "memory_peak_ratio": 0.57},
    "support": {"oom_killed": "E1", "memory_peak_ratio": "E2"},
    "errors": [],
    "dependencies": ["redis"],
    "dropped_evidence": [],
}
EXPECTED = {
    "root_cause": "memory_limit_too_low",
    "acceptable_root_causes": [],
    "expected_action": "increase_memory_limit",
}


class RecordingLLM:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.systems: list[str] = []
        self.prompts: list[str] = []

    def generate_json(self, system, prompt, schema):
        self.systems.append(system)
        self.prompts.append(prompt)
        return LLMReply(json.dumps(self.reply), "fake-1", 5, 100, 20)


def llm_reply(cause="memory_limit_too_low", action="increase_memory_limit"):
    return {
        "summary": "s",
        "root_cause": cause,
        "confidence": 0.8,
        "evidence": ["E1"],
        "alternatives": [],
        "proposed_action": {"type": action, "target": "orders-api", "reason": "r"},
        "needs_human": True,
    }


def test_rebuilt_evidence_lets_the_matcher_cite_the_same_fact():
    out = replay.replay_one(OOM_SNAPSHOT, "matcher", None, replay.load_signatures())
    d = out["diagnosis"]
    assert (d.root_cause, d.evidence) == ("memory_limit_too_low", ["E1"])
    assert replay.grade_row(EXPECTED, d)["cause_exact"]


def test_llm_only_gets_no_matcher_candidates_and_hybrid_does():
    sigs = replay.load_signatures()
    alone, hybrid = RecordingLLM(llm_reply()), RecordingLLM(llm_reply())
    replay.replay_one(OOM_SNAPSHOT, "llm", alone, sigs)
    replay.replay_one(OOM_SNAPSHOT, "hybrid", hybrid, sigs)
    assert "no known signature matched" in alone.prompts[0]
    assert "oom-killed" in hybrid.prompts[0]


def test_prompt_versions_differ_only_in_the_system_prompt():
    sigs = replay.load_signatures()
    v1, v2 = RecordingLLM(llm_reply()), RecordingLLM(llm_reply())
    out1 = replay.replay_one(OOM_SNAPSHOT, "hybrid", v1, sigs)
    out2 = replay.replay_one(OOM_SNAPSHOT, "hybrid-v2", v2, sigs)
    assert v1.prompts == v2.prompts and v1.systems != v2.systems
    assert (out1["prompt_version"], out2["prompt_version"]) == ("rca_v1", "rca_v2")


def test_first_snapshot_is_the_main_incidents_first_diagnosis():
    run = {
        "incidents": [
            {
                "incident": {"service": "redis", "opened_at": "1"},
                "diagnoses": [{"snapshot": {"x": 0}}],
            },
            {
                "incident": {"service": "orders-api", "opened_at": "2"},
                "diagnoses": [{"snapshot": {"x": 1}}, {"snapshot": {"x": 2}}],
            },
        ]
    }
    assert replay.first_snapshot(run) == {"x": 1}
    assert replay.first_snapshot({"incidents": []}) is None
