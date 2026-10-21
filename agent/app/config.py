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
