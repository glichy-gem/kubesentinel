"""In-memory stand-ins for the cluster, Prometheus, Loki, GitHub and the LLM."""

import json

from app.llm import LLMError, LLMReply


class FakeKube:
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, path: str, **params) -> dict:
        self.calls.append(path)
        if path not in self.responses:
            raise RuntimeError(f"404 {path}")
        return self.responses[path]


class FakePrometheus:
    def __init__(self, values: dict[str, float | None]) -> None:
        self.values = values  # substring of the query -> value

    def scalar(self, query: str) -> float | None:
        for needle, value in self.values.items():
            if needle in query:
                return value
        return None


class FakeLoki:
    def __init__(self, lines: list[tuple[dict, str]]) -> None:
        self._lines = lines

    def lines(self, query: str, start_ns: int, limit: int) -> list[tuple[dict, str]]:
        return self._lines


class FakeGitHub:
    def __init__(self, commits: dict[str, dict]) -> None:
        self.commits = commits

    def commit(self, repo: str, sha: str) -> dict:
        return self.commits[sha]


class FakeLLM:
    """Replies with each queued item in turn: a dict (sent as JSON), a raw string, or an exception."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def generate_json(self, system: str, prompt: str, schema: dict) -> LLMReply:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMReply(
            text=text, model="fake-1", latency_ms=5, input_tokens=100, output_tokens=50
        )


def llm_error() -> LLMError:
    return LLMError("Gemini returned HTTP 429")
