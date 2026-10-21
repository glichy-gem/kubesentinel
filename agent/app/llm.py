"""LLM providers behind one small interface (ADR 3), so Gemini can be swapped out.

The provider only turns a prompt into JSON text. Validation, citation checks and
fallbacks live in rca.py and do not depend on which model answered.
"""

import time
from dataclasses import dataclass
from typing import Protocol

import httpx

# Worth one more try on the same model. 503 ("high demand") is not on the list:
# that model rarely recovers within seconds, so the next model is tried instead.
RETRYABLE = {429, 500, 502, 504}


@dataclass(frozen=True)
class LLMReply:
    text: str
    model: str  # the exact model version that answered, for evals
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0


class LLMError(Exception):
    pass


class LLMProvider(Protocol):
    def generate_json(self, system: str, prompt: str, schema: dict) -> LLMReply: ...


class GeminiProvider:
    """Gemini over its REST API with JSON-mode output constrained by a schema.

    Tries each model in order. The free tier often answers "high demand" (503)
    for the main model while a lighter one is free: a 503 or a timeout moves
    straight to the next model, other transient errors get one retry, and when
    every model has failed the caller falls back to the rule matcher.
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
    RETRY_DELAY = 3  # seconds before retrying the same model once

    def __init__(
        self,
        api_key: str,
        models: list[str],
        thinking_level: str = "",
        timeout: float = 25,
    ) -> None:
        if not models:
            raise ValueError("at least one model is required")
        self.models = models
        # "low" keeps a short, well-structured task from spending ~40 s thinking.
        self.thinking_level = thinking_level
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            timeout=timeout,
            headers={"x-goog-api-key": api_key},
        )

    def generate_json(self, system: str, prompt: str, schema: dict) -> LLMReply:
        generation_config = {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        }
        if self.thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        started = time.perf_counter()
        failures = []
        for model in self.models:
            response = self._post(model, body)
            if response is not None and response.status_code == 200:
                return self._reply(response.json(), model, started)
            # Never include the request (it carries the key); the status is enough.
            status = response.status_code if response is not None else "no response"
            failures.append(f"{model}: {status}")
        raise LLMError(f"no model answered ({', '.join(failures)})")

    def _post(self, model: str, body: dict) -> httpx.Response | None:
        response = None
        for delay in (0, self.RETRY_DELAY):
            time.sleep(delay)
            try:
                response = self._client.post(
                    f"/models/{model}:generateContent", json=body
                )
            except httpx.TimeoutException:
                return None  # a slow model will not be faster on a second try
            except httpx.HTTPError:
                continue
            if response.status_code not in RETRYABLE:
                break
        return response

    @staticmethod
    def _reply(data: dict, model: str, started: float) -> LLMReply:
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            reason = data.get("candidates", [{}])[0].get(
                "finishReason", "no candidates"
            )
            raise LLMError(f"{model} returned no text ({reason})") from exc
        usage = data.get("usageMetadata", {})
        return LLMReply(
            text=text,
            model=data.get("modelVersion", model),
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=usage.get("promptTokenCount", 0),
            output_tokens=usage.get("candidatesTokenCount", 0),
        )
