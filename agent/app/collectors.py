"""Read-only evidence collection for one incident (design doc section 6.3).

Each collector returns a few short facts, not raw dumps, so the prompt stays
small. A collector that fails records the failure and the others carry on: a
partial picture is better than none in the middle of an incident.
"""

import logging
import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

from app.clients import GitHubClient, KubeClient, LokiClient, PrometheusClient
from app.config import AGENT_AUTHOR
from app.evidence import Evidence

logger = logging.getLogger("kubesentinel.collectors")

WINDOW = timedelta(minutes=15)
DEPLOY_WINDOW = timedelta(minutes=60)
LOG_PATTERN = r"(?i)(error|exception|traceback|refused|timed? ?out|killed|invalid)"
CONNECTION_ERRORS = re.compile(
    r"(?i)connection ?(refused|error|reset)|timed? ?out|unreachable"
)
CONFIG_ERRORS = re.compile(
    r"(?i)validationerror|validation error|invalid (config|setting|value)"
)
IMAGE_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def mib(value: float) -> str:
    return f"{value / 1024 / 1024:.0f} MiB"


class Collector:
    def __init__(
        self,
        kube: KubeClient,
        prometheus: PrometheusClient,
        loki: LokiClient,
        github: GitHubClient,
        gitops_repo: str,
        dependencies: dict[str, list[str]],
    ) -> None:
        self.kube = kube
        self.prometheus = prometheus
        self.loki = loki
        self.github = github
        self.gitops_repo = gitops_repo
        self.dependencies = dependencies

    def collect(
        self, namespace: str, service: str, first_alert_at: datetime
    ) -> Evidence:
        evidence = Evidence()
        steps: list[tuple[str, Callable[[], None]]] = [
            ("pods", lambda: self.pods(evidence, namespace, service, first_alert_at)),
            ("deployment", lambda: self.deployment(evidence, namespace, service)),
            (
                "dependencies",
                lambda: self.dependency_status(evidence, namespace, service),
            ),
            (
                "events",
                lambda: self.events(evidence, namespace, service, first_alert_at),
            ),
            ("metrics", lambda: self.metrics(evidence, namespace, service)),
            ("logs", lambda: self.logs(evidence, namespace, service, first_alert_at)),
            ("deploys", lambda: self.deploys(evidence, service, first_alert_at)),
        ]
        for name, step in steps:
            # One broken source must not stop the investigation, whatever it raised.
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "collector failed",
                    extra={"fields": {"collector": name, "error": repr(exc)}},
                )
                evidence.errors.append(f"{name}: {type(exc).__name__}")
        return evidence

    # ------------------------------------------------------------- kubernetes

    def pods(
        self, ev: Evidence, namespace: str, service: str, first_alert_at: datetime
    ) -> None:
        pods = self.kube.get(
            f"/api/v1/namespaces/{namespace}/pods", labelSelector=f"app={service}"
        )
        not_ready = 0
        for pod in pods["items"]:
            name = pod["metadata"]["name"]
            status = pod.get("status", {})
            for cs in status.get("containerStatuses", []):
                parts = [
                    f"pod {name}: phase {status.get('phase')}, ready {cs.get('ready')}",
                    f"restarts {cs.get('restartCount', 0)}",
                ]
                waiting = cs.get("state", {}).get("waiting")
                if waiting:
                    parts.append(
                        f"waiting {waiting.get('reason')}: {waiting.get('message', '')[:160]}"
                    )
                terminated = cs.get("lastState", {}).get("terminated")
                if terminated:
                    parts.append(
                        f"last terminated {terminated.get('reason')} (exit {terminated.get('exitCode')})"
                        f" at {terminated.get('finishedAt')}"
                    )
                fact = ev.add("pods", ", ".join(parts))
                if not cs.get("ready"):
                    not_ready += 1
                reason = (waiting or {}).get("reason")
                if reason == "CrashLoopBackOff":
                    ev.signal("crash_looping", True, fact)
                if reason in IMAGE_PULL_REASONS:
                    ev.signal("image_pull_error", True, fact)
                # lastState lingers for hours; only a recent OOM kill counts.
                finished = parse_time((terminated or {}).get("finishedAt"))
                if (
                    terminated
                    and terminated.get("reason") == "OOMKilled"
                    and finished
                    and finished >= first_alert_at - WINDOW
                ):
                    ev.signal("oom_killed", True, fact)
        if not_ready:
            ev.signals.pods_not_ready = not_ready

    def deployment(self, ev: Evidence, namespace: str, service: str) -> None:
        dep = self.kube.get(
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{service}"
        )
        spec, status = dep["spec"], dep.get("status", {})
        container = spec["template"]["spec"]["containers"][0]
        resources = container.get("resources", {})
        # Names only: env values may be secrets and are never read.
        env_names = [e["name"] for e in container.get("env", [])]
        desired = spec.get("replicas", 1)
        updated = status.get("updatedReplicas", 0)
        fact = ev.add(
            "deployment",
            f"deployment {service}: {status.get('readyReplicas', 0)}/{desired} ready,"
            f" {updated} updated, image {container.get('image')},"
            f" limits {resources.get('limits', {})}, requests {resources.get('requests', {})},"
            f" env names {env_names}",
        )
        if updated < desired:
            ev.signal("rollout_stuck", True, fact)

    def dependency_status(self, ev: Evidence, namespace: str, service: str) -> None:
        for dep_name in self.dependencies.get(service, []):
            dep = self.kube.get(
                f"/apis/apps/v1/namespaces/{namespace}/deployments/{dep_name}"
            )
            desired = dep["spec"].get("replicas", 1)
            ready = dep.get("status", {}).get("readyReplicas", 0)
            note = " (scaled to zero)" if desired == 0 else ""
            fact = ev.add(
                "dependencies",
                f"dependency {dep_name} of {service}: {ready}/{desired} replicas ready{note}",
            )
            if desired == 0 or ready < desired:
                ev.signal("dependency_unavailable", True, fact)

    def events(
        self, ev: Evidence, namespace: str, service: str, first_alert_at: datetime
    ) -> None:
        since = first_alert_at - WINDOW
        items = self.kube.get(
            f"/api/v1/namespaces/{namespace}/events", fieldSelector="type=Warning"
        )["items"]
        grouped: Counter[tuple[str, str, str]] = Counter()
        for item in items:
            obj = item.get("involvedObject", {}).get("name", "")
            if not obj.startswith(service):
                continue
            when = parse_time(item.get("lastTimestamp") or item.get("eventTime"))
            if when and when < since:
                continue
            # Pod names differ per replica; group by reason and message.
            message = re.sub(
                r"\b" + re.escape(service) + r"-[a-z0-9-]+",
                f"{service}-*",
                item.get("message", ""),
            )
            grouped[
                (
                    item.get("reason", ""),
                    message[:200],
                    item["involvedObject"].get("kind", ""),
                )
            ] += item.get("count", 1)
        for (reason, message, kind), count in grouped.most_common(6):
            ev.add("events", f"event {reason} x{count} on {kind}: {message}")

    # ------------------------------------------------------------- metrics and logs

    def metrics(self, ev: Evidence, namespace: str, service: str) -> None:
        sel = f'namespace="{namespace}",service="{service}"'
        csel = f'namespace="{namespace}",container="{service}"'
        q = self.prometheus.scalar

        error_ratio = q(
            f"sum(rate(http_request_errors_total{{{sel}}}[5m])) / sum(rate(http_requests_total{{{sel}}}[5m]))"
        )
        if error_ratio is not None:
            fact = ev.add("metrics", f"5xx error ratio over 5m: {error_ratio:.1%}")
            ev.signal("error_ratio", round(error_ratio, 4), fact)

        p95 = q(
            f"histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{sel}}}[5m])))"
        )
        if p95 is not None:
            fact = ev.add("metrics", f"p95 latency over 5m: {p95:.3f} s")
            ev.signal("p95_seconds", round(p95, 3), fact)

        peak = q(
            f"max(max_over_time(container_memory_working_set_bytes{{{csel}}}[15m]))"
        )
        mem_limit = q(
            f'max(kube_pod_container_resource_limits{{{csel},resource="memory"}})'
        )
        if peak is not None and mem_limit:
            fact = ev.add(
                "metrics",
                f"peak memory over 15m: {mib(peak)} = {peak / mem_limit:.0%} of the {mib(mem_limit)} limit",
            )
            ev.signal("memory_peak_ratio", round(peak / mem_limit, 3), fact)

        cpu = q(f"max(rate(container_cpu_usage_seconds_total{{{csel}}}[2m]))")
        cpu_limit = q(
            f'max(kube_pod_container_resource_limits{{{csel},resource="cpu"}})'
        )
        if cpu is not None and cpu_limit:
            fact = ev.add(
                "metrics",
                f"CPU per pod (max): {cpu:.3f} cores = {cpu / cpu_limit:.0%} of the {cpu_limit:g}-core limit",
            )
            ev.signal("cpu_ratio", round(cpu / cpu_limit, 3), fact)

        restarts = q(
            f"sum(increase(kube_pod_container_status_restarts_total{{{csel}}}[15m]))"
        )
        if restarts is not None:
            fact = ev.add("metrics", f"container restarts over 15m: {restarts:.0f}")
            ev.signal("restarts_15m", round(restarts, 1), fact)
            if restarts >= 3:
                ev.signal("crash_looping", True, fact)

    def logs(
        self, ev: Evidence, namespace: str, service: str, first_alert_at: datetime
    ) -> None:
        start = first_alert_at - WINDOW
        lines = self.loki.lines(
            f'{{namespace="{namespace}", app="{service}"}} |~ "{LOG_PATTERN}"',
            int(start.timestamp() * 1e9),
            limit=200,
        )
        # Repeated lines are one fact with a count; most frequent first.
        counts: Counter[str] = Counter()
        pods: dict[str, str] = {}
        for labels, line in lines:
            key = re.sub(r'"(ts|request_id|duration_ms)": [^,}]+', "", line)[:240]
            counts[key] += 1
            pods.setdefault(key, labels.get("pod", "?"))
        for key, count in counts.most_common(5):
            fact = ev.add("logs", f"log x{count} from {pods[key]}: {key}")
            if CONNECTION_ERRORS.search(key):
                ev.signal("connection_errors_in_logs", True, fact)
            if CONFIG_ERRORS.search(key):
                ev.signal("config_error_in_logs", True, fact)

    # ------------------------------------------------------------- change history

    def deploys(self, ev: Evidence, service: str, first_alert_at: datetime) -> None:
        app = self.kube.get(
            f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/{service}"
        )
        history = app.get("status", {}).get("history", [])
        recent = [
            h
            for h in history
            if (at := parse_time(h.get("deployedAt")))
            and first_alert_at - DEPLOY_WINDOW <= at <= first_alert_at
        ]
        if not recent:
            ev.add(
                "deploys", f"no deploys of {service} in the hour before the first alert"
            )
            return
        for entry in sorted(recent, key=lambda h: h["deployedAt"], reverse=True)[:3]:
            revision = entry.get("revision", "")
            deployed_at = parse_time(entry["deployedAt"])
            minutes = (first_alert_at - deployed_at).total_seconds() / 60
            commit = self.github.commit(self.gitops_repo, revision)
            message = commit["commit"]["message"].splitlines()[0]
            author = commit["commit"]["author"]["name"]
            changes = [
                line
                for f in commit.get("files", [])
                for line in f.get("patch", "").splitlines()
                if line[:1] in "+-" and line[:3] not in ("+++", "---")
            ][:6]
            # Found live: the agent proposed rolling back its own approved fix. Its
            # deploys stay visible but are never the suspect; if a fix makes things
            # worse, the circuit breaker handles it, not another rollback.
            by_agent = author == AGENT_AUTHOR
            fact = ev.add(
                "deploys",
                f"deploy {revision[:7]} {minutes:.1f} min before the first alert: '{message}' by {author}"
                f"{' (a KubeSentinel remediation, not a release)' if by_agent else ''};"
                f" changed lines: {' | '.join(changes) or 'none shown'}",
            )
            if by_agent:
                continue
            if ev.signals.minutes_since_deploy is None:  # newest first
                ev.signal("minutes_since_deploy", round(minutes, 1), fact)
                ev.signal("suspect_revision", revision, fact)
                ev.signal("suspect_message", message[:120], fact)
