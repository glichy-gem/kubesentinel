"""Thin read-only HTTP clients for the systems the collectors query.

Plain httpx instead of vendor SDKs: the agent needs a handful of GET calls, and
the Kubernetes and Google SDKs alone would add more memory than the whole agent.
"""

import math
from pathlib import Path
from typing import Any

import httpx

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class KubeClient:
    """Kubernetes API access as the pod's ServiceAccount, or via `kubectl proxy` locally."""

    def __init__(self, base_url: str = "", timeout: float = 10) -> None:
        self._in_cluster = not base_url
        if self._in_cluster:
            self._client = httpx.Client(
                base_url="https://kubernetes.default.svc",
                verify=str(SA_DIR / "ca.crt"),
                timeout=timeout,
            )
        else:
            self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        if not self._in_cluster:
            return {}
        # Projected tokens rotate, so read the file on every call.
        return {"Authorization": f"Bearer {(SA_DIR / 'token').read_text().strip()}"}

    def get(self, path: str, **params: Any) -> dict:
        response = self._client.get(path, params=params, headers=self._headers())
        response.raise_for_status()
        return response.json()

    # The only writes, and RBAC allows exactly these: delete pods in demo,
    # patch Argo CD Applications (refresh / sync). Everything else goes through Git.

    def delete(self, path: str) -> None:
        response = self._client.delete(path, headers=self._headers())
        response.raise_for_status()

    def merge_patch(self, path: str, body: dict) -> dict:
        response = self._client.patch(
            path,
            json=body,
            headers={**self._headers(), "Content-Type": "application/merge-patch+json"},
        )
        response.raise_for_status()
        return response.json()


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 10) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def scalar(self, query: str) -> float | None:
        """Run an instant query that returns at most one series; None when empty."""
        response = self._client.get("/api/v1/query", params={"query": query})
        response.raise_for_status()
        result = response.json()["data"]["result"]
        if not result:
            return None
        value = float(result[0]["value"][1])
        return None if math.isnan(value) else value  # NaN (0/0) means "no data"


class LokiClient:
    def __init__(self, base_url: str, timeout: float = 10) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def lines(self, query: str, start_ns: int, limit: int) -> list[tuple[dict, str]]:
        """Newest first: (stream labels, log line)."""
        response = self._client.get(
            "/loki/api/v1/query_range",
            params={
                "query": query,
                "start": start_ns,
                "limit": limit,
                "direction": "backward",
            },
        )
        response.raise_for_status()
        out = []
        for stream in response.json()["data"]["result"]:
            out.extend((stream["stream"], line) for _, line in stream["values"])
        return out


class GitHubClient:
    """Unauthenticated: the GitOps repo is public and the agent makes few calls."""

    def __init__(self, timeout: float = 10) -> None:
        self._client = httpx.Client(
            base_url="https://api.github.com",
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )

    def commit(self, repo: str, sha: str) -> dict:
        response = self._client.get(f"/repos/{repo}/commits/{sha}")
        response.raise_for_status()
        return response.json()
