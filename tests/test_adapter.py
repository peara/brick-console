"""Adapter tests (issue #8): the bleak/pybricksdev seam mapping, pinned by
a fake hub — no BLE, no hub (testing.md §"Server tests").

The fake mirrors the real ``PybricksHubBLE`` surface exactly as
api-notes documents it: real reactivex subjects (a plain ``Subject`` for
stdout, a ``BehaviorSubject`` seeded with ``StatusFlag(0)`` for status,
a ``BehaviorSubject`` for connection state — the same semantics the
installed 2.3.2 uses), the same method names, and a record of every call.
Real-hub behavior is proven by the hardware smoke test (#14), not here.

The two import-ban tests enforce D6's grep-able convention: no module
outside ``adapter.py`` may import bleak or pybricksdev. The status
bit-pinning import in ``test_transport.py`` is the sanctioned exception
(it *pins the library's values*, it does not use them) — see
``test_no_pybricksdev_import_outside_adapter``.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path

import pytest
from pybricksdev.ble.pybricks import StatusFlag
from pybricksdev.connections import ConnectionState
from reactivex.subject import BehaviorSubject, Subject

import brick_console.adapter as adapter_module
from brick_console.adapter import DiscoveredPybricksHub, PybricksDevTransport
from brick_console.transport import StatusFlags

# A synthetic flag word carrying named bits we consume plus bits we do
# not name in StatusFlags — passthrough must preserve the whole word and
# specific bits must test true/false (never plain truthiness: zero flags
# are falsy but still a valid snapshot).
SYNTHETIC_WORD = (
    int(StatusFlag.USER_PROGRAM_RUNNING)
    | int(StatusFlag.BLE_HOST_CONNECTED)
    | (1 << 20)
)
SYNTHETIC_FLAGS = StatusFlags(SYNTHETIC_WORD)


class FakeBLEDevice:
    """The bleak BLEDevice, faked to the two fields the seam exposes."""

    def __init__(self, name: str, address: str) -> None:
        self.name = name
        self.address = address


class CallRecord:
    """One recorded hub-method call: name plus keyword arguments."""

    def __init__(self, name: str, kwargs: dict[str, object] | None = None) -> None:
        self.name = name
        self.kwargs: dict[str, object] = kwargs if kwargs is not None else {}


class FakePybricksHubBLE:
    """Test double for ``pybricksdev.connections.pybricks.PybricksHubBLE``.

    Follows the real object: connection state as a BehaviorSubject seeded
    DISCONNECTED, status as a BehaviorSubject seeded ``StatusFlag(0)``,
    stdout as a plain Subject (no replay). ``connect()`` mirrors the real
    state machine — CONNECTING, then CONNECTED, or DISCONNECTED via the
    exit-stack on any failure.
    """

    def __init__(self, device: FakeBLEDevice) -> None:
        self.device = device
        self.print_output = True
        self.connection_state_observable: BehaviorSubject[ConnectionState] = (
            BehaviorSubject(ConnectionState.DISCONNECTED)
        )
        self.status_observable: BehaviorSubject[StatusFlag] = BehaviorSubject(
            StatusFlag(0)
        )
        self.stdout_observable: Subject[bytes] = Subject()
        self.calls: list[CallRecord] = []
        self.connect_delay: float = 0.0
        self.connect_error: Exception | None = None
        self.disconnect_error: Exception | None = None
        self.print_output_at_connect: bool | None = None
        self._running_program = 0
        self._selected_slot = 0

    async def connect(self) -> None:
        self.calls.append(CallRecord("connect"))
        self.connection_state_observable.on_next(ConnectionState.CONNECTING)
        if self.connect_delay > 0:
            await asyncio.sleep(self.connect_delay)
        if self.connect_error is not None:
            self.connection_state_observable.on_next(ConnectionState.DISCONNECTED)
            raise self.connect_error
        self.print_output_at_connect = self.print_output
        self.connection_state_observable.on_next(ConnectionState.CONNECTED)

    async def disconnect(self) -> None:
        self.calls.append(CallRecord("disconnect"))
        if self.connection_state_observable.value == ConnectionState.CONNECTED:
            self.connection_state_observable.on_next(ConnectionState.DISCONNECTING)
            if self.disconnect_error is not None:
                raise self.disconnect_error
            # The real bleak client invokes the disconnected callback from
            # its teardown — synchronous here.
            self.connection_state_observable.on_next(ConnectionState.DISCONNECTED)

    async def run(
        self,
        py_path: str,
        *,
        wait: bool,
        print_output: bool,
        line_handler: bool,
    ) -> None:
        self.calls.append(
            CallRecord(
                "run",
                {
                    "py_path": py_path,
                    "wait": wait,
                    "print_output": print_output,
                    "line_handler": line_handler,
                },
            )
        )

    async def stop_user_program(self) -> None:
        self.calls.append(CallRecord("stop_user_program"))

    async def write_string(self, value: str) -> None:
        self.calls.append(CallRecord("write_string", {"value": value}))

    def drop_connection(self, *, power_button: bool = False) -> None:
        """Simulate a spontaneous drop — the bleak disconnected callback
        path (the real hub's power-button press lands the same way)."""
        self.connection_state_observable.on_next(ConnectionState.DISCONNECTED)

    def emit_status(self, flags: int | StatusFlag) -> None:
        self.status_observable.on_next(StatusFlag(int(flags)))

    def emit_stdout(self, data: bytes) -> None:
        self.stdout_observable.on_next(data)


class Harness:
    """One wired adapter + every hub its factory built (latest first)."""

    def __init__(self, transport: PybricksDevTransport, device: FakeBLEDevice):
        self.transport = transport
        self.device = device
        self.hubs: list[FakePybricksHubBLE] = []

    @property
    def hub(self) -> FakePybricksHubBLE:
        """The most recently built hub — the one a connect would drive."""
        return self.hubs[-1]


class _DisconnectCounter:
    """Counting DisconnectListener — asserts exactly-once across both
    the deliberate and the spontaneous path."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def make_transport(
    *,
    connect_timeout: float = 10.0,
    connect_delay: float = 0.0,
    connect_error: Exception | None = None,
) -> Harness:
    """Build the adapter over a fake hub factory — no BLE anywhere."""
    device = FakeBLEDevice("Pybricks Hub", "38:D3:4E:D4:E6:A1")
    harness: Harness | None = None

    def factory(dev: FakeBLEDevice) -> FakePybricksHubBLE:
        hub = FakePybricksHubBLE(dev)
        hub.connect_delay = connect_delay
        hub.connect_error = connect_error
        assert harness is not None
        harness.hubs.append(hub)
        return hub

    transport = PybricksDevTransport(
        connect_timeout=connect_timeout,
        hub_factory=factory,  # type: ignore[arg-type]
    )
    harness = Harness(transport=transport, device=device)
    return harness


async def test_discover_wraps_device_name_and_address(monkeypatch) -> None:
    # discover → find_device: the result is wrapped opaquely (name/address
    # only), the timeout passed through, and asyncio.TimeoutError from the
    # scan propagates untouched (gotcha 5).
    seen: dict[str, object] = {}

    async def fake_find_device(name=None, service=None, timeout=None):
        seen["name"] = name
        seen["timeout"] = timeout
        return FakeBLEDevice("Pybricks Hub", "AA:BB:CC:DD:EE:FF")

    monkeypatch.setattr("brick_console.adapter.find_device", fake_find_device)
    transport = PybricksDevTransport()
    handle = await transport.discover("Pybricks Hub", timeout=7.5)
    assert seen == {"name": "Pybricks Hub", "timeout": 7.5}
    assert isinstance(handle, DiscoveredPybricksHub)
    assert handle.name == "Pybricks Hub"
    assert handle.address == "AA:BB:CC:DD:EE:FF"


async def test_discover_timeout_passes_through(monkeypatch) -> None:
    # TimeoutError is the "hub off/asleep" signal — never retyped.
    async def fake_find_device(name=None, service=None, timeout=None):
        raise TimeoutError()

    monkeypatch.setattr("brick_console.adapter.find_device", fake_find_device)
    transport = PybricksDevTransport()
    with pytest.raises(asyncio.TimeoutError):
        await transport.discover("Pybricks Hub")


async def test_connect_builds_fresh_hub_and_sets_print_output_false() -> None:
    h = make_transport()
    counter = _DisconnectCounter()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=counter)
    # print_output set False before the handshake (gotcha 4) — the fake
    # records its value at connect completion time.
    assert h.hub.print_output_at_connect is False
    assert counter.count == 0


async def test_connect_builds_a_fresh_hub_per_call() -> None:
    # CLI reconnect pattern: every connect() builds a new hub object —
    # never reuse (pybricksdev has no reconnect-on-one-object API).
    h = make_transport()
    counter = _DisconnectCounter()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=counter)
    first = h.hub
    await h.transport.disconnect()
    await h.transport.connect(handle, on_disconnect=counter)
    assert h.hub is not first
    await h.transport.disconnect()


async def test_connect_bounded_by_timeout_and_releases_role() -> None:
    # A handshake that never completes is bounded by the adapter's own
    # wait_for (<= the consumer's 15 s bound so this one is effective),
    # and the abandoned hub is disconnected best-effort so the BLE
    # central role is not left held (the #7 mid-handshake-timeout lesson).
    h = make_transport(connect_timeout=0.05, connect_delay=5.0)
    handle = DiscoveredPybricksHub(h.device)
    counter = _DisconnectCounter()
    with pytest.raises(asyncio.TimeoutError):
        await h.transport.connect(handle, on_disconnect=counter)
    # The release attempt happened exactly once, after the timeout.
    names = [c.name for c in h.hub.calls]
    assert names.count("disconnect") == 1
    assert counter.count == 0  # failed connects never had a connection


async def test_connect_failure_wrapped_as_connection_error() -> None:
    # Protocol mismatch (RuntimeError at connect) and bleak handshake
    # errors are connect failures per the caller's taxonomy — and the
    # role-release still runs.
    h = make_transport(connect_error=RuntimeError("Unsupported Pybricks protocol"))
    handle = DiscoveredPybricksHub(h.device)
    with pytest.raises(ConnectionError, match="Unsupported Pybricks protocol"):
        await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    assert [c.name for c in h.hub.calls].count("disconnect") == 1


async def test_connect_rejects_foreign_hub_handle() -> None:
    # The handle must come from this transport's discover() — structural
    # look-alikes are not trusted (the seam stays opaque).
    transport = PybricksDevTransport(hub_factory=FakePybricksHubBLE)

    class Stranger:
        name = "Pybricks Hub"
        address = "00:00:00:00:00:00"

    with pytest.raises(TypeError):
        await transport.connect(Stranger(), on_disconnect=_DisconnectCounter())


async def test_install_and_start_pins_run_flags() -> None:
    # The mapping table's sharpest pin: run() called with exactly
    # wait=False, print_output=False, line_handler=False — RAM-only,
    # never wait, never echo, never line-handle.
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    program = Path("agent_main.py")
    await h.transport.install_and_start(program)
    run_calls = [c for c in h.hub.calls if c.name == "run"]
    assert len(run_calls) == 1
    assert run_calls[0].kwargs == {
        "py_path": "agent_main.py",
        "wait": False,
        "print_output": False,
        "line_handler": False,
    }


async def test_stop_maps_to_stop_user_program() -> None:
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    await h.transport.stop()
    assert [c.name for c in h.hub.calls] == ["connect", "stop_user_program"]


async def test_write_stdin_decodes_and_delegates_chunking() -> None:
    # write_stdin → write_string(decode utf-8); chunking to the
    # negotiated size is pybricksdev's job (api-notes row 7) — the adapter
    # never sees chunk boundaries.
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    await h.transport.write_stdin("print('héllo')\r\n".encode())
    writes = [c for c in h.hub.calls if c.name == "write_string"]
    assert [c.kwargs["value"] for c in writes] == ["print('héllo')\r\n"]


async def test_subscribe_stdout_fans_out_raw_bytes() -> None:
    # N listeners, each receiving the same raw payload — no splitting, no
    # parsing, no replay (the stdout Subject delivers future events only).
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    got_a: list[bytes] = []
    got_b: list[bytes] = []
    await h.transport.subscribe_stdout(got_a.append)
    await h.transport.subscribe_stdout(got_b.append)
    h.hub.emit_stdout(b'{"t":"battery"}\r\n')
    h.hub.emit_stdout(b"partial")
    assert got_a == [b'{"t":"battery"}\r\n', b"partial"]
    assert got_b == [b'{"t":"battery"}\r\n', b"partial"]


async def test_subscribe_stdout_before_connect_no_replay_after() -> None:
    # Registry is adapter-level: a listener registered while offline
    # (per-connection subscription happens at connect) still receives
    # everything from the next connection — and no pre-connect data is
    # ever replayed (plain Subject, no replay).
    h = make_transport()
    got: list[bytes] = []
    await h.transport.subscribe_stdout(got.append)
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    h.hub.emit_stdout(b"live")
    assert got == [b"live"]


async def test_subscribe_status_delivers_snapshot_on_subscribe() -> None:
    # BehaviorSubject snapshot semantics: a listener registered after a
    # report already arrived receives the *current* flags immediately on
    # subscribe — then every subsequent report.
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    h.hub.emit_status(SYNTHETIC_WORD)
    got: list[StatusFlags] = []
    await h.transport.subscribe_status(got.append)
    assert len(got) == 1
    snapshot = got[0]
    assert isinstance(snapshot, StatusFlags)
    assert int(snapshot) == SYNTHETIC_WORD
    h.hub.emit_status(0)
    assert len(got) == 2
    assert int(got[-1]) == 0


async def test_subscribe_status_replays_snapshot_on_reconnect() -> None:
    # The consumer registers its listener once per connection (same bound
    # method); the adapter's registry is idempotent by equality, and each
    # connect's arm-time BehaviorSubject replay re-syncs the current
    # snapshot — here: the seeded zero, then a real report, then the
    # fresh connection's seeded zero again.
    h = make_transport()
    got: list[StatusFlags] = []
    await h.transport.subscribe_status(got.append)
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    assert int(got[-1]) == 0  # arm-time replay of the seeded snapshot
    h.hub.emit_status(int(StatusFlag.BLE_HOST_CONNECTED))
    assert int(got[-1]) == int(StatusFlag.BLE_HOST_CONNECTED)
    await h.transport.disconnect()
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    # The re-subscribe (the consumer's per-connection pattern) delivers
    # the fresh connection's current snapshot — idempotent, no double
    # registration, and no stale pre-disconnect report leaks through.
    await h.transport.subscribe_status(got.append)
    assert int(got[-1]) == 0
    assert int(got[-2]) == 0  # the reconnect's arm-time replay
    assert int(got[-3]) == int(StatusFlag.BLE_HOST_CONNECTED)
    await h.transport.disconnect()


async def test_status_flags_passthrough_specific_bits() -> None:
    # The raw 32-bit word passes through unchanged — tested against
    # specific bits, never truthiness: two named bits set, a named bit
    # clear, and an unnamed bit preserved (the hub reports more flags
    # than StatusFlags names).
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    got: list[StatusFlags] = []
    await h.transport.subscribe_status(got.append)
    got.clear()  # discard the seeded zero snapshot
    h.hub.emit_status(SYNTHETIC_WORD)
    flags = got[-1]
    assert int(flags) == SYNTHETIC_WORD  # unnamed bit 20 survived
    assert flags & StatusFlags.USER_PROGRAM_RUNNING
    assert flags & StatusFlags.BLE_HOST_CONNECTED
    assert not flags & StatusFlags(1 << 4)  # BLE_LOW_SIGNAL bit clear
    assert not flags & StatusFlags(1 << 0)  # battery warning bit clear


async def test_status_zero_flags_is_a_valid_snapshot() -> None:
    # StatusFlags(0) is falsy — consumers must bit-test, and the adapter
    # must deliver the zero snapshot like any other (program-end edge).
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    got: list[StatusFlags] = []
    await h.transport.subscribe_status(got.append)
    assert int(got[-1]) == 0  # seeded snapshot already delivered
    h.hub.emit_status(int(StatusFlag.USER_PROGRAM_RUNNING))
    h.hub.emit_status(0)
    assert [int(f) for f in got] == [0, int(StatusFlag.USER_PROGRAM_RUNNING), 0]


async def test_status_report_running_program_and_slot_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Research addendum (issue #8, settled option (a)): data[5] (running
    # program id) and data[6] (selected slot) are decoded by pybricksdev
    # before the flags are emitted; the adapter logs both at DEBUG —
    # including the builtin-id attribution (PORT_VIEW = 129).
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    got: list[StatusFlags] = []
    await h.transport.subscribe_status(got.append)
    h.hub._running_program = 129  # port-view builtin, button-started
    h.hub._selected_slot = 2
    with caplog.at_level(logging.DEBUG, logger="brick_console.adapter"):
        h.hub.emit_status(int(StatusFlag.USER_PROGRAM_RUNNING))
    assert "PORT_VIEW (129)" in caplog.text
    assert "selected_slot=2" in caplog.text
    # The extra bytes are logging-only: the seam payload stays flags-only.
    assert int(got[-1]) == int(StatusFlag.USER_PROGRAM_RUNNING)


async def test_status_report_program_id_logged_on_change_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The hub reports status at cadence but the program-id/slot bytes are
    # logged on *change* only — per-report logging would be DEBUG noise at
    # the full report cadence, and id 0 (nothing running) renders as
    # "none", never as a slot address.
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    await h.transport.subscribe_status(lambda f: None)
    with caplog.at_level(logging.DEBUG, logger="brick_console.adapter"):
        h.hub.emit_status(int(StatusFlag.BLE_HOST_CONNECTED))
        h.hub.emit_status(int(StatusFlag.USER_PROGRAM_RUNNING))
        assert caplog.text.count("running_program=") == 0
        h.hub._running_program = 129  # port-view builtin started
        h.hub.emit_status(int(StatusFlag.USER_PROGRAM_RUNNING))
        assert caplog.text.count("running_program=") == 1
        h.hub.emit_status(int(StatusFlag.USER_PROGRAM_RUNNING))
        assert caplog.text.count("running_program=") == 1
        h.hub._running_program = 0  # back to nothing running
        h.hub.emit_status(int(StatusFlag.BLE_HOST_CONNECTED))
        assert caplog.text.count("running_program=") == 2
        assert "running_program=none" in caplog.text


async def test_on_disconnect_fires_exactly_once_deliberate() -> None:
    h = make_transport()
    counter = _DisconnectCounter()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=counter)
    await h.transport.disconnect()
    assert counter.count == 1


async def test_on_disconnect_fires_exactly_once_spontaneous() -> None:
    h = make_transport()
    counter = _DisconnectCounter()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=counter)
    h.hub.drop_connection()
    assert counter.count == 1
    # A second DISCONNECTED report (chatter) must not re-fire.
    h.hub.drop_connection()
    assert counter.count == 1


async def test_on_disconnect_exactly_once_across_deliberate_then_spontaneous() -> None:
    # The full Done-when scenario: connect → deliberate disconnect →
    # reconnect → spontaneous drop — each episode fires exactly once.
    h = make_transport()
    counter = _DisconnectCounter()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=counter)
    await h.transport.disconnect()
    assert counter.count == 1
    await h.transport.connect(handle, on_disconnect=counter)
    assert counter.count == 1  # reconnect alone fires nothing
    h.hub.drop_connection()
    assert counter.count == 2
    h.hub.drop_connection()
    await h.transport.disconnect()  # idempotent cleanup after a drop
    assert counter.count == 2


async def test_on_disconnect_fires_if_gatt_teardown_raises() -> None:
    # pybricksdev's disconnect() asserts the state report arrived; if the
    # GATT teardown raises instead, the callback still lands exactly once
    # (seam contract: the deliberate path always ends with it fired).
    h = make_transport()
    counter = _DisconnectCounter()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=counter)
    h.hub.disconnect_error = RuntimeError("teardown failed")
    with pytest.raises(RuntimeError, match="teardown failed"):
        await h.transport.disconnect()
    assert counter.count == 1


async def test_disconnect_idempotent() -> None:
    # Second disconnect() is a no-op — pybricksdev-side idempotent, and
    # on_disconnect is not re-fired.
    h = make_transport()
    counter = _DisconnectCounter()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=counter)
    await h.transport.disconnect()
    await h.transport.disconnect()
    await h.transport.disconnect()
    assert counter.count == 1
    assert [c.name for c in h.hub.calls].count("disconnect") == 1


async def test_operations_before_connect_raise_connection_error() -> None:
    # Operations outside a live connection surface as ConnectionError —
    # the never-give-up loop's catch-all, not a crash.
    transport = PybricksDevTransport(hub_factory=FakePybricksHubBLE)
    for op in (
        lambda: transport.install_and_start(Path("x.py")),
        lambda: transport.stop(),
        lambda: transport.write_stdin(b"hi"),
    ):
        with pytest.raises(ConnectionError):
            await op()


async def test_operations_after_disconnect_raise_connection_error() -> None:
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    await h.transport.disconnect()
    with pytest.raises(ConnectionError):
        await h.transport.stop()


async def test_reconnect_after_spontaneous_drop_without_disconnect_call() -> None:
    # The manager's loop parks on the disconnect event and calls
    # disconnect() later — but a reconnect must also work if it never
    # does (the stale bridge is torn down, exactly-once preserved).
    h = make_transport()
    first_counter = _DisconnectCounter()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=first_counter)
    h.hub.drop_connection()
    assert first_counter.count == 1
    second_counter = _DisconnectCounter()
    await h.transport.connect(handle, on_disconnect=second_counter)
    assert second_counter.count == 0
    h.hub.drop_connection()
    assert first_counter.count == 1  # the old bridge stays silent
    assert second_counter.count == 1


async def test_stdout_fanout_survives_reconnect() -> None:
    # The registry is adapter-level and the per-connection bridging is
    # rebuilt on every connect — a listener registered once keeps
    # receiving across reconnects (the consumer subscribes per
    # connection; this pins the stronger property too).
    h = make_transport()
    got: list[bytes] = []
    await h.transport.subscribe_stdout(got.append)
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    h.hub.emit_stdout(b"first")
    await h.transport.disconnect()
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())
    h.hub.emit_stdout(b"second")
    assert got == [b"first", b"second"]


async def test_broken_listener_does_not_kill_dispatch() -> None:
    # A raising consumer must never kill the hub's notification dispatch
    # (R1) — the healthy listener still receives its payload.
    h = make_transport()
    handle = DiscoveredPybricksHub(h.device)
    await h.transport.connect(handle, on_disconnect=_DisconnectCounter())

    def boom(data: bytes) -> None:
        raise RuntimeError("broken consumer")

    got: list[bytes] = []
    await h.transport.subscribe_stdout(boom)
    await h.transport.subscribe_stdout(got.append)
    h.hub.emit_stdout(b"payload")
    assert got == [b"payload"]


async def test_discover_rejects_unwrapped_device(monkeypatch) -> None:
    # find_device returns a BLEDevice; the wrap is the adapter's own —
    # this pins that connect() cannot accidentally receive a raw device
    # (the seam stays opaque end to end).
    async def fake_find_device(name=None, service=None, timeout=None):
        return FakeBLEDevice("Pybricks Hub", "AA:BB:CC:DD:EE:FF")

    monkeypatch.setattr("brick_console.adapter.find_device", fake_find_device)
    transport = PybricksDevTransport()
    handle = await transport.discover("Pybricks Hub")
    assert isinstance(handle, DiscoveredPybricksHub)
    assert not isinstance(handle, FakeBLEDevice)


# ----------------------------------------------------------------------
# D6 enforcement: the import ban, AST-enforced (immune to prose)
# ----------------------------------------------------------------------

SRC_ROOT = Path(adapter_module.__file__ or ".").resolve().parent
TESTS_ROOT = SRC_ROOT.parent.parent / "tests"
BANNED_PACKAGES = ("bleak", "pybricksdev")
# The convention's sanctioned exceptions, encoded:
#   - adapter.py — the implementation, the ban's whole point;
#   - test_adapter.py — the fake mirrors the real library's enum objects
#     (ConnectionState/StatusFlag) so bridge/passthrough behavior is
#     tested against reality, exactly as test_transport.py pins bits;
#   - test_transport.py — pins StatusFlags bit values *against the
#     installed library* (merged precedent).
D6_ALLOWED = {"adapter.py", "test_adapter.py", "test_transport.py"}


def _imported_packages(path: Path) -> set[str]:
    """Top-level packages actually imported by a Python file (AST —
    docstrings and comments never count)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found & set(BANNED_PACKAGES)


def test_no_bleak_or_pybricksdev_import_outside_adapter() -> None:
    # D6: adapter.py is the only server module that imports bleak or
    # pybricksdev; a reviewer grepping for the imports must find only it
    # (plus the sanctioned test files). AST-based, so prose that merely
    # mentions the packages never trips it. Recursive over the source
    # tree — a future subpackage (web/, ws/, …) is covered from day one.
    offenders: list[str] = []
    scanned = sorted(SRC_ROOT.rglob("*.py")) + sorted(TESTS_ROOT.glob("test_*.py"))
    for path in scanned:
        if path.name in D6_ALLOWED:
            continue
        found = _imported_packages(path)
        for pkg in sorted(found):
            offenders.append(f"{path}: imports {pkg}")
    assert not offenders, f"D6 violations: {offenders}"
