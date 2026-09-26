"""Tests for :mod:`brick_console.store` (issue #9).

Each test maps to one Done-when checkbox from issue #9. Plain sync tests — the
store is synchronous, thread-unsafe by design (the BLE manager writes on the
asyncio event loop; the WS gateway reads on the same thread).

Event fixtures are built from :mod:`brick_console.events` dataclasses with
distinct, recognisable values so ordinal/order assertions are unambiguous.
"""

from __future__ import annotations

import pytest

from brick_console.events import Battery, HubInfo, Imu, TelemetryEvent
from brick_console.store import TelemetryStore


def _battery(mv: int) -> Battery:
    return Battery(voltage_mv=mv, current_ma=0, percent=50)


def _imu(ax: int) -> Imu:
    return Imu(accel=(ax, 0, 0), gyro=(0, 0, 0), up="top")


def _hub_info(name: str) -> HubInfo:
    return HubInfo(name=name, firmware="4.0.1", model="technichub")


def _append_n(store: TelemetryStore, events: list[TelemetryEvent]) -> list[int]:
    return [store.append_event(e) for e in events]


# ---------------------------------------------------------------------------
# Eviction bounds on both rings
# ---------------------------------------------------------------------------


def test_event_ring_eviction_drops_oldest_never_newest() -> None:
    store = TelemetryStore(event_capacity=3)
    ordinals = _append_n(store, [_battery(mv) for mv in (1, 2, 3, 4)])

    assert store.event_count == 3
    replayed = store.replay_events_from(ordinals[0])
    # Oldest (mv=1) dropped; newest (mv=4) retained; order == append order.
    assert [e.voltage_mv for _, e in replayed] == [2, 3, 4]
    assert [ordinal for ordinal, _ in replayed] == ordinals[1:]


def test_raw_line_ring_eviction_drops_oldest_never_newest() -> None:
    store = TelemetryStore(raw_line_capacity=2)
    for line in (b"one", b"two", b"three"):
        store.append_raw_line(line)

    assert store.raw_line_count == 2
    assert store.replay_raw_lines(10) == [b"two", b"three"]


# ---------------------------------------------------------------------------
# Replay: current-connection slice, count cap, append order
# ---------------------------------------------------------------------------


def test_replay_defaults_to_current_connection_slice() -> None:
    store = TelemetryStore(event_capacity=100)
    # First connection: 3 events.
    _append_n(store, [_battery(mv) for mv in (1, 2, 3)])
    store.mark_connection_start()
    # Second connection: 2 events.
    _append_n(store, [_battery(mv) for mv in (4, 5)])

    replayed = store.replay_events(100)
    assert [e.voltage_mv for _, e in replayed] == [4, 5]


def test_replay_count_cap_respected() -> None:
    store = TelemetryStore(event_capacity=100)
    _append_n(store, [_battery(mv) for mv in (1, 2, 3, 4, 5)])

    replayed = store.replay_events(2)
    assert len(replayed) == 2
    assert [e.voltage_mv for _, e in replayed] == [4, 5]


def test_replay_appends_order_preserved_within_connection() -> None:
    store = TelemetryStore(event_capacity=100)
    events = [_battery(mv) for mv in (10, 20, 30, 40)]
    store.mark_connection_start()
    ordinals = _append_n(store, events)

    replayed = store.replay_events(100)
    assert [ordinal for ordinal, _ in replayed] == ordinals
    assert [e.voltage_mv for _, e in replayed] == [10, 20, 30, 40]


def test_replay_across_reconnect_current_slice_only() -> None:
    store = TelemetryStore(event_capacity=100)
    _append_n(store, [_battery(mv) for mv in (1, 2)])
    store.mark_connection_start()
    _append_n(store, [_battery(mv) for mv in (3, 4)])
    store.mark_connection_start()
    _append_n(store, [_battery(mv) for mv in (5,)])

    assert [e.voltage_mv for _, e in store.replay_events(100)] == [5]


def test_replay_no_connection_marker_returns_newest() -> None:
    store = TelemetryStore(event_capacity=100)
    _append_n(store, [_battery(mv) for mv in (1, 2, 3, 4)])

    # No mark_connection_start called: replay returns newest N from whole ring.
    assert [e.voltage_mv for _, e in store.replay_events(2)] == [3, 4]


# ---------------------------------------------------------------------------
# hub_info snapshot cache
# ---------------------------------------------------------------------------


def test_hub_info_latest_wins_and_replaceable() -> None:
    store = TelemetryStore()
    assert store.hub_info is None

    store.set_hub_info(_hub_info("first"))
    assert store.hub_info is not None
    assert store.hub_info.name == "first"

    store.set_hub_info(_hub_info("second"))
    assert store.hub_info.name == "second"


def test_hub_info_survives_event_ring_eviction() -> None:
    store = TelemetryStore(event_capacity=2)
    store.set_hub_info(_hub_info("persistent"))
    # Fill and overflow the event ring; hub_info must be unaffected.
    _append_n(store, [_battery(mv) for mv in (1, 2, 3, 4)])

    assert store.event_count == 2
    assert store.hub_info is not None
    assert store.hub_info.name == "persistent"


# ---------------------------------------------------------------------------
# Raw lines retained verbatim, independently bounded
# ---------------------------------------------------------------------------


def test_raw_lines_retained_verbatim() -> None:
    store = TelemetryStore()
    raw = b'{"t":"battery","v":8085,"c":42,"pct":87}\r\n'
    store.append_raw_line(raw)

    assert store.replay_raw_lines(1) == [raw]


def test_raw_line_bound_independent_of_event_bound() -> None:
    store = TelemetryStore(event_capacity=2, raw_line_capacity=100)
    # Overflow the event ring; raw lines must be untouched (independent bounds).
    for mv in (1, 2, 3, 4, 5):
        store.append_event(_battery(mv))
    store.append_raw_line(b"survives")

    assert store.event_count == 2
    assert store.raw_line_count == 1
    assert store.replay_raw_lines(10) == [b"survives"]


def test_event_bound_independent_of_raw_bound() -> None:
    store = TelemetryStore(event_capacity=100, raw_line_capacity=2)
    store.append_event(_battery(1))
    for line in (b"a", b"b", b"c"):
        store.append_raw_line(line)

    assert store.raw_line_count == 2
    assert store.event_count == 1
    assert [e.voltage_mv for _, e in store.replay_events(10)] == [1]


# ---------------------------------------------------------------------------
# Reconnect reset: marker moves, pre-connection history addressable
# ---------------------------------------------------------------------------


def test_connection_marker_moves_on_reconnect() -> None:
    store = TelemetryStore(event_capacity=100)
    store.mark_connection_start()
    first_start = store.connection_start_ordinal
    _append_n(store, [_battery(1), _battery(2)])

    store.mark_connection_start()
    assert store.connection_start_ordinal == first_start + 2


def test_absolute_ordinals_never_reused_across_reconnect() -> None:
    store = TelemetryStore(event_capacity=100)
    store.mark_connection_start()
    conn1 = _append_n(store, [_battery(mv) for mv in (1, 2, 3)])
    store.mark_connection_start()
    conn2 = _append_n(store, [_battery(mv) for mv in (4, 5)])

    # No overlap between the two connections' ordinal sets.
    assert set(conn1).isdisjoint(conn2)
    # Ordinals are strictly increasing, never reused.
    assert conn1 == sorted(conn1)
    assert conn2 == sorted(conn2)
    assert conn1[-1] < conn2[0]


def test_pre_connection_history_addressable_via_absolute_ordinals() -> None:
    store = TelemetryStore(event_capacity=100)
    store.mark_connection_start()
    conn1 = _append_n(store, [_battery(mv) for mv in (1, 2, 3)])
    store.mark_connection_start()
    _append_n(store, [_battery(mv) for mv in (4, 5)])

    # Default replay is the current (second) connection only.
    assert [e.voltage_mv for _, e in store.replay_events(100)] == [4, 5]

    # But absolute ordinals reach back into the first connection.
    pre = store.replay_events_from(conn1[0])
    assert [e.voltage_mv for _, e in pre] == [1, 2, 3, 4, 5]

    # And a specific slice into the pre-connection history works.
    mid = store.replay_events_from(conn1[1])
    assert [e.voltage_mv for _, e in mid] == [2, 3, 4, 5]


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_reject_nonpositive_capacities() -> None:
    with pytest.raises(ValueError):
        TelemetryStore(event_capacity=0)
    with pytest.raises(ValueError):
        TelemetryStore(raw_line_capacity=-1)


def test_replay_returns_empty_for_zero_or_negative_count() -> None:
    store = TelemetryStore()
    _append_n(store, [_battery(1)])
    assert store.replay_events(0) == []
    assert store.replay_events(-1) == []
    assert store.replay_raw_lines(0) == []


def test_default_capacities_are_5000() -> None:
    store = TelemetryStore()
    assert store.event_capacity == 5000
    assert store.raw_line_capacity == 5000
