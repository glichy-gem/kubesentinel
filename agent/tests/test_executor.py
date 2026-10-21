import subprocess
from pathlib import Path

import pytest
from conftest import T0, am_alert, parse
from fakes import FakeKube, FakeNotifier
from test_collectors import APPS, NS, deployment

from app.correlator import Correlator
from app.db import iso
from app.executor import (
    ARGO_APPS,
    Executor,
    GitError,
    GitOpsRepo,
    set_memory_limit,
    set_replicas,
)
from app.policy import load_policy

# The real orders-api manifest from the GitOps repo, trimmed: comments and the
# requests/limits pair are what the editors must get right.
MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders-api
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: orders-api
          # Placeholder name: kustomization.yaml maps it to the GHCR image.
          image: orders-api
          env:
            - name: ORDERS_REDIS_URL
              value: redis://redis:6379/0
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 250m
              memory: 128Mi
          securityContext:
            runAsNonRoot: true
"""


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


# ------------------------------------------------------------- manifest edits


def test_memory_edit_changes_only_the_limit_and_keeps_comments(tmp_path):
    path = tmp_path / "deployment.yaml"
    path.write_text(MANIFEST)
    set_memory_limit(path, "orders-api", "128Mi", "256Mi")
    text = path.read_text()
    assert "memory: 256Mi" in text and "memory: 64Mi" in text  # request untouched
    assert "# Placeholder name" in text
    assert text.replace("memory: 256Mi", "memory: 128Mi") == MANIFEST


def test_edits_refuse_when_git_disagrees_with_the_plan(tmp_path):
    path = tmp_path / "deployment.yaml"
    path.write_text(MANIFEST.replace("memory: 128Mi", "memory: 192Mi"))
    with pytest.raises(GitError, match="expected 128Mi"):
        set_memory_limit(path, "orders-api", "128Mi", "256Mi")
    with pytest.raises(GitError, match="expected 3"):
        set_replicas(path, 3, 4)


def test_replicas_edit(tmp_path):
    path = tmp_path / "deployment.yaml"
    path.write_text(MANIFEST)
    set_replicas(path, 2, 3)
    assert "  replicas: 3\n" in path.read_text()


# ------------------------------------------------------------- real git


@pytest.fixture
def remote(tmp_path) -> Path:
    """A bare repo standing in for kubesentinel-gitops, with a good and a bad commit."""
    bare = tmp_path / "gitops.git"
    git("init", "--bare", "--initial-branch=main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", str(bare), str(seed), cwd=tmp_path)
    git("config", "user.email", "t@example.com", cwd=seed)
    git("config", "user.name", "t", cwd=seed)
    (seed / "apps/orders-api").mkdir(parents=True)
    (seed / "apps/orders-api/deployment.yaml").write_text(MANIFEST)
    git("add", ".", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    (seed / "apps/orders-api/deployment.yaml").write_text(
        MANIFEST.replace("image: orders-api", "image: broken")
    )
    git("commit", "-am", "chore(orders-api): tune things", cwd=seed)
    git("push", "origin", "HEAD:main", cwd=seed)
    return bare


@pytest.fixture
def gitops(tmp_path, remote) -> GitOpsRepo:
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "id_ed25519").write_text("not a real key: local clones do not use ssh\n")
    (keys / "known_hosts").write_text("")
    return GitOpsRepo(str(remote), keys, tmp_path / "work", "KubeSentinel agent")


def remote_file(remote: Path, tmp_path: Path) -> str:
    check = tmp_path / "check"
    git("clone", "-q", str(remote), str(check), cwd=tmp_path)
    return (check / "apps/orders-api/deployment.yaml").read_text()


def test_commit_pushes_an_attributed_change(gitops, remote, tmp_path):
    sha = gitops.commit(
        "fix(orders-api): raise memory limit to 256Mi\n\nKubeSentinel incident #4",
        lambda repo: set_memory_limit(
            repo / "apps/orders-api/deployment.yaml", "orders-api", "128Mi", "256Mi"
        ),
    )
    assert git("rev-parse", "main", cwd=remote) == sha
    assert git("log", "-1", "--format=%an|%s", "main", cwd=remote) == (
        "KubeSentinel agent|fix(orders-api): raise memory limit to 256Mi"
    )
    assert "memory: 256Mi" in remote_file(remote, tmp_path)
    assert (
        gitops.key.stat().st_mode & 0o777
    ) == 0o600  # ssh demands a private key file


def test_revert_undoes_the_suspect_commit(gitops, remote, tmp_path):
    bad = git("rev-parse", "main", cwd=remote)
    gitops.revert(bad, "KubeSentinel incident #7, approved by Shivam")
    message = git("log", "-1", "--format=%B", "main", cwd=remote)
    assert message.startswith('Revert "chore(orders-api): tune things"')
    assert "approved by Shivam" in message
    assert "image: orders-api" in remote_file(remote, tmp_path)


def test_a_change_with_no_diff_is_an_error(gitops):
    with pytest.raises(GitError, match="no diff"):
        gitops.commit("nothing", lambda repo: None)


# ------------------------------------------------------------- executor


class LiveKube(FakeKube):
    """FakeKube whose Argo CD application reports whatever main points at."""

    def __init__(self, responses, remote: Path | None = None):
        super().__init__(responses)
        self.remote = remote

    def get(self, path, **params):
        if path.startswith(ARGO_APPS) and self.remote:
            self.calls.append(path)
            return {
                "status": {
                    "sync": {"revision": git("rev-parse", "main", cwd=self.remote)}
                }
            }
        return super().get(path, **params)


def fast_policy(verify_minutes=0.02):
    policy = load_policy()
    policy.circuit_breaker.verify_minutes = verify_minutes
    return policy


def new_action(db, **overrides) -> int:
    Correlator(db, 10).ingest(parse(am_alert("PodOOMKilled")), T0)
    db.save_diagnosis(
        1,
        {
            "incident_id": 1,
            "created_at": iso(T0),
            "source": "llm",
            "root_cause": "x",
            "confidence": 1,
            "action_type": "x",
            "action_target": "orders-api",
            "prompt_version": "v",
            "model": "m",
            "latency_ms": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "agrees_with_matcher": 1,
            "collect_ms": 1,
            "diagnosis_json": "{}",
            "candidates_json": "[]",
            "signals_json": "{}",
            "notes_json": "[]",
        },
        [],
    )
    record = {
        "incident_id": 1,
        "diagnosis_id": 1,
        "created_at": iso(T0),
        "service": "orders-api",
        "namespace": "demo",
        "type": "restart_pod",
        "target": "orders-api",
        "via": "kubernetes",
        "params_json": '{"pod": "orders-api-bad"}',
        "description": "d",
        "risk": "low",
        "mode": "auto",
        "reason": "r",
        "status": "approved",
        "decided_by": "policy (auto)",
    }
    return db.insert_action({**record, **overrides})


def healthy_cluster(remote=None):
    return LiveKube(
        {f"{APPS}/deployments/orders-api": deployment("orders-api")}, remote
    )


def test_restart_pod_runs_and_recovery_is_verified(db):
    action_id = new_action(db)
    kube, notifier = healthy_cluster(), FakeNotifier()
    Executor(db, kube, None, fast_policy(1), notifier, poll_seconds=0).run(action_id)
    action = db.get_action(action_id)
    assert action["status"] == "recovered"
    assert kube.writes == [("delete", f"{NS}/pods/orders-api-bad")]
    assert notifier.follow_ups == ["executed", "recovered"]


def test_git_action_waits_for_argo_cd_to_sync_the_commit(db, gitops, remote):
    action_id = new_action(
        db,
        type="increase_memory_limit",
        via="git",
        params_json='{"path": "apps/orders-api/deployment.yaml", "container": "orders-api",'
        ' "from": "128Mi", "to": "256Mi"}',
        mode="approval",
        decided_by="Shivam",
    )
    kube = healthy_cluster(remote)
    Executor(db, kube, gitops, fast_policy(1), FakeNotifier(), poll_seconds=0).run(
        action_id
    )
    action = db.get_action(action_id)
    assert action["status"] == "recovered"
    assert action["commit_sha"] == git("rev-parse", "main", cwd=remote)
    assert action["result"].startswith("memory limit 128Mi -> 256Mi")
    # An Argo CD refresh was requested so the change does not wait for the 3-minute poll.
    assert (
        "patch",
        f"{ARGO_APPS}/orders-api",
        {"metadata": {"annotations": {"argocd.argoproj.io/refresh": "normal"}}},
    ) in kube.writes
    message = git("log", "-1", "--format=%B", "main", cwd=remote)
    assert "KubeSentinel incident #1" in message and "approved by Shivam" in message


def test_a_fix_that_does_not_help_trips_the_circuit_breaker(db):
    action_id = new_action(db)
    sick = FakeKube(
        {f"{APPS}/deployments/orders-api": deployment("orders-api", ready=1)}
    )
    notifier = FakeNotifier()
    Executor(db, sick, None, fast_policy(0.005), notifier, poll_seconds=0).run(
        action_id
    )
    assert db.get_action(action_id)["status"] == "not_recovered"
    assert db.automation_paused("orders-api", T0)
    assert notifier.follow_ups[-1] == "not_recovered"


def test_git_action_without_write_access_fails_cleanly(db):
    action_id = new_action(
        db, type="scale_up", via="git", params_json='{"path": "p", "from": 2, "to": 3}'
    )
    notifier = FakeNotifier()
    Executor(db, healthy_cluster(), None, fast_policy(), notifier, poll_seconds=0).run(
        action_id
    )
    action = db.get_action(action_id)
    assert action["status"] == "failed"
    assert "make agent-git-key" in action["result"]
    assert notifier.follow_ups == ["failed"]


def test_only_approved_actions_run_and_only_once(db):
    proposed = new_action(db, status="proposed")
    kube = healthy_cluster()
    executor = Executor(db, kube, None, fast_policy(1), None, poll_seconds=0)
    executor.run(proposed)
    assert kube.writes == [] and db.get_action(proposed)["status"] == "proposed"

    approved = new_action(db)
    executor.run(approved)
    executor.run(approved)  # a second run (double tap) finds it already done
    assert len(kube.writes) == 1


def test_argocd_sync_action(db):
    action_id = new_action(
        db,
        type="restart_dependency",
        target="redis",
        via="argocd",
        params_json='{"application": "redis"}',
    )
    kube = LiveKube(
        {
            f"{APPS}/deployments/redis": deployment("redis", 1, 1, 1),
            f"{APPS}/deployments/orders-api": deployment("orders-api"),
        }
    )
    Executor(db, kube, None, fast_policy(1), None, poll_seconds=0).run(action_id)
    ((verb, path, body),) = kube.writes
    assert (verb, path) == ("patch", f"{ARGO_APPS}/redis")
    assert body["operation"]["sync"] == {"revision": "HEAD"}
    # A dependency fix is only a recovery when the service itself is healthy too.
    assert f"{APPS}/deployments/orders-api" in kube.calls
    assert db.get_action(action_id)["status"] == "recovered"
