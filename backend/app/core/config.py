"""Application settings, loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


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

    # --- Claude agent -----------------------------------------------------------------------
    # Secrets come only from the environment (or an untracked .env); never hardcoded.
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str | None = Field(default=None, alias="ANTHROPIC_MODEL")
    anthropic_effort: Effort = Field(default="medium", alias="ANTHROPIC_EFFORT")
    # Server-side refusal fallback (`fallbacks: "default"`); Claude API only.
    anthropic_refusal_fallback: bool = Field(default=True, alias="ANTHROPIC_REFUSAL_FALLBACK")

    agent_max_tool_calls: int = Field(default=15, gt=0)
    agent_max_turns: int = Field(default=25, gt=0)  # hard stop on model round-trips
    agent_tool_timeout_seconds: float = Field(default=20.0, gt=0)
    agent_token_budget: int = Field(default=600_000, gt=0)  # all input + output tokens
    agent_max_tokens: int = Field(default=16_000, gt=0)  # per model response
    agent_report_retries: int = Field(default=1, ge=0)

    # --- Approval gate / executor --------------------------------------------------------
    executor_url: str = Field(default="http://localhost:8001", alias="EXECUTOR_URL")
    executor_api_key: SecretStr | None = Field(default=None, alias="EXECUTOR_API_KEY")
    approval_signing_key: SecretStr | None = Field(default=None, alias="APPROVAL_SIGNING_KEY")
    approval_token_ttl_seconds: int = Field(default=300, gt=0)
    rollout_wait_seconds: float = Field(default=60.0, ge=0)

    # --- HTTP API -----------------------------------------------------------------------
    # Approvers as "name:key" pairs, comma-separated. The name is recorded in the audit log.
    approver_api_keys: SecretStr | None = Field(default=None, alias="APPROVER_API_KEYS")
    cors_origins: str = Field(default="http://localhost:3000", alias="CORS_ORIGINS")
    max_concurrent_investigations: int = Field(default=3, gt=0)
    sse_keepalive_seconds: float = Field(default=15.0, gt=0)

    def approvers(self) -> dict[str, str]:
        """{api_key: approver_name}. Malformed entries and keys under 16 chars are rejected."""
        if self.approver_api_keys is None:
            return {}
        out: dict[str, str] = {}
        for entry in self.approver_api_keys.get_secret_value().split(","):
            if not entry.strip():
                continue
            name, sep, key = entry.strip().partition(":")
            if not sep or not name or len(key) < 16:
                raise ValueError(
                    "APPROVER_API_KEYS entries must be name:key with keys of 16+ characters"
                )
            out[key] = name
        return out

    # --- Storage ------------------------------------------------------------------------------
    database_url: str = Field(default="sqlite:///./k8s_ai_monitor.db", alias="DATABASE_URL")

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
