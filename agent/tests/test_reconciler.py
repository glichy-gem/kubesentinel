from datetime import timedelta

from conftest import T0, am_alert, parse

from app.db import iso
from app.reconciler import Reconciler


def minutes(n: float):
    return T0 + timedelta(minutes=n)


def key(name="PodCrashLooping", starts_at=T0, namespace="demo", service="orders-api"):
    return (f"fp-{name}-{namespace}-{service}", iso(starts_at))


def test_lost_resolution_is_repaired_and_the_next_failure_gets_its_own_incident(
    correlator, db
):
    # The live sequence: an alert fires, its "resolved" webhook is lost during a
    # restart, and a later, unrelated failure would be swallowed by the zombie.
    correlator.ingest(parse(am_alert("PodCrashLooping")), minutes(0))
    assert correlator.reconcile(set(), minutes(30)) == [1]
    assert db.get_incident(1)["status"] == "resolved"

    changes = correlator.ingest(
        parse(am_alert("PodOOMKilled", starts_at=minutes(31))), minutes(31)
    )
    assert [c.event for c in changes] == ["opened"]


def test_alerts_still_active_in_alertmanager_stay_firing(correlator, db):
    correlator.ingest(parse(am_alert("PodCrashLooping")), minutes(0))
    assert correlator.reconcile({key()}, minutes(30)) == []
    assert db.get_incident(1)["status"] == "open"


def test_fresh_alerts_are_not_reconciled_away(correlator, db):
    # Alertmanager may not list an alert yet when its webhook has just arrived.
    correlator.ingest(parse(am_alert("PodCrashLooping")), minutes(0))
    assert correlator.reconcile(set(), minutes(1)) == []
    assert db.get_incident(1)["status"] == "open"


def test_one_of_two_alerts_resolving_keeps_the_incident_open(correlator, db):
    correlator.ingest(parse(am_alert("A"), am_alert("B")), minutes(0))
    assert correlator.reconcile({key("A")}, minutes(10)) == []
    alerts = {a["name"]: a["status"] for a in db.get_incident(1)["alerts"]}
    assert alerts == {"A": "firing", "B": "resolved"}


def test_recurrence_of_a_known_alert_is_counted(correlator):
    correlator.ingest(parse(am_alert("PodOOMKilled")), minutes(0))
    changes = correlator.ingest(
        parse(am_alert("PodOOMKilled", starts_at=minutes(40))), minutes(40)
    )
    assert changes[0].recurrences == 1
    assert changes[0].new_symptoms == ()
    repeat = correlator.ingest(
        parse(am_alert("PodOOMKilled", starts_at=minutes(40))), minutes(41)
    )
    assert (
        repeat[0].recurrences == 0
    )  # same occurrence again: just a repeat notification


class FakeAlertmanager:
    def __init__(self, alerts=(), fail=False):
        self.alerts, self.fail = list(alerts), fail

    def active_alerts(self):
        if self.fail:
            raise ConnectionError("alertmanager down")
        return self.alerts


def test_reconciler_reads_alertmanager_format(correlator, db):
    correlator.ingest(parse(am_alert("PodCrashLooping")), minutes(-30))
    active = [
        {
            "fingerprint": "fp-PodCrashLooping-demo-orders-api",
            "startsAt": "2026-10-21T18:00:00Z",
        }
    ]
    changed = []
    reconciler = Reconciler(
        FakeAlertmanager(active), correlator, 60, lambda: changed.append(1)
    )
    assert reconciler.run_once() == []  # same alert, same start: still firing
    reconciler.alertmanager = FakeAlertmanager([])
    assert reconciler.run_once() == [1]
    assert changed == [1]


def test_unreachable_alertmanager_resolves_nothing(correlator, db):
    correlator.ingest(parse(am_alert("PodCrashLooping")), minutes(-30))
    reconciler = Reconciler(FakeAlertmanager(fail=True), correlator, 0.01)
    reconciler.start()
    reconciler._stop.wait(0.1)
    reconciler.stop()
    assert db.get_incident(1)["status"] == "open"
