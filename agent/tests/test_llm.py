import httpx
import pytest

from app.llm import GeminiProvider, LLMError


def gemini_ok(text='{"ok": true}', version="gemini-x-flash"):
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": text}]}}],
            "modelVersion": version,
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 3},
        },
    )


def provider(handler, models=("main", "lite")) -> tuple[GeminiProvider, list]:
    calls = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    p = GeminiProvider("test-key", list(models), thinking_level="low")
    p.RETRY_DELAY = 0
    p._client = httpx.Client(
        base_url=p.BASE_URL,
        transport=httpx.MockTransport(record),
        headers={"x-goog-api-key": "test-key"},
    )
    return p, calls


def model_calls(calls) -> list[str]:
    return [c.url.path.split("/")[-1].split(":")[0] for c in calls]


def test_success_records_model_version_and_tokens():
    p, calls = provider(lambda r: gemini_ok())
    reply = p.generate_json("system", "prompt", {"type": "OBJECT"})
    assert reply.text == '{"ok": true}'
    assert (reply.model, reply.input_tokens, reply.output_tokens) == (
        "gemini-x-flash",
        11,
        3,
    )
    body = calls[0].read().decode()
    assert '"thinkingLevel": "low"' in body.replace('":"', '": "')
    assert model_calls(calls) == ["main"]


def test_overloaded_main_model_moves_straight_to_the_next_model():
    def handler(request):
        if "/models/main:" in request.url.path:
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return gemini_ok(version="lite-1")

    p, calls = provider(handler)
    assert p.generate_json("s", "p", {}).model == "lite-1"
    assert model_calls(calls) == ["main", "lite"]


def test_rate_limit_is_retried_once_on_the_same_model():
    responses = iter([httpx.Response(429, json={}), gemini_ok(version="main-1")])
    p, calls = provider(lambda r: next(responses))
    assert p.generate_json("s", "p", {}).model == "main-1"
    assert model_calls(calls) == ["main", "main"]


def test_timeout_moves_on_without_retrying():
    def handler(request):
        if "/models/main:" in request.url.path:
            raise httpx.ReadTimeout("slow", request=request)
        return gemini_ok(version="lite-1")

    p, calls = provider(handler)
    assert p.generate_json("s", "p", {}).model == "lite-1"
    assert model_calls(calls) == ["main", "lite"]


def test_client_errors_are_not_retried():
    p, calls = provider(lambda r: httpx.Response(400, json={}), models=("main",))
    with pytest.raises(LLMError, match="main: 400"):
        p.generate_json("s", "p", {})
    assert len(calls) == 1


def test_all_models_busy_raises_without_leaking_the_key():
    p, _ = provider(lambda r: httpx.Response(503, json={}))
    with pytest.raises(LLMError) as err:
        p.generate_json("s", "p", {})
    assert "test-key" not in str(err.value)
    assert str(err.value) == "no model answered (main: 503, lite: 503)"


def test_network_errors_get_one_retry_per_model():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    p, calls = provider(handler)
    with pytest.raises(LLMError, match="main: no response, lite: no response"):
        p.generate_json("s", "p", {})
    assert model_calls(calls) == ["main", "main", "lite", "lite"]


def test_blocked_reply_without_text_is_an_error():
    p, _ = provider(
        lambda r: httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})
    )
    with pytest.raises(LLMError, match="SAFETY"):
        p.generate_json("s", "p", {})
