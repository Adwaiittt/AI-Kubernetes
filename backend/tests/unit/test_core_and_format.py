from pathlib import Path
from unittest.mock import patch

import pytest
from kubernetes import client as k

from app.agent.tools._format import container_state, cpu_millicores, memory_mib, probe
from app.core import k8s as k8s_mod
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger

KUBECONFIG = """
apiVersion: v1
kind: Config
clusters: [{name: c, cluster: {server: "https://127.0.0.1:6443"}}]
users: [{name: monitor-reader, user: {token: abc}}]
contexts: [{name: monitor-reader, context: {cluster: c, user: monitor-reader}}]
current-context: monitor-reader
"""


def test_build_reader_clients_from_kubeconfig(tmp_path: Path) -> None:
    path = tmp_path / "reader.kubeconfig"
    path.write_text(KUBECONFIG)
    settings = Settings(_env_file=None, MONITOR_READER_KUBECONFIG=path)
    clients = k8s_mod.build_reader_clients(settings)
    assert clients.request_timeout == settings.k8s_request_timeout_seconds
    api_client = clients.core.list_namespaced_pod.__self__.api_client
    assert api_client.configuration.host == "https://127.0.0.1:6443"
    assert api_client.configuration.api_key == {"BearerToken": "Bearer abc"}


def test_build_reader_clients_in_cluster_when_no_kubeconfig() -> None:
    settings = Settings(_env_file=None)
    with patch.object(k8s_mod, "_load_incluster_config") as load:
        k8s_mod.build_reader_clients(settings)
    load.assert_called_once()


@pytest.mark.parametrize(
    ("p", "kind", "detail"),
    [
        (k.V1Probe(tcp_socket=k.V1TCPSocketAction(port=5432)), "tcpSocket", ":5432"),
        # The runtime attribute is `_exec`; the bundled stubs wrongly call it `var_exec`.
        (
            k.V1Probe(_exec=k.V1ExecAction(command=["cat", "/app/ready"])),  # type: ignore[call-arg]
            "exec",
            "cat /app/ready",
        ),
        (k.V1Probe(grpc=k.V1GRPCAction(port=9000)), "grpc", ":9000"),
        (k.V1Probe(), "unknown", ""),
    ],
)
def test_probe_kinds(p: k.V1Probe, kind: str, detail: str) -> None:
    summary = probe(p)
    assert summary is not None
    assert (summary.type, summary.detail) == (kind, detail)


def test_container_state_and_quantities() -> None:
    assert container_state(None) is None
    assert container_state(k.V1ContainerState()) is None
    running = container_state(k.V1ContainerState(running=k.V1ContainerStateRunning()))
    assert running is not None and running.state == "running"
    assert cpu_millicores("1") == 1000.0
    assert cpu_millicores(None) is None
    assert memory_mib("1Gi") == 1024.0
    assert memory_mib(None) is None


def test_logging_configures() -> None:
    configure_logging("DEBUG")
    get_logger("test").info("hello", key="value")
