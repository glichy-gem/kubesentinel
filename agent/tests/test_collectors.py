from datetime import timedelta

from conftest import T0
from fakes import FakeGitHub, FakeKube, FakeLoki, FakePrometheus

from app.collectors import Collector

NS = "/api/v1/namespaces/demo"
APPS = "/apis/apps/v1/namespaces/demo"
ARGO = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications"


def ts(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def pod(name, *, ready=True, restarts=0, waiting=None, last_terminated=None):
    cs = {"name": "orders-api", "ready": ready, "restartCount": restarts, "state": {}}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting, "message": f"{waiting} details"}}
    if last_terminated:
        reason, finished = last_terminated
        cs["lastState"] = {
            "terminated": {"reason": reason, "exitCode": 137, "finishedAt": finished}
        }
    return {
        "metadata": {"name": name},
        "status": {"phase": "Running", "containerStatuses": [cs]},
    }


def deployment(
    name, replicas=2, ready=2, updated=2, image="ghcr.io/x/orders-api:sha-1"
):
    return {
        "spec": {
            "replicas": replicas,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": image,
                            "resources": {"limits": {"memory": "128Mi"}},
                            "env": [
                                {
                                    "name": "ORDERS_REDIS_URL",
                                    "value": "redis://secret@redis",
                                }
                            ],
                        }
                    ]
                }
            },
        },
        "status": {"readyReplicas": ready, "updatedReplicas": updated},
    }


def cluster(pods=(), events=(), history=(), redis=None, orders=None):
    return FakeKube(
        {
            f"{NS}/pods": {"items": list(pods)},
            f"{NS}/events": {"items": list(events)},
            f"{APPS}/deployments/orders-api": orders or deployment("orders-api"),
            f"{APPS}/deployments/redis": redis or deployment("redis", 1, 1, 1),
            f"{ARGO}/orders-api": {"status": {"history": list(history)}},
        }
    )


def collector(kube, prom=None, loki=None, github=None):
    return Collector(
        kube,
        prom or FakePrometheus({}),
        loki or FakeLoki([]),
        github or FakeGitHub({}),
        "owner/gitops",
        {"orders-api": ["redis"]},
    )


def test_recent_oom_kill_is_a_signal_but_an_old_one_is_not():
    recent = cluster(
        [pod("orders-api-a", restarts=2, last_terminated=("OOMKilled", ts(-1)))]
    )
    ev = collector(recent).collect("demo", "orders-api", T0)
    assert ev.signals.oom_killed
    assert "last terminated OOMKilled (exit 137)" in ev.facts[ev_index(ev, "pods")].text

    old = cluster(
        [pod("orders-api-a", restarts=2, last_terminated=("OOMKilled", ts(-180)))]
    )
    assert not collector(old).collect("demo", "orders-api", T0).signals.oom_killed


def ev_index(ev, source):
    return next(i for i, f in enumerate(ev.facts) if f.source == source)


def test_waiting_reasons_become_signals():
    kube = cluster(
        [
            pod("orders-api-a", ready=False, waiting="ImagePullBackOff"),
            pod("orders-api-b", ready=False, waiting="CrashLoopBackOff"),
        ]
    )
    ev = collector(kube).collect("demo", "orders-api", T0)
    assert ev.signals.image_pull_error and ev.signals.crash_looping
    assert ev.signals.pods_not_ready == 2


def test_env_values_are_never_collected():
    ev = collector(cluster()).collect("demo", "orders-api", T0)
    text = " ".join(f.text for f in ev.facts)
    assert "ORDERS_REDIS_URL" in text
    assert "secret@redis" not in text


def test_stuck_rollout_and_dependency_scaled_to_zero():
    kube = cluster(
        orders=deployment("orders-api", replicas=2, ready=2, updated=1),
        redis=deployment("redis", replicas=0, ready=0, updated=0),
    )
    ev = collector(kube).collect("demo", "orders-api", T0)
    assert ev.signals.rollout_stuck
    assert ev.signals.dependency_unavailable
    assert any(
        "redis of orders-api: 0/0 replicas ready (scaled to zero)" in f.text
        for f in ev.facts
    )


def test_events_are_grouped_filtered_by_service_and_time():
    def event(reason, obj, minutes, count=1):
        return {
            "reason": reason,
            "message": f"Back-off restarting failed container in pod {obj}",
            "involvedObject": {"kind": "Pod", "name": obj},
            "lastTimestamp": ts(minutes),
            "count": count,
        }

    kube = cluster(
        events=[
            event("BackOff", "orders-api-abc-1", -2, 3),
            event(
                "BackOff", "orders-api-abc-2", -1, 2
            ),  # same message once pod names are masked
            event("BackOff", "redis-xyz", -1),  # another service
            event("BackOff", "orders-api-abc-3", -60),  # too old
        ]
    )
    ev = collector(kube).collect("demo", "orders-api", T0)
    events = [f.text for f in ev.facts if f.source == "events"]
    assert events == [
        "event BackOff x5 on Pod: Back-off restarting failed container in pod orders-api-*"
    ]


def test_metrics_become_facts_and_signals():
    prom = FakePrometheus(
        {
            "http_request_errors_total": 0.25,
            "histogram_quantile": 1.2,
            "container_memory_working_set_bytes": 120 * 1024 * 1024,
            'resource="memory"': 128 * 1024 * 1024,
            "container_cpu_usage_seconds_total": 0.24,
            'resource="cpu"': 0.25,
            "kube_pod_container_status_restarts_total": 4,
        }
    )
    ev = collector(cluster(), prom=prom).collect("demo", "orders-api", T0)
    s = ev.signals
    assert (s.error_ratio, s.p95_seconds, s.memory_peak_ratio, s.cpu_ratio) == (
        0.25,
        1.2,
        0.938,
        0.96,
    )
    assert s.crash_looping  # 4 restarts in 15 minutes
    assert any("120 MiB = 94% of the 128 MiB limit" in f.text for f in ev.facts)


def test_logs_are_deduplicated_and_flag_connection_and_config_errors():
    line = '{"ts": "%s", "level": "ERROR", "message": "redis error", "error": "ConnectionError"}'
    loki = FakeLoki(
        [({"pod": "orders-api-a"}, line % i) for i in range(4)]
        + [
            (
                {"pod": "orders-api-b"},
                "pydantic_core.ValidationError: 1 validation error for Settings",
            )
        ]
    )
    ev = collector(cluster(), loki=loki).collect("demo", "orders-api", T0)
    logs = [f.text for f in ev.facts if f.source == "logs"]
    assert logs[0].startswith("log x4 from orders-api-a")
    assert ev.signals.connection_errors_in_logs and ev.signals.config_error_in_logs


def test_deploys_before_the_first_alert_are_described_with_their_diff():
    github = FakeGitHub(
        {
            "abc1234567": {
                "commit": {
                    "message": "chore(orders-api): tune redis client timeout\n\nbody",
                    "author": {"name": "Dev"},
                },
                "files": [{"patch": "@@ -1 +1 @@\n-  timeout: 1\n+  value: 1.5s\n"}],
            }
        }
    )
    kube = cluster(
        history=[
            {"revision": "old0000000", "deployedAt": ts(-300)},  # outside the hour
            {"revision": "abc1234567", "deployedAt": ts(-3)},
            {
                "revision": "after00000",
                "deployedAt": ts(+5),
            },  # after the alert: not a suspect
        ]
    )
    ev = collector(kube, github=github).collect("demo", "orders-api", T0)
    deploys = [f.text for f in ev.facts if f.source == "deploys"]
    assert len(deploys) == 1
    assert (
        "deploy abc1234 3.0 min before the first alert: 'chore(orders-api): tune redis client timeout'"
        in deploys[0]
    )
    assert "+ value: 1.5s" in deploys[0]
    assert ev.signals.minutes_since_deploy == 3.0


def test_a_failing_source_is_recorded_and_the_rest_still_run():
    kube = cluster()
    del kube.responses[f"{ARGO}/orders-api"]
    ev = collector(kube).collect("demo", "orders-api", T0)
    assert ev.errors == ["deploys: RuntimeError"]
    assert any(f.source == "deployment" for f in ev.facts)


def test_secrets_in_logs_are_redacted():
    loki = FakeLoki(
        [
            (
                {"pod": "p"},
                "error connecting with password=hunter2 key AIzaSyA1234567890123456789012345678901",
            )
        ]
    )
    ev = collector(cluster(), loki=loki).collect("demo", "orders-api", T0)
    text = next(f.text for f in ev.facts if f.source == "logs")
    assert "hunter2" not in text and "AIza" not in text
    assert "password=[REDACTED]" in text
