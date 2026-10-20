from kubernetes import client as k

from app.agent.tools.events import GetEventsInput, get_events
from tests.conftest import FakeCluster
from tests.unit import factories as f


def test_events_sorted_newest_first_and_deduplicated(cluster: FakeCluster) -> None:
    cluster.core.list_namespaced_event.return_value = k.CoreV1EventList(
        items=[
            f.event("Scheduled", "Successfully assigned", etype="Normal", first=0, last=0, uid="1"),
            f.event(
                "BackOff", "Back-off restarting failed container", first=2, last=5, count=3, uid="2"
            ),
            # Same object/reason/message re-emitted as a separate Event object.
            f.event(
                "BackOff", "Back-off restarting failed container", first=6, last=9, count=2, uid="3"
            ),
            f.event("OOMKilling", "Memory cgroup out of memory", first=1, last=7, uid="4"),
        ]
    )
    out = get_events(cluster.ctx, GetEventsInput(namespace="demo"))

    assert [e.reason for e in out.events] == ["BackOff", "OOMKilling", "Scheduled"]
    backoff = out.events[0]
    assert backoff.count == 5
    assert backoff.first_seen == f.ts(2).isoformat()
    assert backoff.last_seen == f.ts(9).isoformat()
    assert backoff.object == "Pod/checkout-7d9f-abc"


def test_events_filtered_by_involved_object(cluster: FakeCluster) -> None:
    cluster.core.list_namespaced_event.return_value = k.CoreV1EventList(items=[])
    get_events(cluster.ctx, GetEventsInput(namespace="demo", involved_object="checkout-7d9f-abc"))
    cluster.core.list_namespaced_event.assert_called_once_with(
        "demo", field_selector="involvedObject.name=checkout-7d9f-abc", _request_timeout=5.0
    )


def test_events_capped(cluster: FakeCluster) -> None:
    cluster.ctx.settings.events_max_items = 3
    cluster.core.list_namespaced_event.return_value = k.CoreV1EventList(
        items=[f.event("R", f"message {i}", last=i, uid=str(i)) for i in range(10)]
    )
    out = get_events(cluster.ctx, GetEventsInput(namespace="demo"))
    assert [e.message for e in out.events] == ["message 9", "message 8", "message 7"]
    assert out.omitted == 7


def test_event_without_timestamps_falls_back_to_creation_time(cluster: FakeCluster) -> None:
    ev = f.event("FailedScheduling", "0/2 nodes are available: Insufficient memory")
    ev.first_timestamp = None
    ev.last_timestamp = None
    ev.count = None
    cluster.core.list_namespaced_event.return_value = k.CoreV1EventList(items=[ev])
    out = get_events(cluster.ctx, GetEventsInput(namespace="demo"))
    assert out.events[0].count == 1
    assert out.events[0].last_seen == f.T0.isoformat()
