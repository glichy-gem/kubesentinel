"""Run approved actions, then check that the service recovered (design doc 6.6).

Git-managed settings (replicas, memory, the deployed version) change only through
a commit to the GitOps repo, which Argo CD then applies: a direct `kubectl patch`
would be reverted by self-heal and leave no audit trail. The only direct cluster
writes are deleting a pod and asking Argo CD to refresh or sync.

All execution runs on one worker thread, so two actions never race.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.clients import KubeClient
from app.db import Database, iso
from app.metrics import ACTIONS
from app.policy import Policy

logger = logging.getLogger("kubesentinel.executor")

ARGO_APPS = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications"


class GitError(Exception):
    pass


class GitOpsRepo:
    """Commits to the GitOps repo with the agent's own deploy key (`make agent-git-key`)."""

    def __init__(self, url: str, key_dir: Path, workdir: Path, author: str) -> None:
        self.url = url
        self.workdir = workdir
        self.author = author
        workdir.mkdir(parents=True, exist_ok=True)
        # ssh refuses keys others can read; the Secret volume is root-owned, so use a private copy.
        self.key = workdir / "id_ed25519"
        shutil.copyfile(key_dir / "id_ed25519", self.key)
        self.key.chmod(0o600)
        self.env = {
            **os.environ,
            "HOME": str(workdir),
            "GIT_SSH_COMMAND": (
                f"ssh -i {self.key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes"
                f" -o UserKnownHostsFile={key_dir / 'known_hosts'}"
            ),
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": "kubesentinel-agent@users.noreply.github.com",
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": "kubesentinel-agent@users.noreply.github.com",
        }

    def _git(self, *args: str, cwd: Path) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(f"git {args[0]} failed: {result.stderr.strip()[-300:]}")
        return result.stdout.strip()

    def commit(self, message: str, change: Callable[[Path], None]) -> str:
        """Clone, apply `change` to the checkout, commit and push. Returns the new commit sha."""
        with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:
            repo = Path(tmp) / "repo"
            self._git(
                "clone",
                "--depth",
                "50",
                "--branch",
                "main",
                self.url,
                str(repo),
                cwd=Path(tmp),
            )
            change(repo)
            if not self._git("status", "--porcelain", cwd=repo):
                raise GitError("the change produced no diff")
            self._git("commit", "-am", message, cwd=repo)
            return self._push(repo)

    def revert(self, revision: str, message_suffix: str) -> str:
        with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:
            repo = Path(tmp) / "repo"
            self._git(
                "clone",
                "--depth",
                "50",
                "--branch",
                "main",
                self.url,
                str(repo),
                cwd=Path(tmp),
            )
            self._git("revert", "--no-edit", revision, cwd=repo)
            subject = self._git("log", "-1", "--format=%B", cwd=repo)
            self._git(
                "commit", "--amend", "-m", f"{subject}\n\n{message_suffix}", cwd=repo
            )
            return self._push(repo)

    def _push(self, repo: Path) -> str:
        # CI and humans push to the same branch: rebase and retry on a race.
        for attempt in (1, 2, 3):
            try:
                self._git("push", "origin", "HEAD:main", cwd=repo)
                return self._git("rev-parse", "HEAD", cwd=repo)
            except GitError:
                if attempt == 3:
                    raise
                time.sleep(attempt * 2)
                self._git("pull", "--rebase", "origin", "main", cwd=repo)
        raise AssertionError("unreachable")


def set_memory_limit(path: Path, container: str, old: str, new: str) -> None:
    """Edit `resources.limits.memory` of one container, keeping the file's comments and layout."""
    lines = path.read_text().splitlines(keepends=True)
    in_container = in_limits = False
    limits_indent = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(rf"-?\s*name:\s*{re.escape(container)}\s*$", stripped):
            in_container = True
        elif in_container and stripped.startswith("limits:"):
            in_limits, limits_indent = True, len(line) - len(line.lstrip())
        elif in_limits:
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= limits_indent:
                in_limits = False
            elif re.match(r"memory:\s*\S+", stripped):
                current = stripped.split(":", 1)[1].strip()
                if current != old:
                    raise GitError(
                        f"memory limit in Git is {current}, expected {old}; not touching it"
                    )
                lines[i] = line.replace(current, new, 1)
                path.write_text("".join(lines))
                return
    raise GitError(f"no resources.limits.memory found for container {container}")


def set_replicas(path: Path, old: int, new: int) -> None:
    text = path.read_text()
    pattern = re.compile(r"^(\s*replicas:\s*)(\d+)\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        raise GitError("no replicas field in the manifest")
    if int(match.group(2)) != old:
        raise GitError(
            f"replicas in Git is {match.group(2)}, expected {old}; not touching it"
        )
    path.write_text(text[: match.start(2)] + str(new) + text[match.end(2) :])


class Executor:
    def __init__(
        self,
        db: Database,
        kube: KubeClient,
        gitops: GitOpsRepo | None,
        policy: Policy,
        notifier,  # app.telegram.Notifier or None
        poll_seconds: float = 15,
    ) -> None:
        self.db = db
        self.kube = kube
        self.gitops = gitops
        self.policy = policy
        self.notifier = notifier
        self.poll_seconds = poll_seconds
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="execute")

    def submit(self, action_id: int):
        return self._pool.submit(self._run_safely, action_id)

    def _run_safely(self, action_id: int) -> None:
        try:
            self.run(action_id)
        except Exception:
            logger.exception(
                "action crashed", extra={"fields": {"action_id": action_id}}
            )
            self.db.transition(
                action_id, ("executing",), "failed", result="internal error"
            )

    def run(self, action_id: int) -> None:
        if not self.db.transition(action_id, ("approved",), "executing"):
            return  # already running, rejected or expired in the meantime
        action = self.db.get_action(action_id)
        incident = self.db.get_incident(action["incident_id"])
        # Re-check the one rule that can change while waiting for a human.
        if action["namespace"] not in self.policy.namespaces:
            self.db.transition(
                action_id, ("executing",), "failed", result="namespace off limits"
            )
            return
        try:
            result, commit = self._execute(action, incident)
        except Exception as exc:  # noqa: BLE001 - report every failure, never retry blindly
            logger.warning(
                "action failed",
                extra={"fields": {"action_id": action_id, "error": str(exc)}},
            )
            self.db.transition(
                action_id,
                ("executing",),
                "failed",
                result=str(exc)[:500],
                executed_at=iso(datetime.now(UTC)),
            )
            ACTIONS.labels(action["type"], "failed").inc()
            if self.notifier:
                self.notifier.follow_up(incident, self.db.get_action(action_id))
            return

        started = time.monotonic()
        self.db.transition(
            action_id,
            ("executing",),
            "executed",
            result=result,
            commit_sha=commit,
            executed_at=iso(datetime.now(UTC)),
        )
        ACTIONS.labels(action["type"], "executed").inc()
        logger.info(
            "action executed",
            extra={"fields": {"action_id": action_id, "result": result}},
        )
        if self.notifier:
            self.notifier.follow_up(incident, self.db.get_action(action_id))

        recovered, detail = self._verify(action, started, commit)
        now = datetime.now(UTC)
        self.db.transition(
            action_id,
            ("executed",),
            "recovered" if recovered else "not_recovered",
            verified_at=iso(now),
            verification=detail,
        )
        ACTIONS.labels(
            action["type"], "recovered" if recovered else "not_recovered"
        ).inc()
        if not recovered:
            # Circuit breaker: a fix that did not help stops automation for this service.
            until = now + timedelta(minutes=self.policy.circuit_breaker.pause_minutes)
            self.db.pause_automation(
                action["service"], until, f"action {action_id} did not help: {detail}"
            )
        if self.notifier:
            self.notifier.follow_up(incident, self.db.get_action(action_id))

    def _execute(self, action: dict, incident: dict) -> tuple[str, str | None]:
        kind, target, ns, params = (
            action["type"],
            action["target"],
            action["namespace"],
            action["params"],
        )
        trailer = (
            f"KubeSentinel incident #{incident['id']} ({incident['title']}), action #{action['id']},"
            f" approved by {action['decided_by']}"
        )
        if action["via"] == "git":
            if self.gitops is None:
                raise GitError("no GitOps write access configured (make agent-git-key)")
            if kind == "rollback":
                sha = self.gitops.revert(params["revision"], trailer)
                result = f"reverted {params['revision'][:7]} as {sha[:7]}"
            elif kind == "increase_memory_limit":
                sha = self.gitops.commit(
                    f"fix({target}): raise memory limit to {params['to']}\n\n{trailer}",
                    lambda repo: set_memory_limit(
                        repo / params["path"],
                        params["container"],
                        params["from"],
                        params["to"],
                    ),
                )
                result = f"memory limit {params['from']} -> {params['to']} in {sha[:7]}"
            elif kind == "scale_up":
                sha = self.gitops.commit(
                    f"fix({target}): scale to {params['to']} replicas\n\n{trailer}",
                    lambda repo: set_replicas(
                        repo / params["path"], params["from"], params["to"]
                    ),
                )
                result = f"replicas {params['from']} -> {params['to']} in {sha[:7]}"
            else:
                raise GitError(f"no git executor for {kind}")
            # Do not wait for Argo CD's 3-minute poll.
            self._argo_patch(
                target,
                {"metadata": {"annotations": {"argocd.argoproj.io/refresh": "normal"}}},
            )
            return result, sha
        if action["via"] == "kubernetes":
            pods = (
                [params["pod"]]
                if "pod" in params
                else [
                    p["metadata"]["name"]
                    for p in self.kube.get(
                        f"/api/v1/namespaces/{ns}/pods",
                        labelSelector=params["selector"],
                    )["items"]
                ]
            )
            for pod in pods:
                self.kube.delete(f"/api/v1/namespaces/{ns}/pods/{pod}")
            return f"deleted pod(s) {', '.join(pods)}", None
        if action["via"] == "argocd":
            self._argo_patch(
                params["application"],
                {
                    "operation": {
                        "initiatedBy": {"username": "kubesentinel-agent"},
                        "sync": {"revision": "HEAD"},
                    }
                },
            )
            return f"Argo CD sync of {params['application']} requested", None
        raise ValueError(f"nothing to execute for {kind}")

    def _argo_patch(self, application: str, body: dict) -> None:
        self.kube.merge_patch(f"{ARGO_APPS}/{application}", body)

    def _verify(
        self, action: dict, started: float, commit: str | None
    ) -> tuple[bool, str]:
        """Recovered = the change is live and every affected deployment is ready, twice in a row.

        For a Git change, "live" means Argo CD has synced that commit: before that,
        the old pods can look perfectly healthy and fake a recovery.
        """
        deadline = started + self.policy.circuit_breaker.verify_minutes * 60
        ns = action["namespace"]
        names = [action["target"]]
        if action["service"] != action["target"]:
            names.append(
                action["service"]
            )  # a dependency fix must also heal the service
        healthy_in_a_row = 0
        detail = "not checked"
        synced = commit is None
        while time.monotonic() < deadline:
            time.sleep(self.poll_seconds)
            if not synced:
                app = self.kube.get(f"{ARGO_APPS}/{action['target']}")
                synced = (
                    app.get("status", {}).get("sync", {}).get("revision", "") == commit
                )
                detail = f"waiting for Argo CD to sync {commit[:7]}"
                if not synced:
                    continue
            states = []
            for name in names:
                dep = self.kube.get(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
                spec, status = dep["spec"].get("replicas", 1), dep.get("status", {})
                ok = (
                    spec > 0
                    and status.get("readyReplicas", 0) == spec
                    and status.get("updatedReplicas", 0) == spec
                    and status.get("unavailableReplicas", 0) == 0
                )
                states.append(
                    (name, ok, f"{status.get('readyReplicas', 0)}/{spec} ready")
                )
            detail = ", ".join(f"{n} {d}" for n, _, d in states)
            healthy_in_a_row = (
                healthy_in_a_row + 1 if all(ok for _, ok, _ in states) else 0
            )
            if healthy_in_a_row >= 2:
                return True, f"{detail} after {time.monotonic() - started:.0f} s"
        return (
            False,
            f"still unhealthy after {self.policy.circuit_breaker.verify_minutes:g} min: {detail}",
        )
