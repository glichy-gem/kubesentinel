from datetime import timedelta

from conftest import T0, am_alert, parse


def minutes(n: float):
    return T0 + timedelta(minutes=n)


def test_first_alert_opens_an_incident(correlator, db):
    changes = correlator.ingest(parse(am_alert()), minutes(0))
    assert [c.event for c in changes] == ["opened"]
    incident = db.get_incident(changes[0].incident_id)
    assert incident["status"] == "open"
    assert incident["service"] == "orders-api"
    assert incident["title"] == "orders-api PodCrashLooping"
    assert incident["raw_alert_count"] == 1


def test_repeat_notifications_do_not_create_new_alerts(correlator, db):
    for m in (0, 1, 2):
        changes = correlator.ingest(parse(am_alert()), minutes(m))
    incident = db.get_incident(changes[0].incident_id)
    assert incident["raw_alert_count"] == 1
    assert incident["notification_count"] == 3
    assert incident["alerts"][0]["notifications"] == 3
    assert [c.event for c in changes] == ["updated"]


def test_related_alerts_for_one_service_become_one_incident(correlator, db):
    correlator.ingest(parse(am_alert("PodCrashLooping")), minutes(0))
    correlator.ingest(
        parse(am_alert("DeploymentRolloutStuck", severity="warning")), minutes(4)
    )
    correlator.ingest(parse(am_alert("HighErrorRate")), minutes(6))
    incidents = db.list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["raw_alert_count"] == 3


def test_different_services_get_different_incidents(correlator, db):
    correlator.ingest(parse(am_alert(service="orders-api")), minutes(0))
    correlator.ingest(parse(am_alert(service="redis")), minutes(0))
    assert {i["service"] for i in db.list_incidents()} == {"orders-api", "redis"}


def test_same_service_in_another_namespace_is_separate(correlator, db):
    correlator.ingest(parse(am_alert(namespace="demo")), minutes(0))
    correlator.ingest(parse(am_alert(namespace="staging")), minutes(0))
    assert len(db.list_incidents()) == 2


def test_severity_only_goes_up(correlator, db):
    correlator.ingest(
        parse(am_alert("DeploymentRolloutStuck", severity="warning")), minutes(0)
    )
    correlator.ingest(
        parse(am_alert("PodCrashLooping", severity="critical")), minutes(1)
    )
    correlator.ingest(parse(am_alert("HighLatencyP95", severity="warning")), minutes(2))
    assert db.list_incidents()[0]["severity"] == "critical"


def test_incident_resolves_when_all_its_alerts_resolve(correlator, db):
    correlator.ingest(parse(am_alert("A"), am_alert("B")), minutes(0))
    changes = correlator.ingest(parse(am_alert("A", status="resolved")), minutes(3))
    assert [c.event for c in changes] == ["updated"]  # B still firing
    changes = correlator.ingest(parse(am_alert("B", status="resolved")), minutes(4))
    assert [c.event for c in changes] == ["resolved"]
    assert db.list_incidents()[0]["status"] == "resolved"


def test_flapping_alert_reopens_its_incident(correlator, db):
    correlator.ingest(parse(am_alert()), minutes(0))
    correlator.ingest(parse(am_alert(status="resolved")), minutes(5))
    # Same alert fires again a few minutes later: new occurrence, same incident.
    changes = correlator.ingest(parse(am_alert(starts_at=minutes(8))), minutes(8))
    assert [c.event for c in changes] == ["reopened"]
    incidents = db.list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["status"] == "open"
    assert incidents[0]["raw_alert_count"] == 2


def test_incident_closes_after_quiet_period_and_new_alert_opens_a_new_one(
    correlator, db
):
    correlator.ingest(parse(am_alert()), minutes(0))
    correlator.ingest(parse(am_alert(status="resolved")), minutes(5))
    assert correlator.close_stale(minutes(14)) == 0  # 9 quiet minutes: still resolved
    assert correlator.close_stale(minutes(15)) == 1
    changes = correlator.ingest(parse(am_alert(starts_at=minutes(20))), minutes(20))
    assert [c.event for c in changes] == ["opened"]
    assert [i["status"] for i in db.list_incidents()] == ["open", "closed"]


def test_one_payload_with_two_services_reports_both(correlator):
    changes = correlator.ingest(
        parse(am_alert(service="orders-api"), am_alert(service="redis")), minutes(0)
    )
    assert sorted(c.event for c in changes) == ["opened", "opened"]


def test_first_alert_at_tracks_the_earliest_start(correlator, db):
    correlator.ingest(parse(am_alert("A", starts_at=minutes(2))), minutes(3))
    correlator.ingest(parse(am_alert("B", starts_at=minutes(1))), minutes(4))
    incident = db.list_incidents()[0]
    assert incident["first_alert_at"].startswith("2026-10-21T18:01:00")
