"""bleak/pybricksdev adapter: the only module that touches the BLE libraries.

Implements :class:`~brick_console.transport.Transport` on top of pybricksdev
2.3.2 + bleak 3.0.2. D6: every bit of hub access flows through the seam, and
this module is the single one on the far side of it — the only place bleak,
pybricksdev, and pybricksdev's RxPY subjects (``reactivex``) are imported.
Every library behavior used here is pinned with file:line cites in
``docs/research/pybricksdev-api-notes.md`` (the capability map + gotchas).

Mapping (issue #8 table):

- discover → ``pybricksdev.ble.find_device`` — scan by advertised name;
  ``asyncio.TimeoutError`` on miss passes straight through (gotcha 5: it is
  the "hub off/asleep" signal for the caller's rescan loop).
- connect → one fresh ``PybricksHubBLE(device)`` per call — pybricksdev has
  no reconnect-on-one-object API, and its own CLI builds a fresh hub object
  per connection (``cli/__init__.py:259-279``); this adapter follows that
  proven pattern. ``print_output = False`` before the handshake (gotcha 4:
  the default echoes hub stdout onto the *server's* stdout), the handshake
  wrapped in ``asyncio.wait_for`` (gotcha 5: bleak's own connect bound is
  30 s), and the hub's ``connection_state_observable`` bridged onto the
  caller's ``on_disconnect`` — fired exactly once per connection (a latch;
  pybricksdev's ``disconnect()`` relies on the same DISCONNECTED report for
  both the deliberate and the spontaneous path, so one bridge covers both).
- install_and_start → ``run(path, wait=False, print_output=False,
  line_handler=False)`` — RAM-only, never the 5 permanent slots; program end
  is observed via status edges (``USER_PROGRAM_RUNNING`` clearing), never by
  waiting here.
- stop → ``stop_user_program()`` — the hub-side command is idempotent and
  the safe first move in any mode transition (api-notes row 5).
- write_stdin → ``write_string()`` — chunking to the negotiated max write
  size is pybricksdev's job (api-notes row 7); ours is the utf-8 decode.
- subscribe_stdout → fan out ``stdout_observable`` payloads to N listeners,
  raw bytes untouched. The registry lives at the *adapter* level, stable
  across connections: the consumer re-subscribes once per connection with
  the same bound methods, so registration is idempotent by equality and
  only the per-connection bridging is rebuilt on each connect. Plain
  Subject — no replay, future events only.
- subscribe_status → ``status_observable`` snapshots; the raw 32-bit flag
  word passes through unchanged as :class:`StatusFlags` (the pybricksdev
  enum wrapper is swapped for ours — D6 containment — but the word,
  including unnamed bits, is preserved). Snapshot semantics: the current
  flags are delivered immediately on subscribe (BehaviorSubject mirror),
  then every subsequent report.
- disconnect → ``hub.disconnect()`` — pybricksdev-side idempotent; both
  deliberate and spontaneous ends fire ``on_disconnect`` exactly once.

Lifecycle (api-notes "Lifecycle"): the adapter creates no long-lived
tasks; it is a passive coroutine-driven holder — the state machine
(:mod:`~brick_console.ble_manager`) owns every task and serializes the
connect/disconnect lifecycle.

Error mapping: ``HubDisconnectError`` / ``HubPowerButtonPressedError``
during operations surface as ``ConnectionError`` (a disconnect, not a
crash — the drop itself also fires ``on_disconnect`` via the state
observable); a protocol-mismatch ``RuntimeError`` at connect is wrapped as
a connect failure (``ConnectionError``); every other operation error —
including the bleak GATT write failure that carries ``CommandError.BUSY``
— propagates untouched: BUSY is a caller-side precondition, sequenced
stop-before-install by the manager (D6), never retyped or swallowed here.

The constructor takes a hub factory (device → hub-like object) so unit
tests fake ``PybricksHubBLE`` entirely — no real BLE anywhere in this
module's tests (testing.md §"Server tests"); real-hub behavior is proven by
the hardware smoke test, not by this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from bleak.backends.device import BLEDevice
from pybricksdev.ble import find_device
from pybricksdev.ble.pybricks import StatusFlag, UserProgramId
from pybricksdev.connections import ConnectionState
from pybricksdev.connections.pybricks import (
    HubDisconnectError,
    HubPowerButtonPressedError,
    PybricksHubBLE,
)
from reactivex import Observable
from reactivex.abc import DisposableBase

from brick_console.transport import (
    DisconnectListener,
    DiscoveredHub,
    StatusFlags,
    StatusListener,
    StdoutListener,
    Transport,
)

__all__ = [
    "DiscoveredPybricksHub",
    "HubFactory",
    "PybricksDevTransport",
]

logger = logging.getLogger(__name__)

_DEFAULT_CONNECT_TIMEOUT = 10.0
"""Bound on the BLE handshake. Must stay at or below the consumer's own
bound (``BLEManagerConfig.connect_timeout``, default 15 s) so this one is
the effective limit — and this is the one that releases the central role
itself on timeout (see :meth:`PybricksDevTransport.connect`)."""

_DISCONNECT_ERRORS = (HubDisconnectError, HubPowerButtonPressedError)
"""pybricksdev exceptions meaning "the hub went away mid-operation" —
mapped to ``ConnectionError`` so operations surface as disconnects, not
crashes (the state observable fires ``on_disconnect`` in parallel)."""


def _safe(fn: Callable[..., None], /, *args: object) -> None:
    """Invoke one fan-out listener; a broken consumer must never kill the
    hub's notification dispatch (R1) — log and continue. Same discipline as
    the manager's listener fan-outs."""
    try:
        fn(*args)
    except Exception:
        logger.exception("transport listener failed; continuing")


def _describe_program_id(value: object) -> str:
    """Render a STATUS_REPORT program id for the DEBUG log.

    Ids 1–127 are user slots (an *address*, not a name — the runtime
    protocol has no read-slot command, so slot provenance stays
    host-side); 128–130 are named builtin ids (pybricksdev
    ``UserProgramId``); 0 means no program running.
    """
    if not isinstance(value, int) or value == 0:
        return "none"
    try:
        return f"{UserProgramId(value).name} ({value})"
    except ValueError:
        return str(value)


class DiscoveredPybricksHub:
    """Wrap the bleak ``BLEDevice``; expose ``name``/``address`` only.

    The handle is opaque to consumers (the seam protocol has just these
    two fields); the address is informational, never an invariant — it
    drifts after re-flashes, which is why discovery re-scans by name
    (api-notes gotcha 2).
    """

    __slots__ = ("_device",)

    def __init__(self, device: BLEDevice) -> None:
        self._device = device

    @property
    def name(self) -> str:
        # BLEDevice.name is Optional[str]; discovery matched on the
        # advertised name so it is set in practice — "" is the degenerate
        # fallback for a type-truthful property.
        return self._device.name or ""

    @property
    def address(self) -> str:
        return self._device.address


class HubFactory(Protocol):
    """Injectable ``device → hub`` construction — the real factory builds
    a ``PybricksHubBLE``; tests substitute a fake so no BLE is touched."""

    def __call__(self, device: BLEDevice) -> object: ...


def _default_hub_factory(device: BLEDevice) -> PybricksHubBLE:
    return PybricksHubBLE(device)


@runtime_checkable
class _HubProtocol(Protocol):
    """The hub-like object this adapter drives — structural, so both the
    real ``PybricksHubBLE`` and test fakes satisfy it."""

    print_output: bool

    connection_state_observable: Observable[ConnectionState]
    stdout_observable: Observable[bytes]
    status_observable: Observable[StatusFlag]

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def run(
        self,
        py_path: str,
        *,
        wait: bool,
        print_output: bool,
        line_handler: bool,
    ) -> None: ...

    async def stop_user_program(self) -> None: ...

    async def write_string(self, value: str) -> None: ...


class _DisconnectBridge:
    """Exactly-once ``on_disconnect`` delivery for one connection.

    pybricksdev reports connection state on a BehaviorSubject; the
    DISCONNECTED report arrives in both the deliberate path (``disconnect``
    → DISCONNECTING, then the bleak callback fires DISCONNECTED) and the
    spontaneous path (bleak callback only). The bridge latches on the first
    DISCONNECTED and ignores everything after — including the
    CONNECTING/DISCONNECTED chatter a failed handshake produces on the same
    observable — so the callback fires exactly once per connection.

    Armed only once the handshake has *succeeded* (a failed connect never
    had a connection to drop); the BehaviorSubject replay then covers a
    drop in the handshake→arm window.
    """

    def __init__(self, on_disconnect: DisconnectListener) -> None:
        self._on_disconnect = on_disconnect
        self._fired = False
        self._active = True

    def __call__(self, state: object) -> None:
        if self._active and not self._fired and state == ConnectionState.DISCONNECTED:
            self._fire()

    def fire_pending(self) -> None:
        """Fire unless already fired — used by :meth:`disconnect` so the
        callback still lands exactly once even when the GATT teardown
        raises or the state report never arrives (seam contract: both the
        deliberate and the spontaneous path end with it fired)."""
        if self._active and not self._fired:
            self._fire()

    def _fire(self) -> None:
        self._fired = True
        logger.debug("hub connection dropped; notifying")
        _safe(self._on_disconnect)

    def deactivate(self) -> None:
        """Silence the bridge without firing — teardown of bridging left
        over from a connection the caller never ``disconnect()``ed."""
        self._active = False


class PybricksDevTransport(Transport):
    """Transport over pybricksdev's ``PybricksHubBLE``.

    One instance is the server's capability holder, reused across
    connections; each :meth:`connect` builds a fresh hub object (the CLI
    reconnect pattern) and bridges that hub's observables onto the
    adapter-level listener registries.
    """

    def __init__(
        self,
        *,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        hub_factory: HubFactory = _default_hub_factory,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._hub_factory = hub_factory
        # Listener registries: adapter-level, stable across connections —
        # the consumer re-subscribes the same callbacks once per
        # connection, so registration is idempotent by equality.
        self._stdout_listeners: list[StdoutListener] = []
        self._status_listeners: list[StatusListener] = []
        # Per-connection state, rebuilt on every connect:
        self._hub: _HubProtocol | None = None
        self._connection_subscriptions: list[DisposableBase] = []
        self._disconnect_bridge: _DisconnectBridge | None = None
        # Last (running_program, selected_slot) pair logged — the hub
        # reports status at cadence; the extra bytes are logged on change
        # only, not per report.
        self._logged_program_state: tuple[int, int] | None = None

    # ------------------------------------------------------------------
    # Discovery / connection lifecycle
    # ------------------------------------------------------------------

    async def discover(self, name: str, *, timeout: float = 10.0) -> DiscoveredHub:
        """Scan by advertised name; wrap the bleak device opaquely.

        ``asyncio.TimeoutError`` on miss passes through unchanged — the
        caller treats it as "hub off/asleep" (AGENTS.md rule 5).
        """
        device = await find_device(name, timeout=timeout)
        return DiscoveredPybricksHub(device)

    async def connect(
        self, hub: DiscoveredHub, *, on_disconnect: DisconnectListener
    ) -> None:
        """Connect via a fresh hub object, bounded by ``connect_timeout``.

        On any abort (timeout, cancellation, handshake failure) the
        partially-connected hub is disconnected best-effort so the BLE
        central role is never left held (the #7 mid-handshake-timeout
        lesson, F6): pybricksdev's stack unwind usually releases it, and
        its ``disconnect()`` is an idempotent no-op when it already has.

        The disconnect bridge is armed only after the handshake resolves —
        a failed connect never had a connection to drop, and the same
        observable emits DISCONNECTED during every failed handshake
        (pybricksdev's exit-stack), so an early-armed bridge would fire
        spuriously. The state subject's replay covers a spontaneous drop in
        the handshake→arm window.
        """
        device = self._device_of(hub)
        hub_obj = self._hub_factory(device)
        assert isinstance(hub_obj, _HubProtocol)
        # A caller that skipped disconnect() after a previous connection
        # leaves stale bridging behind — drop it so the fresh hub is the
        # only event source (its old subscriptions are inert either way).
        self._teardown_stale_bridging()
        try:
            await asyncio.wait_for(
                self._handshake(hub_obj), timeout=self._connect_timeout
            )
        except asyncio.CancelledError:
            await self._release_role(hub_obj)
            raise
        except TimeoutError:
            # The handshake stalled past the bound — let the timeout
            # propagate (the caller maps it to its own timeout handling)
            # after releasing the role.
            await self._release_role(hub_obj)
            raise
        except Exception as exc:
            await self._release_role(hub_obj)
            # Protocol mismatch (RuntimeError), GATT/handshake errors
            # (bleak), device-gone — all connect failures per the caller's
            # taxonomy (ConnectionError = "connect refused/failed").
            raise ConnectionError(f"connect to hub failed: {exc}") from exc
        self._arm(hub_obj, on_disconnect)

    def _device_of(self, hub: DiscoveredHub) -> BLEDevice:
        """Unwrap a handle from *this* transport's discover().

        Only the concrete wrapper is accepted — the adapter does not trust
        arbitrary objects that happen to structurally satisfy the seam
        protocol.
        """
        if not isinstance(hub, DiscoveredPybricksHub):
            raise TypeError(
                "connect() expects a hub handle from this transport's "
                + f"discover(), got {type(hub).__name__}"
            )
        return hub._device

    async def _handshake(self, hub_obj: _HubProtocol) -> None:
        """Drive one full pybricksdev handshake (service discovery,
        notification enable, capabilities read — a successful return means
        ready for I/O)."""
        hub_obj.print_output = False  # gotcha 4: before any possible stdout
        await hub_obj.connect()

    async def _release_role(self, hub_obj: _HubProtocol) -> None:
        """Best-effort release of the BLE central role after an abandoned
        handshake — pybricksdev's ``disconnect()`` skips when the state
        observable is not CONNECTED, which is exactly the state a failed
        handshake leaves, so this is a no-op whenever the unwind already
        released everything."""
        try:
            await hub_obj.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("post-failure central-role release failed", exc_info=True)

    def _arm(self, hub_obj: _HubProtocol, on_disconnect: DisconnectListener) -> None:
        """Wire the freshly connected hub: the exactly-once disconnect
        bridge, then the stdout/status fan-out bridges onto the
        adapter-level registries.

        Subscribing the status observable replays its cached snapshot
        through the fan-out — a genuine report received during the
        handshake window, or pybricksdev's zero placeholder — either way a
        truthful snapshot for any listener already registered.
        """
        bridge = _DisconnectBridge(on_disconnect)
        subs: list[DisposableBase] = [
            hub_obj.connection_state_observable.subscribe(bridge)
        ]
        self._disconnect_bridge = bridge
        self._hub = hub_obj
        subs.append(hub_obj.stdout_observable.subscribe(self._fan_out_stdout))
        subs.append(hub_obj.status_observable.subscribe(self._on_status_report))
        self._connection_subscriptions = subs

    def _teardown_stale_bridging(self) -> None:
        bridge, self._disconnect_bridge = self._disconnect_bridge, None
        if bridge is not None:
            bridge.deactivate()
        self._dispose_bridging()

    def _dispose_bridging(self) -> None:
        subs, self._connection_subscriptions = self._connection_subscriptions, []
        for sub in subs:
            sub.dispose()

    # ------------------------------------------------------------------
    # The 8 seam operations
    # ------------------------------------------------------------------

    async def install_and_start(self, program: Path, *, wait: bool = False) -> None:
        """Compile ``program``, download into hub RAM, start it — never
        wait, never echo, never line-handle.

        ``wait`` is accepted for seam-signature compatibility but
        completion is always edge-observed (``USER_PROGRAM_RUNNING``
        clearing in status reports) — the mapping table pins all three
        ``run()`` flags to False. BUSY (a bleak GATT write failure while a
        program runs) propagates untouched: stop-before-install sequencing
        is caller-side (D6).
        """
        hub = self._require_hub("install_and_start")
        try:
            await hub.run(
                str(program), wait=False, print_output=False, line_handler=False
            )
        except _DISCONNECT_ERRORS as exc:
            raise ConnectionError(f"hub disconnected during install: {exc}") from exc

    async def stop(self) -> None:
        """Stop the running user program, if any (hub-side idempotent)."""
        hub = self._require_hub("stop")
        try:
            await hub.stop_user_program()
        except _DISCONNECT_ERRORS as exc:
            raise ConnectionError(f"hub disconnected during stop: {exc}") from exc

    async def write_stdin(self, data: bytes) -> None:
        """Send stdin bytes; chunking is delegated to ``write_string``
        (api-notes row 7 — pybricksdev chunks to the negotiated size)."""
        hub = self._require_hub("write_stdin")
        try:
            await hub.write_string(data.decode("utf-8"))
        except _DISCONNECT_ERRORS as exc:
            raise ConnectionError(
                f"hub disconnected during stdin write: {exc}"
            ) from exc

    async def subscribe_stdout(self, listener: StdoutListener) -> None:
        """Register for raw stdout payloads — idempotent by equality, no
        replay (the stdout Subject delivers future events only)."""
        if listener not in self._stdout_listeners:
            self._stdout_listeners.append(listener)

    async def subscribe_status(self, listener: StatusListener) -> None:
        """Register for status snapshots — idempotent by equality.

        While connected, the current flags are delivered synchronously on
        subscribe (BehaviorSubject mirror — a late subscriber cannot miss
        the current state); while disconnected there is no snapshot to
        deliver, and the next connect's arm-time replay re-syncs anyway.
        """
        if listener not in self._status_listeners:
            self._status_listeners.append(listener)
        hub = self._hub
        if hub is None:
            return

        def snapshot(flags: StatusFlag) -> None:
            _safe(listener, StatusFlags(int(flags)))

        # Subscribe → BehaviorSubject delivers the cached snapshot
        # synchronously → dispose: exactly one delivery, no lingering
        # observer (the persistent bridge already covers future reports).
        hub.status_observable.subscribe(snapshot).dispose()

    async def disconnect(self) -> None:
        """Deliberate disconnect — idempotent; both deliberate and
        spontaneous ends fire ``on_disconnect`` exactly once."""
        hub, self._hub = self._hub, None
        bridge, self._disconnect_bridge = self._disconnect_bridge, None
        try:
            if hub is not None:
                await hub.disconnect()
        finally:
            # Exactly-once guarantee even if the GATT teardown raises or
            # the DISCONNECTED report never arrives (pybricksdev's own
            # disconnect asserts it did — this covers the assert failing).
            if bridge is not None:
                bridge.fire_pending()
                bridge.deactivate()
            self._dispose_bridging()

    # ------------------------------------------------------------------
    # Fan-out plumbing
    # ------------------------------------------------------------------

    def _fan_out_stdout(self, data: bytes) -> None:
        """Forward every raw stdout payload to all registered listeners,
        untouched (D1/D4: splitting, parsing, and WS fan-out are consumer
        concerns — the transport hands over bytes only)."""
        for listener in tuple(self._stdout_listeners):
            _safe(listener, data)

    def _on_status_report(self, flags: StatusFlag) -> None:
        """Forward every status report to all registered listeners, the
        raw 32-bit flag word unchanged (pybricksdev's enum wrapper swapped
        for ours — D6 containment — the word, unnamed bits included,
        preserved).

        STATUS_REPORT on protocol ≥ 1.4 also carries two bytes the flags
        word does not: data[5] = running program id, data[6] = selected
        slot — decoded by pybricksdev into ``_running_program`` /
        ``_selected_slot`` before the flags are emitted
        (connections/pybricks.py:270-278; also specified in the Pybricks
        BLE profile doc's v1.4.0 section). Per the settled option (a) in
        the research addendum comment on issue #8, the seam stays
        flags-only and the adapter logs both bytes here at DEBUG — no seam
        change.
        """
        word = StatusFlags(int(flags))
        for listener in tuple(self._status_listeners):
            _safe(listener, word)
        hub = self._hub
        if isinstance(hub, _HubProtocol):
            running_program = getattr(hub, "_running_program", 0)
            selected_slot = getattr(hub, "_selected_slot", 0)
            if (running_program, selected_slot) != self._logged_program_state:
                self._logged_program_state = (running_program, selected_slot)
                logger.debug(
                    "status report: running_program=%s selected_slot=%s",
                    _describe_program_id(running_program),
                    selected_slot,
                )

    def _require_hub(self, op: str) -> _HubProtocol:
        """A seam operation outside a live connection is a caller-side
        sequencing bug (the state machine serializes) — surfaced as
        ``ConnectionError`` so the never-give-up loop still handles it."""
        hub = self._hub
        if hub is None:
            raise ConnectionError(f"{op} with no connection")
        return hub
