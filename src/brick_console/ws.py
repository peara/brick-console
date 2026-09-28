"""WS gateway: the dashboard's push transport (issue #11; R2, decision D7).

Serves ``GET /ws``: telemetry/log/state envelopes to browsers (R2's
transport) with join replay so the dashboard survives refresh (F1), plus
mock mode (``?mock=1``) — the in-process synthetic source that runs the
dashboard with the hub off or BLE busy (architecture §2.3).

Envelope rules (D7 "Envelope key naming", superseding architecture §3.2's
older log example that reused ``"t"``):

- The envelope kind key is ``"type"``: ``telemetry`` | ``log`` | ``state``.
- ``"t"`` appears ONLY inside telemetry ``data`` — it is the telemetry
  event's kind key, nothing else may use it.
- Telemetry ``data`` is the event's D7 wire object verbatim, produced by
  :func:`brick_console.events.encode` (encode → JSON parse) — the
  identical wire schema as live hub data, never a hand-built dict.
- Log ``data`` is ``{"src": "stdout", "line": "…"}`` (D7's suggested shape;
  never ``"t"``). The store's raw-line ring keeps the verbatim bytes; the
  envelope's ``line`` is the browser view — UTF-8 with replacement for
  undecodable bytes, so nothing the hub prints is ever dropped (D7
  raw-log-primary).
- State ``data`` is ``{"state": …, "reason": …}`` (D7 terminology: server
  state OFFLINE/ADVERTISING/AGENT/PROGRAM, architecture §4). The EXTERNAL
  overlay (F6) is derived CLIENT-side from the disconnect reason — the
  server never computes or sends it.
- ``hub`` is the hub label: the cached ``hub_info`` name once the hub has
  reported its identity, else the managed-hub fallback
  (:data:`FALLBACK_HUB_NAME`) — resolved per envelope so a mid-session
  reconnect or rename propagates. Mock mode needs no special case: its
  ``hub_info`` name IS the synthetic identity (:data:`MOCK_HUB_NAME`); the
  multi-hub namespace (R10) will ride this same field (D7).

Join sequence (D7-ordered, never reordered): (1) one ``state`` envelope —
the current server state + reason; (2) the cached ``hub_info`` snapshot if
one exists — *before* live and replay (D7); absent when no hub has ever
reported identity (the state envelope already said why); (3) ring replay of
the store's current-connection slice — default the whole slice (bounded by
ring capacity), capped via ``?replay=N`` (``0`` or negative = none);
(4) the live stream.

Replay/live consistency: the store's ``next_ordinal`` is read and the three
live subscriptions are taken in one synchronous step (no ``await`` between
them — the loop is single-threaded, so no event can land in that window).
Replay is then filtered to ordinals below the boundary: every pre-boundary
event is replayed, every post-boundary event arrives live — exactly
complementary, no gap, no duplicate, even though the join sends ``await``
while live events keep flowing.

Backpressure policy (per client): a bounded send queue (depth
:data:`CLIENT_QUEUE_DEPTH`) drained by one sender task; on overflow the
OLDEST envelope is dropped and the newest kept — point-in-time dashboard
values are worthless stale, so latest wins; the drop is counted and logged
at DEBUG (a deployment choice, the same level discipline as the BLE
manager's stdout logging). A burst backlog beyond the depth therefore
skips ahead, not behind. Accepted M1 trade-off: a ``state`` envelope may
be dropped in such a burst — the next transition (or a rejoin) restores
it, and stale state is no more useful than stale telemetry.

Inbound messages: none are defined in M1 (run/stop is M2, control is M3)
— received messages are logged at DEBUG and ignored.

Mock mode is a parallel universe: its own store, its own listener
registry, its own synthetic AGENT state — synthetic events never mix with
a live hub's ring, and a live client never sees mock data (or vice versa).
The cadence loop runs only while at least one mock client is connected
(refcounted) and emits through the real ``events.encode`` →
``TelemetryParser`` path, so the schema path is exercised identically to
live mode. ``sleep``/``clock`` are injectable so tests never really sleep
(the same discipline as the BLE manager, testing.md).

No BLE anywhere in this module: it imports no
:mod:`brick_console.adapter` and no ``Transport`` — the manager seam is
structural (the same D6 discipline); the gateway talks only to the
subscription surface, the store, and the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from brick_console.events import (
    Battery,
    HubInfo,
    Imu,
    Port,
    TelemetryEvent,
    encode,
)
from brick_console.parsing import TelemetryParser
from brick_console.store import TelemetryStore

__all__ = [
    "CLIENT_QUEUE_DEPTH",
    "FALLBACK_HUB_NAME",
    "MOCK_HUB_NAME",
    "ClientQueue",
    "EventSource",
    "MockSource",
    "hub_label",
    "log_envelope",
    "register_routes",
    "state_envelope",
    "telemetry_envelope",
]

logger = logging.getLogger(__name__)

CLIENT_QUEUE_DEPTH = 256
"""Per-client send-queue depth. Full telemetry cadence is ≈ 70 lines/s
(D7 throughput note), so 256 ≈ 3.7 s of headroom for a slow consumer
(e.g. a backgrounded browser tab) before drop-oldest kicks in."""

FALLBACK_HUB_NAME = "Pybricks Hub"
"""Hub label before the hub has ever reported identity: the console
manages exactly one known hub (R10 multi-hub is future), and this is the
name the service scans for."""

MOCK_HUB_NAME = "Pybricks Hub (mock)"
"""Mock mode's synthetic hub identity — its own ``hub_info`` name, so the
label rule ("cached hub_info name, else fallback") serves both universes."""

MOCK_STATE_REASON = "mock source: simulated hub (architecture §2.3)"

_TICK_PERIOD_S = 0.1
"""One mock cycle: imu + fake port lines (~10 Hz per D7 cadence)."""

_BATTERY_EVERY_TICKS = 10
"""Battery cadence: every Nth tick ≈ 1 Hz (D7: battery ~1 Hz)."""


class EventSource(Protocol):
    """The structural seam the gateway serves from (D6-style, not a
    concrete class): the live manager, or the mock source under
    ``?mock=1``. Satisfied structurally by
    :class:`~brick_console.ble_manager.BLEManager`, the run command's
    :class:`~brick_console.run.StubManager` (no-op subscriptions — an
    offline stub fires nothing), and :class:`MockSource`."""

    @property
    def state(self) -> str: ...

    @property
    def state_reason(self) -> str: ...

    def subscribe_state(
        self, listener: Callable[[str, str, float], None]
    ) -> Callable[[], None]: ...

    def subscribe_telemetry(
        self, listener: Callable[[TelemetryEvent], None]
    ) -> Callable[[], None]: ...

    def subscribe_raw(
        self, listener: Callable[[bytes], None]
    ) -> Callable[[], None]: ...


def _guarded_call(fn: Callable[..., None], /, *args: object) -> None:
    """Invoke one fan-out listener; a broken consumer must never kill the
    feed — log and continue (the BLE manager's ``_safe_call`` discipline)."""
    try:
        fn(*args)
    except Exception:
        logger.exception("event listener failed; continuing")


# ---------------------------------------------------------------------------
# Envelopes — pure builders, the D7 wire contract (pinned by tests)
# ---------------------------------------------------------------------------


def _wire_data(event: TelemetryEvent) -> dict[str, object]:
    """The event's D7 wire object via the real encoder: ``encode`` → JSON
    parse. Building ``data`` any other way would fork the schema; this
    guarantees telemetry ``data`` is the wire object — ``"t"`` included —
    by construction."""
    return json.loads(encode(event))


def state_envelope(hub: str, state: str, reason: str) -> dict[str, object]:
    """Build the ``state`` envelope: ``data`` carries the state name +
    reason (D7). The EXTERNAL overlay is derived client-side (F6) — it is
    deliberately absent here."""
    return {
        "type": "state",
        "hub": hub,
        "data": {"state": str(state), "reason": reason},
    }


def telemetry_envelope(hub: str, event: TelemetryEvent) -> dict[str, object]:
    """Build the ``telemetry`` envelope: ``data`` is the event's D7 wire
    object (``"t"`` is the telemetry kind key — the only place it lives)."""
    return {"type": "telemetry", "hub": hub, "data": _wire_data(event)}


def log_envelope(hub: str, line: bytes) -> dict[str, object]:
    """Build the ``log`` envelope (D7 log shape): ``{"src": "stdout",
    "line": …}`` — never ``"t"``. ``line`` is the raw stdout line decoded
    as UTF-8 with replacement for undecodable bytes (the store's
    raw-line ring keeps the verbatim bytes; this is the browser view)."""
    return {
        "type": "log",
        "hub": hub,
        "data": {"src": "stdout", "line": line.decode("utf-8", errors="replace")},
    }


def hub_label(store: TelemetryStore | None) -> str:
    """Resolve the envelope ``hub`` label: the cached ``hub_info`` name
    (the hub's own identity claim, latest-wins per D7) when present, else
    the managed-hub fallback."""
    if store is not None and store.hub_info is not None:
        return store.hub_info.name
    return FALLBACK_HUB_NAME


# ---------------------------------------------------------------------------
# Per-client bounded send queue — drop-oldest backpressure
# ---------------------------------------------------------------------------


class ClientQueue:
    """Bounded send queue for one client: drop-oldest on overflow.

    Producer side is synchronous (:meth:`put` — called from fan-out
    listeners on the event loop), consumer side awaits :meth:`get` from
    the client's sender task. On overflow the oldest item is dropped so
    the newest survives (see the module docstring's policy), the drop is
    counted in :attr:`dropped` and logged at DEBUG. Loop-thread only —
    the store's same-thread discipline applies.
    """

    def __init__(self, depth: int = CLIENT_QUEUE_DEPTH) -> None:
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=depth)
        self.dropped = 0

    def put(self, envelope: dict[str, object]) -> None:
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()  # drop oldest, keep newest
            self._queue.put_nowait(envelope)
            self.dropped += 1
            logger.debug(
                "ws client queue overflow: dropped oldest (%d so far)", self.dropped
            )

    async def get(self) -> dict[str, object]:
        return await self._queue.get()

    def qsize(self) -> int:
        """Current depth (test/observability access to the bounded slot)."""
        return self._queue.qsize()


# ---------------------------------------------------------------------------
# The /ws route
# ---------------------------------------------------------------------------


def register_routes(app: FastAPI) -> None:
    """Register the ``GET /ws`` route on the app — call BEFORE the static
    mount at ``/`` (routes registered first win; the mount is fallback).

    Live mode (default) serves the manager's fan-out and the app's
    injected store; ``?mock=1`` serves :class:`MockSource` instead (its
    own store and listeners — a parallel universe, see the module
    docstring). ``?replay=N`` caps the join replay (default: the whole
    current-connection slice).
    """

    @app.websocket("/ws")
    async def ws_endpoint(
        websocket: WebSocket, mock: bool = False, replay: int | None = None
    ) -> None:
        await websocket.accept()
        if mock:
            source = _mock_source(app)
            source.client_enter()
            try:
                await _serve_client(websocket, source, source.store, replay)
            finally:
                await source.client_exit()
        else:
            await _serve_client(websocket, app.state.manager, app.state.store, replay)


def _mock_source(app: FastAPI) -> MockSource:
    """Lazily construct (or fetch) the app's mock source — the refcounted
    cadence task starts on the first mock client and stops on the last
    exit. Stored on ``app.state`` (no globals); tests pre-inject their own
    instance there for cadence control."""
    source: MockSource | None = getattr(app.state, "mock_source", None)
    if source is None:
        source = MockSource()
        app.state.mock_source = source
    return source


async def _serve_client(
    websocket: WebSocket,
    source: EventSource,
    store: TelemetryStore | None,
    replay: int | None,
) -> None:
    """One client's lifetime: join sequence, live stream, cleanup.

    Join (D7-ordered): state envelope → cached ``hub_info`` snapshot (if
    any) → ring replay (default the whole current-connection slice, cap
    ``replay``) → live. Replay/live consistency: the store's
    ``next_ordinal`` and the three subscriptions are taken in one
    synchronous step; replay is filtered to ordinals below the boundary —
    every pre-boundary event is replayed, every post-boundary one arrives
    live (no gap, no duplicate; see the module docstring).

    Cleanup is genuinely unconditional: the ``try`` starts right after
    the subscriptions (join sends included) and the ``finally``
    unsubscribes FIRST — a client that vanishes mid-join, or a sender
    task that dies with a stored exception, leaves no listener, no task,
    no queue behind.
    """
    queue = ClientQueue()

    def hub() -> str:
        return hub_label(store)

    def on_state(state: str, reason: str, timestamp: float) -> None:
        queue.put(state_envelope(hub(), state, reason))

    def on_telemetry(event: TelemetryEvent) -> None:
        queue.put(telemetry_envelope(hub(), event))

    def on_raw(raw: bytes) -> None:
        queue.put(log_envelope(hub(), raw))

    # Synchronous boundary step (no await anywhere in this block): events
    # already in the ring have ordinal < boundary and are replayed; events
    # appended from here on are captured by the live subscriptions.
    boundary = store.next_ordinal if store is not None else 0
    unsubs = [
        source.subscribe_state(on_state),
        source.subscribe_telemetry(on_telemetry),
        source.subscribe_raw(on_raw),
    ]
    sender: asyncio.Task[None] | None = None
    try:
        # Join sequence (state → snapshot → replay), in D7 order — inside
        # the try so a mid-join send failure still unsubscribes (no leak).
        await websocket.send_json(
            state_envelope(hub(), source.state, source.state_reason)
        )
        snapshot_sent = False
        if store is not None and store.hub_info is not None:
            await websocket.send_json(telemetry_envelope(hub(), store.hub_info))
            snapshot_sent = True
        if store is not None:
            count = store.event_capacity if replay is None else replay
            for ordinal, event in store.replay_events(count):
                if ordinal >= boundary:
                    continue  # post-boundary: arrives live instead
                if snapshot_sent and isinstance(event, HubInfo):
                    # D7: hub_info is once-per-connect — already delivered
                    # as the snapshot (the cache mirrors the appended
                    # event).
                    continue
                await websocket.send_json(telemetry_envelope(hub(), event))

        # Live stream: one sender task drains the bounded queue in order.
        sender = asyncio.create_task(_send_loop(websocket, queue), name="ws-sender")
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            # Inbound messages: none defined in M1 — document and ignore.
            logger.debug("inbound ws message ignored (none defined in M1)")
    except WebSocketDisconnect:
        pass  # starlette variants raise instead of returning the message
    finally:
        # Unsubscribe FIRST and unconditionally: a join-phase send failure
        # or a sender task that died with a stored exception must never
        # skip listener cleanup (no ghost fan-out into a dead queue).
        for unsubscribe in unsubs:
            unsubscribe()
        if sender is not None:
            sender.cancel()
            # Swallow every exit the sender can take: cancelled, returned
            # quietly on a dying socket, or dead with a stored exception
            # raised by the ASGI send (e.g. ConnectionResetError racing a
            # normal disconnect). Retrieving it here also prevents the
            # "Task exception was never retrieved" journal noise.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender


async def _send_loop(websocket: WebSocket, queue: ClientQueue) -> None:
    """Drain one client's queue to the socket, in order, forever.

    A send failure means the socket is dying: log and exit quietly. The
    handler's receive loop observes the disconnect and runs the shared
    cleanup — and its ``finally`` retrieves this task's exit whatever it
    was, so even an unexpected send exception never leaks listeners.
    """
    while True:
        envelope = await queue.get()
        try:
            await websocket.send_json(envelope)
        except (RuntimeError, WebSocketDisconnect, OSError):
            # The socket is going away (starlette raises either type on a
            # dead/dying session; OSError covers transport-level resets).
            logger.debug("ws send failed (client going away); sender exiting")
            return


# ---------------------------------------------------------------------------
# Mock source — the in-process synthetic hub (architecture §2.3)
# ---------------------------------------------------------------------------


class MockSource:
    """Synthetic in-process hub for ``?mock=1`` (architecture §2.3): no
    BLE, no ``Transport``, no adapter import — typed events are built
    here, emitted through the real ``events.encode`` →
    ``TelemetryParser.feed_with_raw`` path (the identical schema path as
    live mode), and routed to this source's own store + listener fan-out
    exactly the way the BLE manager routes hub stdout (D7
    raw-log-primary: raw line first, parsed event on top, malformed lines
    included).

    Identity: ``hub_info`` is emitted once at construction (the simulated
    connect; D7: once per connect), so every join sees the snapshot —
    clients joining mid-run get it from the cache. State is a constant
    synthetic AGENT — there are no transitions to simulate, so
    ``subscribe_state`` is accepted and never fires.

    Cadence (D7): imu + two fake port devices per ~10 Hz tick, battery
    every 10th tick (~1 Hz). The cadence loop runs only while clients are
    connected: ``client_enter()`` starts it on the first client,
    ``client_exit()`` stops it on the last (:meth:`stop` is the idempotent
    shutdown hook). ``sleep``/``clock`` are injectable so tests never
    really sleep.
    """

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = TelemetryStore()
        self._parser = TelemetryParser(clock=clock)
        self._sleep = sleep
        self._state_listeners: list[Callable[[str, str, float], None]] = []
        self._telemetry_listeners: list[Callable[[TelemetryEvent], None]] = []
        self._raw_listeners: list[Callable[[bytes], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._clients = 0
        self._tick = 0
        self._store.mark_connection_start()  # the simulated connect begins
        self._emit(HubInfo(name=MOCK_HUB_NAME, firmware="4.0.1", model="technichub"))

    # -- EventSource surface (structural — see the protocol) --------------

    @property
    def state(self) -> str:
        return "agent"

    @property
    def state_reason(self) -> str:
        return MOCK_STATE_REASON

    @property
    def store(self) -> TelemetryStore:
        """The mock universe's own ring — never the app's store."""
        return self._store

    @property
    def clients(self) -> int:
        """Current mock-client count (refcount for the cadence task)."""
        return self._clients

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The cadence task while running, else ``None``."""
        return self._task

    def subscribe_state(
        self, listener: Callable[[str, str, float], None]
    ) -> Callable[[], None]:
        """Accepted, never fired — a constant synthetic AGENT has no
        transitions to simulate."""

        def unsubscribe() -> None:
            return None

        return unsubscribe

    def subscribe_telemetry(
        self, listener: Callable[[TelemetryEvent], None]
    ) -> Callable[[], None]:
        """Register for each synthetic parsed event; idempotent
        unsubscribe, like the manager's pattern."""
        return _subscribe_into(self._telemetry_listeners, listener)

    def subscribe_raw(self, listener: Callable[[bytes], None]) -> Callable[[], None]:
        """Register for each synthetic raw line; idempotent unsubscribe."""
        return _subscribe_into(self._raw_listeners, listener)

    # -- Lifecycle ---------------------------------------------------------

    def client_enter(self) -> None:
        """Register a mock client; start the cadence loop on the first."""
        self._clients += 1
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="ws-mock-source")

    async def client_exit(self) -> None:
        """Unregister a mock client; stop the cadence loop on the last."""
        self._clients = max(0, self._clients - 1)
        if self._clients == 0:
            await self.stop()

    async def stop(self) -> None:
        """Cancel the cadence loop (idempotent) — last-client exit lands
        here; app shutdown would too."""
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def run(self) -> None:
        """The cadence loop: imu + two fake port devices per tick (~10
        Hz), battery every 10th tick (~1 Hz) — D7 cadence, forever until
        cancelled (``stop``). Every event flows through the real
        encode → parser path before the store and fan-out."""
        while True:
            self._tick += 1
            for event in self._cycle_events(self._tick):
                self._emit(event)
            if self._tick % _BATTERY_EVERY_TICKS == 0:
                self._emit(self._battery(self._tick))
            await self._sleep(_TICK_PERIOD_S)

    # -- Internals ----------------------------------------------------------

    def _cycle_events(self, tick: int) -> list[TelemetryEvent]:
        """One tick's synthetic events: imu + a Motor on port A + a
        ColorSensor on port B — a couple of fake port devices, mildly
        time-varying so motion on the dashboard is visible."""
        return [
            Imu(
                accel=(120 + tick % 10, -980, 9810 + tick % 7),
                gyro=(1, -2, tick % 5),
                up="top",
            ),
            Port(
                port="A",
                device="Motor",
                angle_deg=(tick * 3) % 360,
                speed_dps=0,
                load_mnm=0,
            ),
            Port(
                port="B",
                device="ColorSensor",
                reflection_pct=30 + tick % 10,
                ambient_pct=10 + tick % 5,
                hue_deg=tick % 360,
                saturation_pct=80,
                value_pct=90,
                color="red",
            ),
        ]

    def _battery(self, tick: int) -> TelemetryEvent:
        """Synthetic battery event (~1 Hz): plausible 2S Li-ion values."""
        return Battery(
            voltage_mv=8085 - tick % 20, current_ma=42, percent=87 - tick % 5
        )

    def _emit(self, event: TelemetryEvent) -> None:
        """Encode → real parser → route: the identical schema path as a
        live hub's stdout (one event per call)."""
        self._route(self._parser.feed_with_raw(encode(event)))

    def _route(self, pairs: list[tuple[bytes, TelemetryEvent | None]]) -> None:
        """Route parsed ``(raw, event)`` pairs the way the manager's
        stdout pipe does: raw line to the ring + log fan-out first, then
        the parsed event to the ring, the ``hub_info`` cache, and the
        telemetry fan-out (malformed lines included in the raw path)."""
        for raw, event in pairs:
            self._store.append_raw_line(raw)
            for listener in tuple(self._raw_listeners):
                _guarded_call(listener, raw)
            if event is None:
                continue  # malformed: counted and logged by the parser
            self._store.append_event(event)
            if isinstance(event, HubInfo):
                self._store.set_hub_info(event)
            for listener in tuple(self._telemetry_listeners):
                _guarded_call(listener, event)


def _subscribe_into[Listener](
    listeners: list[Listener], listener: Listener
) -> Callable[[], None]:
    """The manager's subscription pattern: idempotent unsubscribe via
    list remove (``ValueError`` suppressed)."""
    listeners.append(listener)

    def unsubscribe() -> None:
        with contextlib.suppress(ValueError):
            listeners.remove(listener)

    return unsubscribe
