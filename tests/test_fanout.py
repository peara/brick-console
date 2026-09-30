"""Tests for :mod:`brick_console.fanout` — the consolidated fan-out
discipline (one Done-when checkbox each; the module absorbed the three
former per-module copies: the manager's ``_safe_call``, the gateway's
``_guarded_call``, the adapter's ``_safe``).

Pure unit tests: no BLE, no hub, no asyncio — the guarantees here are the
ones the existing suite pinned on the old copies (adapter stdout dispatch
survives a ``boom`` listener; manager state loop survives a raising state
listener; ws mock fan-out is guarded), now pinned once at the source.
"""

from __future__ import annotations

import logging

from brick_console.events import Battery, HubInfo, Imu, TelemetryEvent
from brick_console.fanout import (
    TelemetryRouter,
    fan_out,
    guarded_call,
    subscribe_into,
)
from brick_console.store import TelemetryStore


def boom(*args: object) -> None:
    raise RuntimeError("listener exploded")


# ---------------------------------------------------------------------------
# guarded_call
# ---------------------------------------------------------------------------


def test_guarded_call_invokes_listener() -> None:
    calls: list[tuple[int, ...]] = []
    guarded_call(lambda a, b: calls.append((a, b)), 1, 2)
    assert calls == [(1, 2)]


def test_guarded_call_swallows_and_logs(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="brick_console.fanout"):
        guarded_call(boom, "payload")
    records = [r for r in caplog.records if r.exc_info is not None]
    assert len(records) == 1
    assert "event listener failed" in records[0].getMessage()


def test_guarded_call_returns_none_on_success_and_failure() -> None:
    assert guarded_call(lambda: None) is None
    assert guarded_call(boom) is None


# ---------------------------------------------------------------------------
# fan_out
# ---------------------------------------------------------------------------


def test_fan_out_preserves_order_across_failure() -> None:
    calls: list[str] = []

    def first(value: str) -> None:
        calls.append(f"first:{value}")

    fan_out([first, boom, first], "x")
    assert calls == ["first:x", "first:x"]


def test_fan_out_snapshots_registry_before_iterating() -> None:
    listeners: list = []

    def mutator(value: str) -> None:
        listeners.clear()  # mid-dispatch unsubscribe

    listeners.append(mutator)
    listeners.append(lambda value: listeners.append("delivered"))
    fan_out(listeners, "x")
    assert "delivered" in listeners


def test_fan_out_empty_registry_is_a_no_op() -> None:
    fan_out([], "anything")


# ---------------------------------------------------------------------------
# subscribe_into
# ---------------------------------------------------------------------------


def test_subscribe_into_appends_and_unsubscribes() -> None:
    listeners: list[str] = []
    unsubscribe = subscribe_into(listeners, "listener")
    assert listeners == ["listener"]
    unsubscribe()
    assert listeners == []


def test_subscribe_into_unsubscribe_is_idempotent() -> None:
    listeners: list[str] = []
    unsubscribe = subscribe_into(listeners, "listener")
    unsubscribe()
    unsubscribe()
    assert listeners == []


def test_subscribe_into_keeps_other_listeners() -> None:
    listeners: list[str] = []
    unsubscribe = subscribe_into(listeners, "first")
    subscribe_into(listeners, "second")
    unsubscribe()
    assert listeners == ["second"]


# ---------------------------------------------------------------------------
# TelemetryRouter — the D7 raw-log-primary pipeline
# ---------------------------------------------------------------------------


def make_router() -> tuple[
    TelemetryRouter, TelemetryStore, list[bytes], list[TelemetryEvent]
]:
    """A router over a real store and live registries (shared lists)."""
    store = TelemetryStore()
    raw_listeners: list[bytes] = []
    telemetry_listeners: list[TelemetryEvent] = []
    router = TelemetryRouter(
        store,
        raw_listeners=raw_listeners,
        telemetry_listeners=telemetry_listeners,
    )
    return router, store, raw_listeners, telemetry_listeners


def test_router_routes_raw_first_then_event_in_order() -> None:
    router, store, raw_listeners, telemetry_listeners = make_router()
    seen: list[str] = []

    raw_listeners.append(lambda raw: seen.append(f"raw:{raw!r}"))
    telemetry_listeners.append(lambda event: seen.append(f"event:{event!r}"))

    event = Battery(voltage_mv=8000, current_ma=100, percent=90)
    raw = b'{"t":"battery"}'
    router.route([(raw, event)])

    assert seen == [f"raw:{raw!r}", f"event:{event!r}"]
    assert store.replay_raw_lines(10) == [raw]
    assert [e for _, e in store.replay_events(10)] == [event]


def test_router_raw_path_includes_malformed_events_none() -> None:
    router, store, raw_listeners, telemetry_listeners = make_router()
    raw_seen: list[bytes] = []

    raw_listeners.append(raw_seen.append)
    telemetry_listeners.append(
        lambda event: (_ for _ in ()).throw(
            AssertionError("malformed lines must not reach the telemetry fan-out")
        )
    )

    router.route([(b"garbage not json", None)])

    assert raw_seen == [b"garbage not json"]
    assert store.replay_raw_lines(10) == [b"garbage not json"]
    assert store.event_count == 0


def test_router_caches_hub_info_only_for_hub_info() -> None:
    router, store, *_ = make_router()
    hub_info = HubInfo(name="Pybricks Hub", firmware="4.0.1", model="technichub")
    battery = Battery(voltage_mv=8000, current_ma=100, percent=90)
    imu = Imu(accel=(0, 0, 0), gyro=(0, 0, 0), up="top")

    router.route([(b"line-1", hub_info), (b"line-2", battery), (b"l3", imu)])

    assert store.hub_info == hub_info
    assert [e for _, e in store.replay_events(10)] == [hub_info, battery, imu]


def test_router_feeds_rings_in_order() -> None:
    router, store, *_ = make_router()
    events = [Battery(voltage_mv=8000, current_ma=1, percent=90) for _ in range(3)]

    router.route(
        [
            (b"l1", events[0]),
            (b"l2", None),
            (b"l3", events[1]),
            (b"l4", events[2]),
        ]
    )

    assert store.replay_raw_lines(10) == [b"l1", b"l2", b"l3", b"l4"]
    assert [e for _, e in store.replay_events(10)] == events


def test_router_fan_out_is_guarded() -> None:
    router, store, raw_listeners, telemetry_listeners = make_router()
    raw_seen: list[bytes] = []
    telemetry_seen: list[TelemetryEvent] = []

    raw_listeners.append(boom)
    raw_listeners.append(raw_seen.append)
    telemetry_listeners.append(boom)
    telemetry_listeners.append(telemetry_seen.append)

    event = Battery(voltage_mv=8000, current_ma=100, percent=90)
    router.route([(b"line", event)])

    assert raw_seen == [b"line"]
    assert telemetry_seen == [event]
    assert store.replay_raw_lines(10) == [b"line"]


def test_router_shares_live_registries_after_construction() -> None:
    store = TelemetryStore()
    raw_listeners: list[bytes] = []
    telemetry_listeners: list[TelemetryEvent] = []
    router = TelemetryRouter(
        store,
        raw_listeners=raw_listeners,
        telemetry_listeners=telemetry_listeners,
    )

    # Subscribe AFTER construction: the router must see the new listener —
    # it shares the caller's lists, not a construction-time copy.
    raw_seen: list[bytes] = []
    raw_listeners.append(raw_seen.append)
    unsub = subscribe_into(telemetry_listeners, lambda event: None)
    unsub()

    router.route([(b"late-subscriber", None)])
    assert raw_seen == [b"late-subscriber"]
