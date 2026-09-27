"""BLE manager tests (issue #7): the state machine + auto-reconnect loop,
driven entirely by a fake Transport (D6, testing.md) — no bleak, no hub.

Covers the loop contract (architecture §4.5): the OFFLINE→ADVERTISING→AGENT
happy path, scan timeout ⇒ OFFLINE, disconnect ⇒ OFFLINE ⇒ rediscover, the
backoff schedule (immediate first, then doubling to the ~10 s ceiling) via an
injected clock, the ≤ 5 s power-cycle budget with fake timing, status-edge
program handling with automatic agent reinstall, stop-before-install BUSY
sequencing, raw-log-primary fan-out, per-connection parser freshness, and
session/transition logging.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from brick_console.ble_manager import (
    BLEManager,
    BLEManagerConfig,
    HubState,
)
from brick_console.events import HubInfo
from brick_console.transport import (
    DiscoveredHub,
    StatusFlags,
    StdoutListener,
    Transport,
)

BAT_LINE = b'{"t":"battery","v":8085,"c":42,"pct":87}\r\n'
HUB_INFO_LINE = (
    b'{"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}\r\n'
)


class FakeHub(DiscoveredHub):
    """The opaque discovered-hub handle, faked with the same two fields the
    real adapter exposes (name, informational address)."""

    def __init__(
        self, name: str = "Pybricks Hub", address: str = "38:D3:4E:D4:E6:A1"
    ) -> None:
        self._name = name
        self._address = address

    @property
    def name(self) -> str:
        return self._name

    @property
    def address(self) -> str:
        return self._address


@dataclass
class ConnectOutcome:
    """Scripted result of one ``connect`` call."""

    fail: bool = False
    drop_after: float | None = None
    """If set, the connection spontaneously drops this many (fake) seconds
    after connect resolves."""


class FakeTransport(Transport):
    """Scriptable Transport (testing.md: fake the seam, not bleak internals).

    Records every operation call in order; the test scripts discovery
    results, connect outcomes, and injects status reports / stdout chunks /
    disconnects exactly the way the real adapter will.
    """

    def __init__(self) -> None:
        self.ops: list[str] = []
        self.install_calls: list[Path] = []
        self.discover_results: list[FakeHub | None] = []
        self.connect_outcomes: list[ConnectOutcome] = []
        self.stdout_listeners: list[StdoutListener] = []
        self.status_listeners: list = []
        self.on_disconnect: list = []
        self.current_flags = StatusFlags(0)
        self.program_running_at_connect: bool = False
        self.stop_calls: int = 0
        self.now = 0.0  # fake time, advanced by sleep()

    async def discover(self, name: str, *, timeout: float = 10.0) -> DiscoveredHub:
        self.ops.append(f"discover({name})")
        if not self.discover_results:
            raise TimeoutError()
        result = self.discover_results.pop(0)
        if result is None:
            raise TimeoutError()
        return result

    async def connect(self, hub: DiscoveredHub, *, on_disconnect) -> None:
        self.ops.append(f"connect({hub.name})")
        # A fresh connection object per connect (the CLI's proven pattern,
        # api-notes): subscriptions do not outlive their connection.
        self.stdout_listeners.clear()
        self.status_listeners.clear()
        self.on_disconnect.clear()
        outcome = (
            self.connect_outcomes.pop(0) if self.connect_outcomes else ConnectOutcome()
        )
        if outcome.fail:
            raise ConnectionError("another central holds the hub")
        self.on_disconnect.append(on_disconnect)
        if self.program_running_at_connect:
            # A stale user program still running at connect — held as the
            # current snapshot; subscribe_status replays it (seam contract).
            self.current_flags = StatusFlags.USER_PROGRAM_RUNNING
        if outcome.drop_after is not None:
            loop = asyncio.get_running_loop()
            loop.call_later(outcome.drop_after, lambda: self.on_disconnect[-1]())

    async def install_and_start(self, program: Path, *, wait: bool = False) -> None:
        self.ops.append(f"install_and_start({program.name})")
        self.install_calls.append(program)
        self._emit_status(StatusFlags.USER_PROGRAM_RUNNING)

    async def stop(self) -> None:
        self.ops.append("stop()")
        self.stop_calls += 1
        self._emit_status(StatusFlags(0))

    async def write_stdin(self, data: bytes) -> None:
        self.ops.append("write_stdin()")

    async def subscribe_stdout(self, listener: StdoutListener) -> None:
        self.ops.append("subscribe_stdout()")
        self.stdout_listeners.append(listener)

    async def subscribe_status(self, listener) -> None:
        self.ops.append("subscribe_status()")
        self.status_listeners.append(listener)
        listener(self.current_flags)  # snapshot replay on subscribe (seam contract)

    async def disconnect(self) -> None:
        self.ops.append("disconnect()")

    def _emit_status(self, flags: StatusFlags) -> None:
        self.current_flags = flags
        for listener in list(self.status_listeners):
            listener(flags)

    # Test-side injectors ------------------------------------------------

    def push_stdout(self, data: bytes) -> None:
        for listener in list(self.stdout_listeners):
            listener(data)

    def fire_disconnect(self) -> None:
        if self.on_disconnect:
            self.on_disconnect[-1]()


@dataclass
class FakeSink:
    """Structural fake of the telemetry store — records every write so tests
    assert both the raw-line path (primary) and the parsed-event path."""

    events: list = field(default_factory=list)
    raw_lines: list[bytes] = field(default_factory=list)
    hub_info: HubInfo | None = None
    connection_marks: int = 0

    def append_event(self, event) -> int:
        self.events.append(event)
        return len(self.events)

    def append_raw_line(self, line: bytes) -> None:
        self.raw_lines.append(line)

    def set_hub_info(self, hub_info: HubInfo) -> None:
        self.hub_info = hub_info

    def mark_connection_start(self) -> None:
        self.connection_marks += 1


@dataclass
class FakeTime:
    """Deterministic clock + sleep: ``sleep`` advances ``now`` by the delay
    (exact backoff schedules) but still yields to the event loop once, so
    cancellation and ``call_later``-scheduled disconnects stay live."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink()


def make_manager(
    transport: FakeTransport,
    sink: FakeSink,
    fake_time: FakeTime,
    **config_kwargs,
) -> BLEManager:
    return BLEManager(
        transport,
        sink,
        config=BLEManagerConfig(agent_program=Path("agent_main.py"), **config_kwargs),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )


async def run_until(
    manager: BLEManager,
    *,
    state: HubState,
    timeout: float = 5.0,
) -> None:
    """Run the manager loop until the state changes to ``state`` (or
    timeout fails the test)."""
    reached = asyncio.Event()

    def on_state(s: HubState, reason: str, timestamp: float) -> None:
        if s is state and not reached.is_set():
            reached.set()

    unsubscribe = manager.subscribe_state(on_state)
    try:
        await asyncio.wait_for(reached.wait(), timeout)
    finally:
        unsubscribe()


async def poll_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Await a condition over fake-time events without wall-clock sleeps."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0)


async def cancel_quietly(task: asyncio.Task[None]) -> None:
    """Cancel a manager loop task and await its quiet death."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def states(manager: BLEManager) -> list[HubState]:
    return [t.to_state for t in manager.transitions()]


def reasons(manager: BLEManager) -> list[str]:
    return [t.reason for t in manager.transitions()]


# ---------------------------------------------------------------------------
# Happy path: OFFLINE → ADVERTISING → AGENT
# ---------------------------------------------------------------------------


async def test_happy_path_offline_advertising_agent(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub()]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)
        assert states(manager) == [HubState.ADVERTISING, HubState.AGENT]
        assert manager.state is HubState.AGENT
        assert transport.ops == [
            "discover(Pybricks Hub)",
            "connect(Pybricks Hub)",
            "subscribe_stdout()",
            "subscribe_status()",
            "stop()",
            "install_and_start(agent_main.py)",
        ]
        assert sink.connection_marks == 1
    finally:
        await cancel_quietly(task)


async def test_scan_timeout_means_hub_offline(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = []  # always asyncio.TimeoutError
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await poll_until(lambda: transport.ops.count("discover(Pybricks Hub)") >= 2)
        assert manager.state is HubState.OFFLINE
        assert "hub not found (scan timeout)" in manager.state_reason
        # OFFLINE→OFFLINE is not recorded as a transition (same-state rule).
        assert states(manager) == []
        # First retry immediate, later rescans on backoff (never a wall-clock spin).
        assert fake_time.sleeps[0] == 0.0
        assert all(delay >= 1.0 for delay in fake_time.sleeps[1:])
    finally:
        await cancel_quietly(task)


async def test_disconnect_lands_offline_then_rediscovery(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub(), FakeHub()]
    transport.connect_outcomes = [ConnectOutcome(drop_after=0.05), ConnectOutcome()]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.OFFLINE)
        await run_until(manager, state=HubState.AGENT)
        assert states(manager) == [
            HubState.ADVERTISING,
            HubState.AGENT,
            HubState.OFFLINE,
            HubState.ADVERTISING,
            HubState.AGENT,
        ]
        # Session 1: fresh parser; session 2: fresh parser — no state leaks.
        assert sink.connection_marks == 2
    finally:
        await cancel_quietly(task)


# ---------------------------------------------------------------------------
# Backoff schedule (F6): immediate first, then doubling to ~10 s ceiling
# ---------------------------------------------------------------------------


async def test_backoff_schedule_immediate_then_doubling_to_ceiling(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = []  # every scan misses
    manager = make_manager(
        transport, sink, fake_time, backoff_base=1.0, backoff_ceiling=10.0
    )
    task = asyncio.create_task(manager.run())
    try:
        await poll_until(lambda: transport.ops.count("discover(Pybricks Hub)") >= 6)
        expected = [0.0, 1.0, 2.0, 4.0, 8.0, 10.0]
        assert fake_time.sleeps[:6] == expected
    finally:
        await cancel_quietly(task)


async def test_backoff_resets_only_on_successful_agent_connection(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    # Two scan misses, then a hub that is discovered but connect fails
    # (another central holds it), then another miss.
    transport.discover_results = [None, None, FakeHub(), None]
    transport.connect_outcomes = [ConnectOutcome(fail=True)]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await poll_until(lambda: transport.ops.count("discover(Pybricks Hub)") >= 4)
        # Schedule: 0, 1, 2 (misses), then connect-fail lands OFFLINE and
        # the next retry continues the escalation — never a reset to
        # immediate, because AGENT was never reached (never fight, F6).
        assert fake_time.sleeps[:4] == [0.0, 1.0, 2.0, 4.0]
        assert any("connect failed" in r for r in reasons(manager))
    finally:
        await cancel_quietly(task)


async def test_connect_failure_holds_backoff_and_never_fights(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub()] * 3
    transport.connect_outcomes = [ConnectOutcome(fail=True)] * 3
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await poll_until(
            lambda: (
                len(
                    [
                        t
                        for t in manager.transitions()
                        if t.to_state is HubState.OFFLINE
                        and "connect failed" in t.reason
                    ]
                )
                >= 2
            )
        )
        assert fake_time.sleeps[:3] == [0.0, 1.0, 2.0]
    finally:
        await cancel_quietly(task)


# ---------------------------------------------------------------------------
# Power-cycle budget (§4 rule 3): full reconnect ≤ 5 s
# ---------------------------------------------------------------------------


async def test_power_cycle_reconnect_within_budget(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    # Hub power-cycles after a first healthy session.
    transport.discover_results = [FakeHub(), FakeHub()]
    transport.connect_outcomes = [ConnectOutcome(drop_after=0.05), ConnectOutcome()]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)  # first session up
        fake_time.now = 100.0  # power-cycle happens at t=100 (fake)
        await run_until(manager, state=HubState.AGENT)
        # Second AGENT reached; reconstruct elapsed time from transitions.
        second = manager.session_transitions(2)
        reconnect_started = 100.0  # disconnect at t=100
        agent_reached = second[-1].timestamp
        assert agent_reached - reconnect_started <= 5.0
        # The immediate first retry after the drop: no backoff delay in between.
        assert fake_time.sleeps == [0.0, 0.0]
    finally:
        await cancel_quietly(task)


# ---------------------------------------------------------------------------
# Status edges: PROGRAM / AGENT + automatic agent reinstall (§4 rule 2)
# ---------------------------------------------------------------------------


async def test_user_program_lifecycle_edges_and_agent_reinstall(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub()]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)
        reached_program = asyncio.Event()
        reached_agent_again = asyncio.Event()

        def on_state(s: HubState, reason: str, timestamp: float) -> None:
            if s is HubState.PROGRAM and not reached_program.is_set():
                reached_program.set()
            if (
                s is HubState.AGENT
                and reached_program.is_set()
                and not reached_agent_again.is_set()
            ):
                reached_agent_again.set()

        unsubscribe = manager.subscribe_state(on_state)
        # Race, both edges before the loop yields: the agent ends (crash —
        # USER_PROGRAM_RUNNING clears, AGENT unchanged, reinstall scheduled)
        # and a foreign program grabs the hub (unexpected set → PROGRAM).
        transport._emit_status(StatusFlags(0))
        transport._emit_status(StatusFlags.USER_PROGRAM_RUNNING)
        await asyncio.wait_for(reached_program.wait(), 5.0)
        assert manager.state is HubState.PROGRAM
        # The auto-reinstall reclaims the hub: its stop() ends the foreign
        # program (PROGRAM → AGENT, "user program ended"), then installs
        # and starts the agent (§4 rule 2) — the reinstall's own SET is
        # expected and must not read as PROGRAM.
        await asyncio.wait_for(reached_agent_again.wait(), 5.0)
        await poll_until(lambda: len(transport.install_calls) >= 2)
        unsubscribe()
        assert len(transport.install_calls) == 2
        assert states(manager) == [
            HubState.ADVERTISING,
            HubState.AGENT,
            HubState.PROGRAM,
            HubState.AGENT,
        ]
        assert reasons(manager)[-2:] == ["user program started", "user program ended"]
    finally:
        unsubscribe()
        await cancel_quietly(task)


async def test_stale_program_at_connect_is_stopped_by_setup(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    # The hub still has a program running when we connect (started from the
    # CLI while we were offline) — setup must stop it and install the agent;
    # no PROGRAM state may be entered.
    transport.discover_results = [FakeHub()]
    transport.program_running_at_connect = True
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)
        assert HubState.PROGRAM not in states(manager)
        assert manager.state is HubState.AGENT
        assert "stop()" in transport.ops
        # stop() emitted a USER_PROGRAM_RUNNING clear; the install's set is
        # expected (the agent itself); neither read as PROGRAM.
        assert len(transport.install_calls) == 1
    finally:
        await cancel_quietly(task)


# ---------------------------------------------------------------------------
# BUSY sequencing (D6): stop() before install_and_start()
# ---------------------------------------------------------------------------


async def test_stop_called_before_install_when_program_running(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub()]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)
        reached_program = asyncio.Event()
        back_to_agent = asyncio.Event()

        def on_state(s: HubState, reason: str, timestamp: float) -> None:
            if s is HubState.PROGRAM and not reached_program.is_set():
                reached_program.set()
            if (
                s is HubState.AGENT
                and reached_program.is_set()
                and not back_to_agent.is_set()
            ):
                back_to_agent.set()

        unsubscribe = manager.subscribe_state(on_state)
        # The agent ends and a foreign program grabs the hub, synchronously.
        transport._emit_status(StatusFlags(0))
        transport._emit_status(StatusFlags.USER_PROGRAM_RUNNING)
        await asyncio.wait_for(reached_program.wait(), 5.0)
        transport._emit_status(StatusFlags(0))
        await asyncio.wait_for(back_to_agent.wait(), 5.0)
        await poll_until(lambda: len(transport.install_calls) >= 2)
        unsubscribe()
        ops = transport.ops
        stop_positions = [i for i, op in enumerate(ops) if op == "stop()"]
        install_positions = [
            i for i, op in enumerate(ops) if op.startswith("install_and_start")
        ]
        # Every install is preceded by a stop (BUSY precondition, D6).
        for install_pos in install_positions:
            assert any(p < install_pos for p in stop_positions)
        assert len(transport.install_calls) == 2
    finally:
        await cancel_quietly(task)


# ---------------------------------------------------------------------------
# Raw-log-primary (D7): raw lines reach the log path even when malformed
# ---------------------------------------------------------------------------


async def test_raw_lines_logged_even_when_malformed(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub()]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)
        chunk = b"Traceback (most recent call last):\r\n" + BAT_LINE + b"not json\r\n"
        transport.push_stdout(chunk)
        await asyncio.sleep(0)
        assert sink.raw_lines == [
            b"Traceback (most recent call last):",
            b'{"t":"battery","v":8085,"c":42,"pct":87}',
            b"not json",
        ]
        assert [type(e).__name__ for e in sink.events] == ["Battery"]
        assert manager.malformed_count == 2
        assert sink.hub_info is None
    finally:
        await cancel_quietly(task)


async def test_hub_info_snapshot_cached_and_telemetry_fanout(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub()]
    manager = make_manager(transport, sink, fake_time)
    seen: list = []
    unsubscribe = manager.subscribe_telemetry(lambda e: seen.append(e))
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)
        transport.push_stdout(HUB_INFO_LINE + BAT_LINE)
        await asyncio.sleep(0)
        assert isinstance(sink.hub_info, HubInfo)
        assert sink.hub_info.name == "Pybricks Hub"
        assert [type(e).__name__ for e in sink.events] == ["HubInfo", "Battery"]
        assert [type(e).__name__ for e in seen] == ["HubInfo", "Battery"]
        unsubscribe()
        transport.push_stdout(BAT_LINE)
        await asyncio.sleep(0)
        assert len(seen) == 2  # no more events after unsubscribe
    finally:
        await cancel_quietly(task)


# ---------------------------------------------------------------------------
# Fresh parser per connect (D7): per-connection state does not leak
# ---------------------------------------------------------------------------


async def test_fresh_parser_per_connect_malformed_count_resets(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub(), FakeHub()]
    transport.connect_outcomes = [ConnectOutcome(drop_after=0.02), ConnectOutcome()]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)
        transport.push_stdout(b"junk\r\n")
        await asyncio.sleep(0)
        assert manager.malformed_count == 1
        transport.fire_disconnect()
        await run_until(manager, state=HubState.OFFLINE)
        assert manager.malformed_count == 0  # offline: no parser at all
        await run_until(manager, state=HubState.AGENT)
        assert manager.malformed_count == 0  # fresh parser, no leak (D7)
        transport.push_stdout(b"junk\r\n")
        await asyncio.sleep(0)
        assert manager.malformed_count == 1  # counts from this connection only
    finally:
        await cancel_quietly(task)


# ---------------------------------------------------------------------------
# Session / transition logging (§4 rule 4)
# ---------------------------------------------------------------------------


async def test_session_and_transition_logging(
    transport: FakeTransport,
    sink: FakeSink,
    fake_time: FakeTime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport.discover_results = [FakeHub(), FakeHub()]
    transport.connect_outcomes = [ConnectOutcome(drop_after=0.02), ConnectOutcome()]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        with caplog.at_level(logging.INFO, logger="brick_console.ble_manager"):
            await run_until(manager, state=HubState.OFFLINE)
            await run_until(manager, state=HubState.AGENT)
        session1 = manager.session_transitions(1)
        assert [(t.from_state, t.to_state, t.reason) for t in session1] == [
            (HubState.OFFLINE, HubState.ADVERTISING, "hub discovered: Pybricks Hub"),
            (HubState.ADVERTISING, HubState.AGENT, "agent installed and started"),
            (HubState.AGENT, HubState.OFFLINE, "hub disconnected"),
        ]
        assert all(isinstance(t.timestamp, float) for t in session1)
        assert manager.session_transitions(2)[0].from_state is HubState.OFFLINE
        # All transitions logged with timestamps (§4 rule 4).
        transition_logs = [
            r for r in caplog.records if "state" in r.message and "->" in r.message
        ]
        assert len(transition_logs) == 5
    finally:
        await cancel_quietly(task)


async def test_state_listener_fanout(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub()]
    manager = make_manager(transport, sink, fake_time)
    seen: list[tuple[HubState, str, float]] = []
    manager.subscribe_state(lambda s, r, t: seen.append((s, r, t)))
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)
        assert seen[0][0] is HubState.ADVERTISING
        assert seen[-1][0] is HubState.AGENT
        assert all(isinstance(t, float) for _, _, t in seen)
    finally:
        await cancel_quietly(task)


async def test_state_listener_error_does_not_kill_loop(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub()]
    manager = make_manager(transport, sink, fake_time)

    def broken(state: HubState, reason: str, timestamp: float) -> None:
        raise RuntimeError("WS gateway bug")

    manager.subscribe_state(broken)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)  # loop survived the bug
    finally:
        await cancel_quietly(task)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def test_teardown_releases_central_role(
    transport: FakeTransport, sink: FakeSink, fake_time: FakeTime
) -> None:
    transport.discover_results = [FakeHub()]
    manager = make_manager(transport, sink, fake_time)
    task = asyncio.create_task(manager.run())
    try:
        await run_until(manager, state=HubState.AGENT)
    finally:
        await cancel_quietly(task)
    assert "disconnect()" in transport.ops  # central role released (F6)
    assert manager.state is HubState.OFFLINE


def test_config_defaults_pin_architecture_constants() -> None:
    config = BLEManagerConfig()
    assert config.hub_name == "Pybricks Hub"
    assert config.backoff_ceiling == 10.0  # F6 ~10 s ceiling
    assert config.backoff_base == 1.0
    assert config.connect_timeout < 30.0  # below bleak's own 30 s bound
    assert config.agent_program.name == "agent_main.py"
