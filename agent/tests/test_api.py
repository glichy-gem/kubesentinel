from conftest import am_alert, am_payload


def test_health_ready_version(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/version").json() == {"version": "dev"}


def test_alertmanager_webhook_opens_an_incident(client):
    response = client.post("/alerts", json=am_payload(am_alert()))
    assert response.status_code == 200
    body = response.json()
    assert body["alerts"] == 1
    assert body["incidents"][0]["event"] == "opened"

    incident_id = body["incidents"][0]["id"]
    incident = client.get(f"/incidents/{incident_id}").json()
    assert incident["status"] == "open"
    assert incident["alerts"][0]["name"] == "PodCrashLooping"
    assert incident["alerts"][0]["labels"]["service"] == "orders-api"


def test_list_incidents_filters_by_status(client):
    client.post("/alerts", json=am_payload(am_alert(service="a")))
    client.post("/alerts", json=am_payload(am_alert(service="b")))
    client.post("/alerts", json=am_payload(am_alert(service="b", status="resolved")))
    assert [i["service"] for i in client.get("/incidents?status=open").json()] == ["a"]
    assert [i["service"] for i in client.get("/incidents?status=resolved").json()] == [
        "b"
    ]
    assert client.get("/incidents?status=bogus").status_code == 422


def test_invalid_payload_is_rejected(client):
    assert client.post("/alerts", json={"alerts": "nope"}).status_code == 422
    bad = am_payload(am_alert())
    del bad["alerts"][0]["fingerprint"]
    assert client.post("/alerts", json=bad).status_code == 422


def test_unknown_incident_is_404(client):
    assert client.get("/incidents/999").status_code == 404


def test_metrics_count_alerts_and_incidents(client):
    client.post("/alerts", json=am_payload(am_alert()))
    text = client.get("/metrics").text
    assert 'kubesentinel_alerts_received_total{status="firing"}' in text
    assert 'kubesentinel_incidents{status="open"} 1.0' in text
