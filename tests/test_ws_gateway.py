"""Tests for :mod:`brick_console.ws` (issue #11) — the WS gateway.

Each test maps to one Done-when checkbox from the issue. TestClient only
(no live socket; the repo-state note pins that TestClient supports WS out
of the box here). No BLE, no hub — the manager seam is faked with the
subscription surface (the same D6 discipline as the app tests: fake the
seam, not FastAPI/starlette internals).

Deterministic live-fire discipline: TestClient runs the app's loop on a
background thread with a ``BlockingPortal``. A manager listener fires on
that loop thread; the test thread cannot ``sleep``-race it reliably. The
correct dance is: fire (from the test thread, into the manager — the
listener registry is plain-list synchronous) → ``portal.call`` an
``await asyncio.sleep(0)`` round-trip so the loop processes the queue →
then read from the client socket.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from brick_console.app import create_app
from brick_console.events import HubInfo, TelemetryEvent
from brick_console.store import TelemetryStore
from brick_console.ws import (
    ClientQueue,
    MockSource,
    hub_label,
    log_envelope,
    state_envelope,
    telemetry_envelope,
)

# ---------------------------------------------------------------------------
# Fakes — the manager seam with the subscription surface
# ---------------------------------------------------------------------------


@dataclass
class FakeManager:
    """Manager with the real fan-out shapes: guarded listeners, idempotent
    unsubscribe, ``(state, reason, timestamp)`` state callbacks. The test
    fires events through ``fire_*`` the way the BLE manager's pipes do."""

    state: str = "offline"
    state_reason: str = "hub not found (scan timeout)"
    telemetry_events: list[TelemetryEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._state_listeners: list = []
        self._telemetry_listeners: list = []
        self._raw_listeners: list = []

    async def run(self) -> None:
        await asyncio.Event().wait()  # park: the lifespan task parks it

    def subscribe_state(self, listener) -> object:
        self._state_listeners.append(listener)
        return lambda: self._off(self._state_listeners, listener)

    def subscribe_telemetry(self, listener) -> object:
        self._telemetry_listeners.append(listener)
        return lambda: self._off(self._telemetry_listeners, listener)

    def subscribe_raw(self, listener) -> object:
        self._raw_listeners.append(listener)
        return lambda: self._off(self._raw_listeners, listener)

    def fire_state(self, state: str, reason: str, timestamp: float = 0.0) -> None:
        self.state, self.state_reason = state, reason
        for listener in list(self._state_listeners):
            listener(state, reason, timestamp)

    def fire_telemetry(self, event: TelemetryEvent) -> None:
        self.telemetry_events.append(event)
        for listener in list(self._telemetry_listeners):
            listener(event)

    def fire_raw(self, raw: bytes) -> None:
        for listener in list(self._raw_listeners):
            listener(raw)

    @staticmethod
    def _off(listeners: list, listener) -> None:
        if listener in listeners:
            listeners.remove(listener)

    @property
    def listener_counts(self) -> tuple[int, int, int]:
        return (
            len(self._state_listeners),
            len(self._telemetry_listeners),
            len(self._raw_listeners),
        )


def make_client(
    manager: FakeManager | None = None,
    store: TelemetryStore | None = None,
) -> tuple[TestClient, FakeManager, TelemetryStore]:
    """Build a live-mode app + client over one manager/store pair."""
    manager = manager if manager is not None else FakeManager()
    store = store if store is not None else TelemetryStore()
    app = create_app(manager, store=store)
    return TestClient(app), manager, store


def seeded_store() -> TelemetryStore:
    """A store with a pre-connection event, a connection marker, and three
    current-connection events (battery/imu/battery) — the replay fixture."""
    store = TelemetryStore()
    pre = HubInfo(name="Pybricks Hub", firmware="4.0.1", model="technichub")
    store.append_event(pre)
    store.set_hub_info(pre)
    from brick_console.events import Battery, Imu

    store.mark_connection_start()
    store.append_event(Battery(voltage_mv=8085, current_ma=42, percent=87))
    store.append_event(Imu(accel=(0, 0, 9810), gyro=(0, 0, 0), up="top"))
    store.append_event(Battery(voltage_mv=8000, current_ma=40, percent=85))
    return store


# ---------------------------------------------------------------------------
# Done-when 1: join sequence — state → hub_info snapshot → replay → live
# ---------------------------------------------------------------------------


def test_join_sequence_exact_order() -> None:
    store = seeded_store()
    with make_client(store=store)[0] as client, client.websocket_connect("/ws") as ws:
        got = [ws.receive_json() for _ in range(5)]

    kinds = [e["type"] for e in got]
    assert kinds == ["state", "telemetry", "telemetry", "telemetry", "telemetry"]
    # (1) state envelope first — current server state + reason.
    assert got[0]["data"] == {
        "state": "offline",
        "reason": "hub not found (scan timeout)",
    }
    # (2) the cached hub_info snapshot BEFORE live and replay (D7).
    assert got[1]["data"] == {
        "t": "hub_info",
        "name": "Pybricks Hub",
        "fw": "4.0.1",
        "model": "technichub",
    }
    # (3) replay: the current-connection slice in append order (the
    # pre-connection hub_info is NOT replayed — it was the snapshot).
    assert [e["data"]["t"] for e in got[2:]] == ["battery", "imu", "battery"]


def test_join_sequence_without_hub_info_snapshot() -> None:
    # No hub_info ever cached: the snapshot step is skipped entirely —
    # join is state → replay → live (the state envelope already said why).
    store = TelemetryStore()
    from brick_console.events import Battery

    store.append_event(Battery(voltage_mv=8085, current_ma=42, percent=87))
    with make_client(store=store)[0] as client, client.websocket_connect("/ws") as ws:
        got = [ws.receive_json() for _ in range(2)]

    assert [e["type"] for e in got] == ["state", "telemetry"]
    assert got[1]["data"]["t"] == "battery"
    # The hub label falls back while no identity is known.
    assert all(e["hub"] == "Pybricks Hub" for e in got)


def test_join_then_live_no_gap_no_duplicate() -> None:
    # The boundary contract: everything appended before the join is
    # replayed exactly once; everything fired after arrives live exactly
    # once — even though the join sends await while events flow.
    store = seeded_store()
    with make_client(store=store)[0] as client, client.websocket_connect("/ws") as ws:
        _ = [ws.receive_json() for _ in range(5)]  # state + snapshot + 3 replay

        from brick_console.events import Imu

        live = Imu(accel=(1, 2, 3), gyro=(4, 5, 6), up="front")
        store.append_event(live)  # appended behind the gateway's back
        import asyncio as _a

        client.portal.call(_a.sleep, 0)
        # Appended straight to the store (no manager fire): the client
        # must NOT receive it — it is not live, and the replay is done.
        # The live path is the manager fire:
        manager = client.app.state.manager
        manager.fire_telemetry(live)
        client.portal.call(_a.sleep, 0)
        got = ws.receive_json()

    assert got["type"] == "telemetry"
    assert got["data"]["t"] == "imu"
    assert got["data"]["ax"] == 1


def test_live_state_and_log_envelopes_flow() -> None:
    store = seeded_store()
    with make_client(store=store)[0] as client, client.websocket_connect("/ws") as ws:
        _ = [ws.receive_json() for _ in range(5)]
        manager = client.app.state.manager
        manager.fire_state("agent", "agent installed and started", 1.5)
        manager.fire_raw(b"Traceback (most recent call last):")
        import asyncio as _a

        client.portal.call(_a.sleep, 0)
        state_evt = ws.receive_json()
        log_evt = ws.receive_json()

    assert state_evt == {
        "type": "state",
        "hub": "Pybricks Hub",
        "data": {"state": "agent", "reason": "agent installed and started"},
    }
    assert log_evt == {
        "type": "log",
        "hub": "Pybricks Hub",
        "data": {"src": "stdout", "line": "Traceback (most recent call last):"},
    }


# ---------------------------------------------------------------------------
# Done-when 2: envelope rules — "type" key; "t" only in telemetry data
# ---------------------------------------------------------------------------


def test_envelope_builders_pin_the_d7_contract() -> None:
    from brick_console.events import Battery, Imu

    # telemetry: "t" inside data is the telemetry kind key.
    tel = telemetry_envelope(
        "Pybricks Hub", Battery(voltage_mv=8085, current_ma=42, percent=87)
    )
    assert tel["type"] == "telemetry"
    assert tel["data"] == {"t": "battery", "v": 8085, "c": 42, "pct": 87}

    # log: {"src", "line"} — never "t" (D7 supersedes architecture §3.2).
    log = log_envelope("Pybricks Hub", b'{"t":"battery"}')
    assert log["type"] == "log"
    assert log["data"] == {"src": "stdout", "line": '{"t":"battery"}'}
    assert "t" not in log["data"]

    # state: state name + reason; the EXTERNAL overlay (F6) is client-side
    # — it must NOT appear anywhere in the server-built envelope.
    st = state_envelope("Pybricks Hub", "offline", "hub disconnected")
    assert st["type"] == "state"
    assert st["data"] == {"state": "offline", "reason": "hub disconnected"}
    assert "external" not in json.dumps(st).lower()

    # imu: full wire object round-trip through the real encoder.
    imu = telemetry_envelope(
        "Pybricks Hub", Imu(accel=(1, -2, 3), gyro=(4, 5, -6), up="top")
    )
    assert imu["data"] == {
        "t": "imu",
        "ax": 1,
        "ay": -2,
        "az": 3,
        "gx": 4,
        "gy": 5,
        "gz": -6,
        "up": "top",
    }


def test_every_live_envelope_respects_the_kind_key_rules() -> None:
    # Over the wire (not just the builders): every envelope's kind key is
    # "type"; "t" appears only inside telemetry data; log/state data never
    # carry it.
    store = seeded_store()
    with make_client(store=store)[0] as client, client.websocket_connect("/ws") as ws:
        manager = client.app.state.manager
        from brick_console.events import Battery

        manager.fire_state("agent", "agent installed", 1.0)
        manager.fire_telemetry(Battery(voltage_mv=8085, current_ma=42, percent=87))
        manager.fire_raw(b"some program print")
        import asyncio as _a

        client.portal.call(_a.sleep, 0)
        got = [ws.receive_json() for _ in range(8)]  # join + 3 live

    for envelope in got:
        assert envelope["type"] in {"telemetry", "log", "state"}
        assert set(envelope) == {"type", "hub", "data"}
        if envelope["type"] in {"log", "state"}:
            assert "t" not in envelope["data"]
    telemetry_data = [e["data"] for e in got if e["type"] == "telemetry"]
    assert all("t" in data for data in telemetry_data)
    log_env = next(e for e in got if e["type"] == "log")
    assert log_env["data"] == {"src": "stdout", "line": "some program print"}


def test_hub_label_prefers_cached_hub_info_name() -> None:
    store = TelemetryStore()
    assert hub_label(store) == "Pybricks Hub"  # fallback before identity
    assert hub_label(None) == "Pybricks Hub"  # and with no store at all
    named = HubInfo(name="Inventor Hub", firmware="4.0.1", model="technichub")
    store.set_hub_info(named)
    assert hub_label(store) == "Inventor Hub"  # the hub's own identity claim


def test_hub_label_propagates_after_identity_arrives() -> None:
    # Per-envelope resolution: envelopes before identity use the fallback;
    # after a hub_info caches, subsequent envelopes carry its name.
    store = TelemetryStore()
    with make_client(store=store)[0] as client, client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["hub"] == "Pybricks Hub"

        named = HubInfo(name="Inventor Hub", firmware="4.0.1", model="technichub")
        store.set_hub_info(named)
        manager = client.app.state.manager
        manager.fire_state("agent", "agent installed and started", 1.0)
        import asyncio as _a

        client.portal.call(_a.sleep, 0)
        second = ws.receive_json()

    assert second["hub"] == "Inventor Hub"


# ---------------------------------------------------------------------------
# Done-when 3: replay — count cap + current-connection marker
# ---------------------------------------------------------------------------


def test_replay_cap_limits_the_join_slice() -> None:
    store = seeded_store()  # 3 current-connection events
    with (
        make_client(store=store)[0] as client,
        client.websocket_connect("/ws?replay=2") as ws,
    ):
        got = [ws.receive_json() for _ in range(4)]

    # state + snapshot + only the newest 2 of the slice (newest-count).
    assert [e["type"] for e in got] == ["state", "telemetry", "telemetry", "telemetry"]
    assert [e["data"]["t"] for e in got[2:]] == ["imu", "battery"]


def test_replay_zero_sends_none() -> None:
    store = seeded_store()
    with (
        make_client(store=store)[0] as client,
        client.websocket_connect("/ws?replay=0") as ws,
    ):
        got = [ws.receive_json() for _ in range(2)]

    assert [e["type"] for e in got] == ["state", "telemetry"]  # snapshot only


def test_replay_default_is_the_current_connection_slice() -> None:
    # Default replay = the store's current-connection slice: a
    # pre-connection event exists in the ring but is NOT replayed by
    # default (it stays addressable via absolute ordinals — the store's
    # contract, pinned in its own tests; here we pin the gateway's use).
    store = seeded_store()
    assert store.connection_start_ordinal == 2  # the fixture's marker

    with make_client(store=store)[0] as client, client.websocket_connect("/ws") as ws:
        got = [ws.receive_json() for _ in range(5)]

    replayed = [e["data"] for e in got[2:]]
    assert [d["t"] for d in replayed] == ["battery", "imu", "battery"]
    # The pre-connection hub_info (ordinal 1) is absent from replay — it
    # was delivered as the snapshot (step 2), not as ring replay.
    assert all(d != got[1]["data"] for d in replayed)


def test_replay_cap_never_leaks_pre_connection_events() -> None:
    # A cap larger than the slice must not reach across the connection
    # marker into pre-connection history.
    store = seeded_store()
    with (
        make_client(store=store)[0] as client,
        client.websocket_connect("/ws?replay=99") as ws,
    ):
        got = [ws.receive_json() for _ in range(5)]

    assert [e["type"] for e in got] == ["state", "telemetry"] + ["telemetry"] * 3
    assert [e["data"]["t"] for e in got[2:]] == ["battery", "imu", "battery"]


# ---------------------------------------------------------------------------
# Done-when 4: mock mode — cadence, real schema path, no BLE import
# ---------------------------------------------------------------------------


def make_mock_client(
    source: MockSource,
) -> TestClient:
    app = create_app(FakeManager(), store=TelemetryStore())
    app.state.mock_source = source
    return TestClient(app)


def test_mock_mode_serves_synthetic_telemetry_at_cadence() -> None:
    # The fake sleep must park (await): a returning sleep spins the
    # cadence loop with no yield and starves the TestClient loop.
    async def park_sleep(seconds: float) -> None:
        assert seconds == 0.1  # the D7 ~10 Hz tick
        await asyncio.Event().wait()

    source = MockSource(sleep=park_sleep)
    with (
        make_mock_client(source) as client,
        client.websocket_connect("/ws?mock=1") as ws,
    ):
        got = [ws.receive_json() for _ in range(8)]

    assert got[0] == {
        "type": "state",
        "hub": "Pybricks Hub (mock)",
        "data": {"state": "agent", "reason": source.state_reason},
    }
    assert got[1]["data"]["t"] == "hub_info"  # the snapshot before live
    assert got[1]["hub"] == "Pybricks Hub (mock)"
    # Live tick 1 — raw-log-primary interleave (D7): each line's raw log
    # envelope fans out FIRST, then its parsed telemetry (the manager's
    # stdout routing: raw before parsed). Order per event: log, telemetry.
    live = got[2:]
    types = [(e["type"], e["data"].get("t") or e["data"]["src"]) for e in live]
    assert types == [
        ("log", "stdout"),
        ("telemetry", "imu"),
        ("log", "stdout"),
        ("telemetry", "port"),
        ("log", "stdout"),
        ("telemetry", "port"),
    ]
    ports = [e for e in live if e["type"] == "telemetry" and e["data"]["t"] == "port"]
    assert {p["data"]["p"] for p in ports} == {"A", "B"}
    # Every live envelope carries the mock identity as its hub label.
    assert all(e["hub"] == "Pybricks Hub (mock)" for e in live)


async def test_mock_source_cadence_kinds_and_battery() -> None:
    # Direct-loop cadence test (the manager-loop pattern from the BLE
    # tests): 10 ticks, then the sleep parks. Tick structure per D7:
    # imu + two port devices every tick, battery on the 10th (~1 Hz).
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 10:
            await asyncio.Event().wait()  # park after 10 ticks

    source = MockSource(sleep=fake_sleep)
    events: list[TelemetryEvent] = []
    raw_lines: list[bytes] = []
    unsub_t = source.subscribe_telemetry(events.append)
    unsub_r = source.subscribe_raw(raw_lines.append)

    task = asyncio.create_task(source.run())
    try:
        for _ in range(500):  # pump the loop until the 10th sleep parks
            await asyncio.sleep(0)
            if len(sleeps) >= 10:
                break
        assert len(sleeps) == 10
        assert all(s == 0.1 for s in sleeps)  # D7 ~10 Hz tick
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    unsub_t()
    unsub_r()

    kinds = [type(e).__name__ for e in events]
    assert kinds[:3] == ["Imu", "Port", "Port"]  # one tick's structure
    assert len(events) == 31  # 10 ticks × 3 + battery on tick 10
    assert kinds[-4:] == ["Imu", "Port", "Port", "Battery"]
    # Fake port devices: Motor on A, ColorSensor on B; every event's wire
    # content went through the real encoder (raw lines) and real parser.
    ports = [e for e in events if type(e).__name__ == "Port"]
    assert {(e.port, e.device) for e in ports} == {
        ("A", "Motor"),
        ("B", "ColorSensor"),
    }
    battery = events[-1]
    assert battery.voltage_mv == 8085 - 10 % 20
    # Raw-log-primary: one raw line per event.
    assert len(raw_lines) == 31
    assert source.store.event_count == 32  # + the __init__ hub_info
    assert source.store.hub_info is not None


def test_mock_source_state_is_synthetic_agent() -> None:
    source = MockSource(sleep=_never_sleep)
    assert source.state == "agent"
    assert "mock" in source.state_reason
    assert source.store.hub_info is not None
    assert source.store.hub_info.name == "Pybricks Hub (mock)"
    # subscribe_state is accepted and never fires (constant state).
    fired: list = []
    unsub = source.subscribe_state(lambda *a: fired.append(a))
    unsub()
    assert fired == []


async def _never_sleep(seconds: float) -> None:
    await asyncio.Event().wait()


def test_mock_mode_isolation_no_ble_no_transport_import() -> None:
    # Module isolation: importing + constructing the full mock path must
    # not pull in bleak, pybricksdev, or the adapter/transport modules.
    code = (
        "import sys;"
        "sys.path.insert(0, 'src');"
        "from brick_console.ws import MockSource, register_routes;"
        "from brick_console.app import create_app;"
        "from brick_console.run import StubManager;"
        "app = create_app(StubManager(), store=None);"
        "ms = MockSource();"
        "assert ms.state == 'agent';"
        "banned = [m for m in ('bleak', 'pybricksdev', "
        "'brick_console.adapter', 'brick_console.transport') if m in sys.modules];"
        "print('BANNED:', banned);"
        "sys.exit(1 if banned else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BANNED: []" in result.stdout


def test_mock_mode_never_mixes_with_live_store() -> None:
    # The parallel-universe contract: a live client's store stays empty of
    # mock events; the mock store never touches the app store.
    source = MockSource(sleep=_never_sleep)
    app = create_app(FakeManager(), store=TelemetryStore())
    app.state.mock_source = source
    with TestClient(app) as client, client.websocket_connect("/ws?mock=1") as ws:
        _ = [ws.receive_json() for _ in range(3)]
        assert source.store.event_count > 0  # mock universe is filling
        assert client.app.state.store.event_count == 0  # live one is not


# ---------------------------------------------------------------------------
# Done-when 5: disconnect cleanup — no leak, no ghost sends
# ---------------------------------------------------------------------------


def test_disconnect_cleans_up_subscriptions_and_tasks() -> None:
    manager = FakeManager()
    store = seeded_store()
    app = create_app(manager, store=store)
    client = TestClient(app)

    with client:
        with client.websocket_connect("/ws") as ws:
            _ = [ws.receive_json() for _ in range(5)]
            assert manager.listener_counts == (1, 1, 1)  # subscribed live

        # Client context exited = disconnect. Everything must be gone:
        # no listener remains (no ghost fan-out into a dead queue).
        assert manager.listener_counts == (0, 0, 0)

        # The mock source is untouched by live clients.
        assert getattr(app.state, "mock_source", None) is None

    # Reconnect: a fresh client gets the full join sequence again — the
    # previous client's cleanup did not poison the route.
    with client, client.websocket_connect("/ws") as ws:
        got = [ws.receive_json() for _ in range(5)]
    assert [e["type"] for e in got] == [
        "state",
        "telemetry",
        "telemetry",
        "telemetry",
        "telemetry",
    ]


def test_disconnect_no_ghost_sends_after_unsubscribe() -> None:
    # Fire events after the client is gone: nothing raises, nothing leaks
    # into the dead client's queue (it was unsubscribed at disconnect).
    manager = FakeManager()
    store = seeded_store()
    client, _, _ = make_client(manager, store)
    with client:
        with client.websocket_connect("/ws") as ws:
            _ = [ws.receive_json() for _ in range(5)]

        assert manager.listener_counts == (0, 0, 0)
        # Fires land on zero listeners — no error, no queue growth.
        manager.fire_state("agent", "agent installed and started", 1.0)
        manager.fire_raw(b"orphan line")
        from brick_console.events import Battery

        manager.fire_telemetry(Battery(voltage_mv=1, current_ma=2, percent=3))
        import asyncio as _a

        client.portal.call(_a.sleep, 0)
        # The next client joins clean — no stale envelopes from the ghost.
        with client.websocket_connect("/ws") as ws2:
            got = [ws2.receive_json() for _ in range(5)]

    assert [e["type"] for e in got] == [
        "state",
        "telemetry",
        "telemetry",
        "telemetry",
        "telemetry",
    ]
    assert got[0]["data"]["state"] == "agent"  # current manager state, fresh


def test_join_phase_failure_still_cleans_up_subscriptions() -> None:
    # Regression (review round 1): the join sends sat outside the
    # try/finally — any join-phase raise leaked the three listeners
    # into the manager forever. hub_info raising simulates a mid-join
    # failure before any client read.
    manager = FakeManager()
    store = seeded_store()

    class ExplodingStore(TelemetryStore):
        @property
        def hub_info(self):
            raise RuntimeError("simulated join-phase failure")

    app = create_app(manager, store=store)
    app.state.store = ExplodingStore()
    client = TestClient(app)

    with (
        client,
        pytest.raises(RuntimeError, match="simulated join-phase failure"),
        client.websocket_connect("/ws") as ws,
    ):
        ws.receive_json()  # never reached: the join raises first

    # The portal is gone with the client; the handler's finally ran
    # during the exception unwind, so the counts must already be zero.
    assert manager.listener_counts == (0, 0, 0)  # no leaked listeners


def test_sender_task_exception_retrieved_not_leaked() -> None:
    # Regression (review round 1): the finally awaited the sender
    # suppressing only CancelledError — a sender dead with a stored
    # exception re-raised out of the finally, skipping the unsubscribes.
    manager = FakeManager()
    store = seeded_store()
    app = create_app(manager, store=store)
    client = TestClient(app)

    async def exploding_send_loop(websocket, queue):
        raise ConnectionResetError("simulated transport reset")

    from brick_console import ws as ws_mod

    original = ws_mod._send_loop
    ws_mod._send_loop = exploding_send_loop
    try:
        with client:
            with client.websocket_connect("/ws") as ws:
                got = [ws.receive_json() for _ in range(5)]  # full join
                # The sender task raises almost immediately; the receive
                # loop keeps waiting until the context close disconnects.
            import asyncio as _a

            client.portal.call(_a.sleep, 0)
            assert manager.listener_counts == (0, 0, 0)  # cleanup still ran
    finally:
        ws_mod._send_loop = original
    assert [e["type"] for e in got] == [
        "state",
        "telemetry",
        "telemetry",
        "telemetry",
        "telemetry",
    ]


def test_mock_disconnect_stops_cadence_task() -> None:
    # The refcounted cadence loop: started on first mock client, stopped
    # (cancelled + joined) when the last leaves — no orphan task.
    async def fake_sleep(seconds: float) -> None:
        await asyncio.Event().wait()

    source = MockSource(sleep=fake_sleep)
    client = make_mock_client(source)
    with client:
        with client.websocket_connect("/ws?mock=1") as ws:
            _ = [ws.receive_json() for _ in range(3)]
            task = source.task
            assert task is not None and not task.done()

        # Last client left: the task is cancelled and cleared.
        import asyncio as _a

        client.portal.call(_a.sleep, 0)
        assert source.task is None
        assert task.cancelled()

        # A second client restarts it fresh.
        with client.websocket_connect("/ws?mock=1") as ws:
            assert source.task is not None and not source.task.done()
            got = ws.receive_json()
    assert got["type"] == "state"


def test_two_mock_clients_share_the_cadence_loop() -> None:
    async def fake_sleep(seconds: float) -> None:
        await asyncio.Event().wait()

    source = MockSource(sleep=fake_sleep)
    with make_mock_client(source) as client:
        with client.websocket_connect("/ws?mock=1") as ws_a:
            with client.websocket_connect("/ws?mock=1") as ws_b:
                assert source.clients == 2
                task = source.task
                _ = ws_a.receive_json()  # join A
                _ = ws_b.receive_json()  # join B
            # B left, A remains: the loop must still be running.
            import asyncio as _a

            client.portal.call(_a.sleep, 0)
            assert source.task is task and not task.done()
        assert source.clients == 0


def test_client_queue_drop_oldest_on_overflow() -> None:
    # Bounded queue, drop-oldest on overflow (the documented policy):
    # newest survives, the drop is counted.
    queue = ClientQueue(depth=3)
    for i in range(3):
        queue.put({"n": i})
    queue.put({"n": 3})  # overflow: {"n": 0} dropped

    assert queue.dropped == 1
    assert queue.qsize() == 3

    async def drain() -> list[dict]:
        return [await queue.get() for _ in range(3)]

    got = asyncio.run(drain())
    assert [e["n"] for e in got] == [1, 2, 3]  # oldest gone, newest kept


def test_client_queue_no_drop_under_capacity() -> None:
    queue = ClientQueue(depth=8)
    for i in range(5):
        queue.put({"n": i})
    assert queue.dropped == 0
    assert queue.qsize() == 5


# ---------------------------------------------------------------------------
# Production wiring — StubManager + register_routes ordering
# ---------------------------------------------------------------------------


def test_stub_manager_ws_join_does_not_crash() -> None:
    # The production wiring today: run.py constructs StubManager. The
    # gateway must serve the join (state → snapshot-if-any → replay) and
    # park in the receive loop without touching BLE or crashing.
    from brick_console.run import StubManager
    from brick_console.store import TelemetryStore

    app = create_app(StubManager(), store=TelemetryStore())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        got = [ws.receive_json() for _ in range(1)]

    assert got[0]["type"] == "state"
    assert got[0]["data"]["state"] == "offline"
    assert "stub" in got[0]["data"]["reason"]


def test_ws_route_wins_over_static_mount(tmp_path) -> None:
    # register_routes runs before the "/" mount: with an index.html
    # present, /ws still upgrades (the mount is fallback only).
    (tmp_path / "index.html").write_text("<html></html>")
    app = create_app(FakeManager(), static_dir=tmp_path)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "state"


def test_inbound_message_ignored() -> None:
    # M1 defines no inbound messages: sending one must not close the
    # socket or error the handler — it is logged at DEBUG and ignored.
    manager = FakeManager()
    store = seeded_store()
    with (
        make_client(manager, store)[0] as client,
        client.websocket_connect("/ws") as ws,
    ):
        _ = [ws.receive_json() for _ in range(5)]
        ws.send_text('{"type": "run", "program": "hello.py"}')
        import asyncio as _a

        client.portal.call(_a.sleep, 0)
        manager.fire_state("agent", "agent installed and started", 1.0)
        client.portal.call(_a.sleep, 0)
        got = ws.receive_json()  # still streaming after the inbound msg

    assert got["data"]["state"] == "agent"
    assert manager.listener_counts == (0, 0, 0)  # cleanup still ran at exit


def test_storeless_app_serves_state_only_join() -> None:
    # A bare app (no store injected) must not crash the gateway: join is
    # just the state envelope, then live-only.
    manager = FakeManager()
    app = create_app(manager)  # store=None
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        got = ws.receive_json()

    assert got == {
        "type": "state",
        "hub": "Pybricks Hub",
        "data": {"state": "offline", "reason": "hub not found (scan timeout)"},
    }
