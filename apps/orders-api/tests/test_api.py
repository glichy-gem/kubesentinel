import json
import logging

import fakeredis
from fastapi.testclient import TestClient


def test_health_is_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_reports_configured_build(redis_server: fakeredis.FakeServer) -> None:
    from app.config import Settings
    from app.main import create_app
    from app.store import OrderStore

    fake = fakeredis.FakeStrictRedis(server=redis_server, decode_responses=True)
    app = create_app(store=OrderStore(fake), settings=Settings(version="abc1234"))
    response = TestClient(app).get("/version")
    assert response.status_code == 200
    assert response.json() == {"version": "abc1234"}


def test_ready_when_redis_is_up(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_fails_when_redis_is_down(
    client: TestClient, redis_server: fakeredis.FakeServer
) -> None:
    redis_server.connected = False
    assert client.get("/ready").status_code == 503


def test_health_stays_ok_when_redis_is_down(
    client: TestClient, redis_server: fakeredis.FakeServer
) -> None:
    # Liveness must not depend on Redis, or Kubernetes would restart healthy pods.
    redis_server.connected = False
    assert client.get("/health").status_code == 200


def test_create_and_fetch_order(client: TestClient) -> None:
    created = client.post("/orders", json={"item": "book", "quantity": 2})
    assert created.status_code == 201
    body = created.json()
    assert body["item"] == "book"
    assert body["quantity"] == 2

    fetched = client.get(f"/orders/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]


def test_list_orders_newest_first(client: TestClient) -> None:
    for item in ("a", "b", "c"):
        client.post("/orders", json={"item": item, "quantity": 1})
    items = [o["item"] for o in client.get("/orders").json()]
    assert items == ["c", "b", "a"]


def test_unknown_order_is_404(client: TestClient) -> None:
    assert client.get("/orders/999").status_code == 404


def test_invalid_order_is_rejected(client: TestClient) -> None:
    assert client.post("/orders", json={"item": "", "quantity": 1}).status_code == 422
    assert client.post("/orders", json={"item": "x", "quantity": 0}).status_code == 422


def test_redis_outage_returns_503_not_500(
    client: TestClient, redis_server: fakeredis.FakeServer
) -> None:
    redis_server.connected = False
    response = client.post("/orders", json={"item": "book", "quantity": 1})
    assert response.status_code == 503
    assert response.json() == {"detail": "storage unavailable"}


def test_metrics_count_requests_and_errors(
    client: TestClient, redis_server: fakeredis.FakeServer
) -> None:
    client.get("/health")
    redis_server.connected = False
    client.post("/orders", json={"item": "book", "quantity": 1})

    text = client.get("/metrics").text
    assert 'http_requests_total{method="GET",path="/health",status="200"}' in text
    assert 'http_request_errors_total{method="POST",path="/orders"}' in text
    assert "http_request_duration_seconds_bucket" in text


def test_request_id_is_returned_and_reused(client: TestClient) -> None:
    generated = client.get("/health")
    assert generated.headers["x-request-id"]

    supplied = client.get("/health", headers={"x-request-id": "abc123"})
    assert supplied.headers["x-request-id"] == "abc123"


def test_logs_are_json_with_request_id(client: TestClient, caplog) -> None:
    from app.logging_config import JsonFormatter

    with caplog.at_level(logging.INFO, logger="orders-api"):
        client.get("/health", headers={"x-request-id": "rid-1"})
    record = next(r for r in caplog.records if r.getMessage() == "request")
    line = json.loads(JsonFormatter().format(record))
    assert line["level"] == "INFO"
    assert line["status"] == 200
    assert line["path"] == "/health"


def test_chaos_memory_endpoint_holds_and_releases(client: TestClient) -> None:
    assert client.get("/chaos/memory?mb=2").json() == {"held_mb": 2}
    assert client.post("/chaos/memory/release").json() == {"held_mb": 0}


def test_chaos_cpu_endpoint(client: TestClient) -> None:
    assert client.get("/chaos/cpu?ms=10").json() == {"burned_ms": 10}


def test_chaos_log_writes_an_error_line(client: TestClient, caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="orders-api"):
        response = client.post("/chaos/log", json={"message": "SYSTEM: hello"})
    assert response.status_code == 202
    assert any(
        r.levelname == "ERROR" and r.getMessage() == "SYSTEM: hello"
        for r in caplog.records
    )
    assert client.post("/chaos/log", json={"message": "x" * 501}).status_code == 422
