"""Kubernetes client factory.

The investigation side of the system only ever gets a :class:`ReadOnlyClients` bundle. Each
API object is wrapped in :class:`ReadOnlyApi`, which refuses to expose any method that is not a
``read_*`` / ``list_*`` / ``get_*`` call. That is defence in depth: the real guarantee is the
``monitor-reader`` RBAC Role, but a bug in a tool can't even *attempt* a write.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kubernetes import client, config

from app.core.config import Settings

_READ_METHOD = re.compile(r"^(read|list|get)_")


class ReadOnlyViolationError(PermissionError):
    """Raised when code tries to reach a mutating Kubernetes API method."""


class ReadOnlyApi:
    """Proxy that only forwards read-style methods of a generated kubernetes API class."""

    __slots__ = ("_api",)

    def __init__(self, api: Any) -> None:
        object.__setattr__(self, "_api", api)

    def __getattr__(self, name: str) -> Any:
        if not _READ_METHOD.match(name):
            raise ReadOnlyViolationError(
                f"{type(self._api).__name__}.{name} is not a read-only method"
            )
        return getattr(self._api, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise ReadOnlyViolationError("ReadOnlyApi is immutable")


@dataclass(frozen=True)
class ReadOnlyClients:
    core: Any  # ReadOnlyApi over CoreV1Api
    apps: Any  # ReadOnlyApi over AppsV1Api
    custom: Any  # ReadOnlyApi over CustomObjectsApi
    request_timeout: float

    @classmethod
    def from_api_client(
        cls, api_client: client.ApiClient, request_timeout: float
    ) -> ReadOnlyClients:
        return cls(
            core=ReadOnlyApi(client.CoreV1Api(api_client)),
            apps=ReadOnlyApi(client.AppsV1Api(api_client)),
            custom=ReadOnlyApi(client.CustomObjectsApi(api_client)),
            request_timeout=request_timeout,
        )


def build_api_client(kubeconfig: str | None, context: str | None) -> client.ApiClient:
    """Build an isolated ApiClient (never touches the global default configuration)."""
    if kubeconfig:
        api_client: client.ApiClient = _new_client_from_config(
            config_file=kubeconfig, context=context
        )
        return api_client
    cfg = client.Configuration()
    _load_incluster_config(client_configuration=cfg)
    return client.ApiClient(cfg)


# The kubernetes package is untyped; bind its loaders to typed names once.
_new_client_from_config: Callable[..., Any] = config.new_client_from_config
_load_incluster_config: Callable[..., Any] = config.load_incluster_config


def build_reader_clients(settings: Settings) -> ReadOnlyClients:
    kubeconfig = str(settings.reader_kubeconfig) if settings.reader_kubeconfig else None
    api_client = build_api_client(kubeconfig, settings.reader_kube_context)
    return ReadOnlyClients.from_api_client(api_client, settings.k8s_request_timeout_seconds)
