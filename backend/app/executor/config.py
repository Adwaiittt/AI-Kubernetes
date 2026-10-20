"""Executor settings. Separate from the backend's: the executor runs as its own process/pod."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.executor.validation import ExecutorPolicy


class ExecutorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Kubeconfig for the monitor-executor identity; unset = in-cluster ServiceAccount token.
    kubeconfig: Path | None = Field(default=None, alias="EXECUTOR_KUBECONFIG")
    kube_context: str | None = Field(default=None, alias="EXECUTOR_KUBE_CONTEXT")
    request_timeout_seconds: float = Field(default=10.0, gt=0, alias="EXECUTOR_REQUEST_TIMEOUT")

    allowed_namespaces: str = Field(default="demo", alias="EXECUTOR_ALLOWED_NAMESPACES")
    max_memory: str = Field(default="4Gi", alias="EXECUTOR_MAX_MEMORY")
    max_cpu: str = Field(default="4", alias="EXECUTOR_MAX_CPU")
    min_replicas: int = Field(default=1, ge=0, alias="EXECUTOR_MIN_REPLICAS")
    max_replicas: int = Field(default=20, ge=1, alias="EXECUTOR_MAX_REPLICAS")

    api_key: SecretStr | None = Field(default=None, alias="EXECUTOR_API_KEY")
    approval_signing_key: SecretStr | None = Field(default=None, alias="APPROVAL_SIGNING_KEY")
    database_url: str = Field(default="sqlite:///./executor.db", alias="EXECUTOR_DATABASE_URL")

    def policy(self) -> ExecutorPolicy:
        namespaces = frozenset(n.strip() for n in self.allowed_namespaces.split(",") if n.strip())
        return ExecutorPolicy(
            allowed_namespaces=namespaces,
            max_memory=self.max_memory,
            max_cpu=self.max_cpu,
            min_replicas=self.min_replicas,
            max_replicas=self.max_replicas,
        )
