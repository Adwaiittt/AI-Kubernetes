"""Application settings, loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Kubernetes access -------------------------------------------------------------
    # Path to a kubeconfig for the *reader* identity. When unset we fall back to the
    # in-cluster ServiceAccount token. We never fall back to the user's default
    # ~/.kube/config: that is usually cluster-admin and would defeat the RBAC model.
    reader_kubeconfig: Path | None = Field(default=None, alias="MONITOR_READER_KUBECONFIG")
    reader_kube_context: str | None = Field(default=None, alias="MONITOR_READER_CONTEXT")
    k8s_request_timeout_seconds: float = Field(default=10.0, gt=0)

    # --- Tool output limits (protect the model's context window) -------------------------
    tool_output_max_bytes: int = Field(default=24_000, gt=0)
    log_max_lines: int = Field(default=1_000, gt=0)
    log_max_bytes: int = Field(default=16_000, gt=0)
    list_max_items: int = Field(default=100, gt=0)
    events_max_items: int = Field(default=50, gt=0)

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
