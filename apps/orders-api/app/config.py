from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from ORDERS_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="ORDERS_")

    redis_url: str = "redis://localhost:6379/0"
    # Short timeouts so a dead Redis fails fast instead of hanging requests.
    redis_timeout_seconds: float = 1.0
    log_level: str = "INFO"
    # Git commit this image was built from. Set at build time by CI.
    version: str = "dev"
    # Endpoints used by chaos scenarios to create CPU and memory pressure.
    enable_chaos_endpoints: bool = True
