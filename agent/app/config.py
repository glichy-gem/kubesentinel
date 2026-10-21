from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from KS_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="KS_")

    db_path: str = "kubesentinel.db"
    log_level: str = "INFO"
    # Git commit this image was built from. Set at build time by CI.
    version: str = "dev"
    # A resolved incident stays resolved (and reopens if its alerts return) for
    # this long, then closes. Matches "close after 10 quiet minutes" in the design.
    quiet_minutes_to_close: int = 10

    # Investigation. Empty k8s_url means "in cluster, as the pod's ServiceAccount";
    # locally, point it at `kubectl proxy` (http://127.0.0.1:8001).
    investigate: bool = True
    investigate_delay_seconds: float = 20
    k8s_url: str = ""
    prometheus_url: str = (
        "http://kps-kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
    )
    loki_url: str = "http://loki.monitoring.svc.cluster.local:3100"
    gitops_repo: str = "glichy-gem/kubesentinel-gitops"
    # Small service map: what each service needs to work. JSON in the env var.
    dependencies: dict[str, list[str]] = {"orders-api": ["redis"]}

    # LLM. Without a key the agent still diagnoses, using the signature matcher only.
    gemini_api_key: SecretStr | None = None
    # Tried in order: the free tier often rejects the main model under load ("high demand").
    gemini_models: list[str] = ["gemini-flash-latest", "gemini-flash-lite-latest"]
    gemini_thinking_level: str = "low"  # empty = the model's default

    # Remediation. Without Telegram there is no way to approve, so approval-mode
    # actions simply wait and expire; auto actions still run within the policy.
    remediate: bool = True
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str = ""
    # GitOps write access: the deploy key from `make agent-git-key`, mounted here.
    # Without it, Git-based actions (rollback, memory, scale) fail and are reported.
    gitops_ssh_url: str = "git@github.com:glichy-gem/kubesentinel-gitops.git"
    git_key_dir: str = "/secrets/git"
    workdir: str = "/tmp/kubesentinel"
