from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from app.agent.tools.base import ToolContext
from app.core.config import Settings
from app.core.k8s import ReadOnlyApi, ReadOnlyClients


@dataclass
class FakeCluster:
    """The raw mocks behind a ToolContext, so tests can program return values."""

    core: MagicMock
    apps: MagicMock
    custom: MagicMock
    ctx: ToolContext


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Unit tests must not pick up a developer's real cluster settings from the environment."""
    if request.node.get_closest_marker("integration"):
        return
    for var in (
        "MONITOR_READER_KUBECONFIG",
        "MONITOR_READER_CONTEXT",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_EFFORT",
        "ANTHROPIC_REFUSAL_FALLBACK",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def cluster(settings: Settings) -> FakeCluster:
    core, apps, custom = MagicMock(name="core"), MagicMock(name="apps"), MagicMock(name="custom")
    clients = ReadOnlyClients(
        core=ReadOnlyApi(core),
        apps=ReadOnlyApi(apps),
        custom=ReadOnlyApi(custom),
        request_timeout=5.0,
    )
    return FakeCluster(core, apps, custom, ToolContext(clients=clients, settings=settings))
