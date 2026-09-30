"""BLE manager: server state machine + auto-reconnect loop over Transport.

(implements the BLE-manager milestone item, architecture §4/§4.5; decision D6.)

Implements the always-on requirement (R1): auto-discover, connect, install
the ``brick_telemetry`` agent, and recover — unattended, forever. This module
is the server's *only* caller of :class:`~brick_console.transport.Transport`
(D6); it imports no bleak (the bleak/pybricksdev adapter is a separate
module) and all its tests drive a fake Transport.

State model (architecture §4 — the canonical table):

    OFFLINE → ADVERTISING → AGENT ⇄ PROGRAM, every disconnect → OFFLINE.

- OFFLINE — hub not seen; the bounded rescan loop runs: first retry
  immediate, then backoff doubling to a ~10 s ceiling (F6). The service's own
  loop is the sanctioned exemption to AGENTS.md rule 5 — it never gives up.
- ADVERTISING — hub advertisement detected; connect attempt in flight.
- AGENT — connected; the agent wrapper is installed and running.
- PROGRAM — a user program owns the hub. M1 never deliberately enters it
  (the Run button is M2), but a program started from the CLI or an external
  client can appear mid-session; when it ends the agent is reinstalled
  automatically (§4 rule 2).

Program-lifecycle edge derivation (the subtle part): the *agent is itself a
user program* on the hub, so ``install_and_start`` flips
``USER_PROGRAM_RUNNING`` too. The expected-start latch is armed in the
await-free window between ``stop()`` resolving and ``install_and_start``
being called — arming before the stop would swallow any program that grabs
the hub while the stop write is in flight, arming after the install would
misread the agent's own start (the hub reports it while the RPC is still in
flight). Any CLEAR edge disarms the latch (a clear proves the armed latch
stale — the hub was stopped or running something else). Only an *unexpected*
set edge (somebody else started a program) enters PROGRAM. Status reports
are snapshots (the seam contract); edges are derived here. During session
setup (``_settling``) snapshots only update the running flag — the setup
sequence itself is the transition into AGENT.

Data pipeline (D7 raw-log-primary): per connect the manager builds a fresh
:class:`~brick_console.parsing.TelemetryParser` (dropped on disconnect, so
per-connection state such as ``malformed_count`` never leaks across
connections). Stdout chunks go through exactly one split
(:meth:`TelemetryParser.feed_with_raw`): every raw line is routed to the
raw-line path *first* — the telemetry store's raw-line ring (primary
retention) and the service log (DEBUG; ~70 lines/s at full cadence, so the
level is a deployment choice) — then telemetry parsing attaches on top:
parsed events go to the store's event ring, ``hub_info`` to its snapshot
cache, and the ``subscribe_telemetry`` fan-out (the WS gateway's live feed).
Nothing the hub prints is ever dropped.

Session model (§4 rule 4): a session = one connect→disconnect episode,
numbered per discovery attempt; every state transition is recorded with a
timestamp and its session id. ``transitions()`` returns the bounded history;
``session_transitions(n)`` one episode's slice.

Reconnect budget (§4 rule 3): full power-cycle reconnect ≤ 5 s — the
immediate first retry after a disconnect exists for this; the backoff then
walks 1, 2, 4, 8 … seconds up to the ceiling. The backoff resets only when a
connection reaches AGENT: connect failures (e.g. another central holding the
hub) never reset it, and the manager never fights for the central role (F6,
api-notes gotcha 1).

Framework-free asyncio, no RxPY (D6): callbacks only. ``clock`` and ``sleep``
are injectable so backoff schedules and budgets are deterministic in tests;
``clock`` stamps both state transitions and the parser's ``received_at``
(float UNIX seconds, D7).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from brick_console.events import TelemetryEvent
from brick_console.fanout import (
    TelemetryRouter,
    TelemetrySink,
    fan_out,
    subscribe_into,
)
from brick_console.parsing import TelemetryParser
from brick_console.transport import DisconnectListener, StatusFlags, Transport

__all__ = [
    "BLEManager",
    "BLEManagerConfig",
    "HubState",
    "RawLineListener",
    "StateListener",
    "StateTransition",
    "TelemetryListener",
    "TelemetrySink",
]

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_PROGRAM = Path(__file__).resolve().parents[2] / "agent" / "agent_main.py"
_TRANSITION_HISTORY = 100

EXTERNAL_TAKEOVER_REASON = "external client took the hub"
"""Canonical reason on the F6 takeover path (kicked by a second BLE
central). The dashboard's token list already matches this wording — the
overlay lights up with zero client changes. Pinned by unit test; the
WS gateway forwards ``state_reason`` verbatim, so this string is wire
contract (issue #24 evidence: the kick produces NO disconnect callback,
so it can only ever be reported by the probe watchdog, never by a
disconnect event)."""


class HubState(StrEnum):
    """Server-tracked hub states (architecture §4 — the canonical table)."""

    OFFLINE = "offline"
    ADVERTISING = "advertising"
    AGENT = "agent"
    PROGRAM = "program"


@dataclass(frozen=True)
class StateTransition:
    """One recorded state change (§4 rule 4): from, to, when, why, which session."""

    from_state: HubState
    to_state: HubState
    timestamp: float
    reason: str
    session: int


RawLineListener = Callable[[bytes], None]
"""Notified with each raw stdout line (verbatim, terminator-stripped) —
the WS gateway's live ``log`` fan-out. Malformed lines are included: raw
retention is primary (D7) and the dashboard console pane shows them."""

StateListener = Callable[[HubState, str, float], None]
"""Notified on every state change with ``(state, reason, timestamp)`` — the
WS gateway's state feed; the interface is deliberately this small."""

TelemetryListener = Callable[[TelemetryEvent], None]
"""Notified with each parsed telemetry event — live fan-out to the WS
gateway; the telemetry store is fed in parallel and owns replay/history."""


# ``TelemetrySink`` (imported from fanout above) is re-exported: the store
# seam used to be defined here, and existing imports keep working.


@dataclass(frozen=True)
class BLEManagerConfig:
    """Tuning knobs; clock/sleep are injected on the manager separately."""

    hub_name: str = "Pybricks Hub"
    """Scan selector — the advertised name only; the address drifts."""
    agent_program: Path = _DEFAULT_AGENT_PROGRAM
    """The agent wrapper the manager installs (RAM-only, never the 5 slots)."""
    scan_timeout: float = 10.0
    """Per-attempt BLE scan window."""
    connect_timeout: float = 15.0
    """``asyncio.wait_for`` bound on connect — bleak's own default is 30 s
    (api-notes gotcha 5)."""
    backoff_base: float = 1.0
    """Second retry delay; doubles per attempt up to the ceiling."""
    backoff_ceiling: float = 10.0
    """Rescan ceiling (F6) — never fight for the single BLE central role."""
    liveness_timeout: float = 5.0
    """Park wait sliced into staleness checks. The hub's own status reports
    (2 Hz push) are the liveness signal — no GATT reads (2026-09-30
    counter-evidence: a program start swaps the hub's GATT table, so
    cached-object reads fail with the kick's exact error shape while the
    link is alive, mislabeling live sessions as takeovers; see D8)."""
    liveness_grace: float = 2.0
    """Window after status silence for the disconnect callback to still
    arrive. Callback within it → ordinary ``hub disconnected`` (power-off
    shape); silence → the takeover reason (kick shape: the 2026-09-29
    evidence showed notifications stop AND no callback ever fires)."""


class _Backoff:
    """Rescan schedule: first retry immediate, then ``base`` × 2^k seconds,
    capped at the ceiling forever after. Reset only when a connection reaches
    AGENT — connect failures keep escalating, so another central holding the
    hub never gets fought (F6)."""

    def __init__(self, *, base: float, ceiling: float) -> None:
        self._base = base
        self._ceiling = ceiling
        self._attempt = 0

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        if self._attempt == 0:
            self._attempt = 1
            return 0.0
        delay = self._base
        # Double per attempt, but stop doubling once the ceiling is reached —
        # the exponent must never grow unbounded (float overflow after ~1023
        # attempts would kill the always-on loop; R1).
        for _ in range(self._attempt - 1):
            delay *= 2
            if delay >= self._ceiling:
                self._attempt += 1
                return self._ceiling
        self._attempt += 1
        return delay


@dataclass
class _Connection:
    """Per-connection state, dropped wholesale on disconnect (D7): the parser
    (fresh per connect), the disconnect latch the loop parks on, and the
    last-status-report stamp the liveness watchdog rides (D8)."""

    parser: TelemetryParser
    disconnect: asyncio.Event
    last_status_at: float


class BLEManager:
    """Owns the hub connection lifecycle: discover → connect → agent install
    → telemetry pipes, with automatic recovery (architecture §4.5).

    One instance owns the process's single BLE central role (F6) — never run
    two. All hub access flows through the injected :class:`Transport` (D6);
    state changes fan out to ``StateListener``\\s, parsed events to
    ``TelemetryListener``\\s, and everything lands in the
    :class:`TelemetrySink` (the telemetry store) as well.
    """

    def __init__(
        self,
        transport: Transport,
        store: TelemetrySink,
        *,
        config: BLEManagerConfig | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._store = store
        self._config = config if config is not None else BLEManagerConfig()
        self._clock = clock
        self._sleep = sleep
        self._state = HubState.OFFLINE
        self._state_reason = "service starting; hub not scanned yet"
        self._transitions: deque[StateTransition] = deque(maxlen=_TRANSITION_HISTORY)
        self._state_listeners: list[StateListener] = []
        self._telemetry_listeners: list[TelemetryListener] = []
        self._raw_listeners: list[RawLineListener] = []
        self._router = TelemetryRouter(
            store,
            raw_listeners=self._raw_listeners,
            telemetry_listeners=self._telemetry_listeners,
        )
        self._backoff = _Backoff(
            base=self._config.backoff_base, ceiling=self._config.backoff_ceiling
        )
        self._conn: _Connection | None = None
        self._session = 0
        self._program_running = False
        self._expected_running = False
        self._settling = False
        self._reinstall_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public surface (the WS gateway / service supervisor consume these)
    # ------------------------------------------------------------------

    @property
    def state(self) -> HubState:
        """Current server-tracked state."""
        return self._state

    @property
    def state_reason(self) -> str:
        """Why the current state was entered (the last transition's reason —
        or a non-transition note such as a rescan miss). The WS gateway
        derives the EXTERNAL overlay from it (F6)."""
        return self._state_reason

    @property
    def session(self) -> int:
        """Current (or most recent) connect→disconnect episode's number."""
        return self._session

    @property
    def config(self) -> BLEManagerConfig:
        return self._config

    @property
    def malformed_count(self) -> int:
        """Current connection's cumulative malformed-line count — 0 while
        offline (the parser is per-connection and dropped on disconnect)."""
        if self._conn is None:
            return 0
        return self._conn.parser.malformed_count

    def subscribe_state(self, listener: StateListener) -> Callable[[], None]:
        """Register for ``(state, reason, timestamp)`` on every change.

        Returns an idempotent unsubscribe function. Fan-out is guarded: a
        raising listener is logged and never kills the loop.
        """
        return subscribe_into(self._state_listeners, listener)

    def subscribe_telemetry(self, listener: TelemetryListener) -> Callable[[], None]:
        """Register for each parsed telemetry event (live feed).

        Returns an idempotent unsubscribe function. The store remains the
        authority for replay/history — this is the live seam only.
        """
        return subscribe_into(self._telemetry_listeners, listener)

    def subscribe_raw(self, listener: RawLineListener) -> Callable[[], None]:
        """Register for each raw stdout line (live ``log`` fan-out, D7
        raw-log-primary: malformed lines included, terminator-stripped
        verbatim bytes).

        Returns an idempotent unsubscribe function. The store's raw-line
        ring remains the authority for raw retention/history — this is the
        live seam only.
        """
        return subscribe_into(self._raw_listeners, listener)

    def transitions(self) -> list[StateTransition]:
        """Bounded transition history (all sessions, oldest first)."""
        return list(self._transitions)

    def session_transitions(self, session: int) -> list[StateTransition]:
        """One episode's timestamped transitions (§4 rule 4)."""
        return [t for t in self._transitions if t.session == session]

    async def run(self) -> None:
        """The always-on loop (R1): scan → connect → install agent → serve
        telemetry, forever. Every failure path lands in OFFLINE and the
        backoff-bounded rescan continues — this loop never gives up, and it
        is the sanctioned exemption to AGENTS.md rule 5. Returns only on
        cancellation; the ``finally`` releases the BLE central role (F6).
        """
        try:
            while True:
                await self._sleep(self._backoff.next_delay())
                try:
                    await self._attempt_connection()
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    self._set_state(HubState.OFFLINE, "hub not found (scan timeout)")
                except ConnectionError as exc:
                    self._set_state(HubState.OFFLINE, f"connect failed: {exc}")
                except Exception:
                    # Never-give-up boundary (R1): an unexpected error kills
                    # neither the loop nor the service — log, go OFFLINE,
                    # rescan on the same backoff.
                    logger.exception("connection attempt failed unexpectedly")
                    self._set_state(
                        HubState.OFFLINE, "unexpected error (see service log)"
                    )
        finally:
            # Teardown always releases the single BLE central role (F6) —
            # also when cancelled mid-setup (ADVERTISING), not just when
            # parked in a live session. From OFFLINE this is a no-op.
            await self._end_session("service stopped")

    # ------------------------------------------------------------------
    # Connection cycle
    # ------------------------------------------------------------------

    async def _attempt_connection(self) -> None:
        """One discover→connect→install cycle; parks until the connection
        drops, then lands in OFFLINE (the loop's next backoff tick follows).

        Raises ``asyncio.TimeoutError`` (scan window missed the hub) or
        ``ConnectionError`` (connect refused/failed — e.g. another central
        holds the hub, or session setup failed); the caller maps both to
        OFFLINE + backoff.
        """
        discovered = await self._transport.discover(
            self._config.hub_name, timeout=self._config.scan_timeout
        )
        self._session += 1
        self._set_state(HubState.ADVERTISING, f"hub discovered: {discovered.name}")
        conn = _Connection(
            parser=TelemetryParser(clock=self._clock, on_malformed=self._log_malformed),
            disconnect=asyncio.Event(),
            last_status_at=time.monotonic(),
        )
        self._expected_running = False
        self._program_running = False
        self._settling = True
        try:
            await asyncio.wait_for(
                self._transport.connect(
                    discovered, on_disconnect=self._disconnect_hook(conn)
                ),
                timeout=self._config.connect_timeout,
            )
        except TimeoutError as exc:
            # A mid-handshake stall can leave the BLE central role held under
            # the cancelled connect — release it (idempotent, best-effort per
            # the seam contract) before OFFLINE, or every later attempt fights
            # our own stale role (F6: never hold the role while offline).
            await self._end_session(
                f"connect timed out after {self._config.connect_timeout}s"
            )
            raise ConnectionError(
                f"connect timed out after {self._config.connect_timeout}s"
            ) from exc
        self._conn = conn
        self._store.mark_connection_start()
        try:
            await self._transport.subscribe_stdout(self._on_stdout)
            await self._transport.subscribe_status(self._on_status)
            await self._install_agent()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._end_session(f"session setup failed: {exc}")
            raise ConnectionError(f"session setup failed: {exc}") from exc
        self._settling = False
        self._set_state(HubState.AGENT, "agent installed and started")
        self._backoff.reset()
        reason = await self._park_until_drop(conn)
        await self._end_session(reason)

    async def _park_until_drop(self, conn: _Connection) -> str:
        """Park in a live session until the connection drops; the session's
        end reason (``hub disconnected`` or ``EXTERNAL_TAKEOVER_REASON``).

        A bare drop wait is not enough: a second-central takeover kills the
        link with NO disconnect callback (BlueZ removes the hub's objects
        without a ``Connected: false`` property change — #24 hardware
        evidence, 2026-09-29), so the loop would park forever. Liveness is
        therefore read from the hub's own status reports (2 Hz push,
        timestamped in ``conn.last_status_at`` by ``_on_status``): silence
        past ``liveness_timeout`` opens the grace window, adjudicated by
        :meth:`_adjudicate_silence`.

        Waits, stamps, and the grace deadline all use real ``asyncio``
        timing (``time.monotonic``), not the injected clock — the injected
        sleep/clock exist for backoff-schedule tests, which pin them
        (e.g. ``sleeps == [0.0, 0.0]``); liveness parking is a real-clock
        concern and must never read a frozen fake clock as its deadline.
        """
        while not conn.disconnect.is_set():
            try:
                await asyncio.wait_for(
                    conn.disconnect.wait(), timeout=self._config.liveness_timeout
                )
            except TimeoutError:
                verdict = await self._adjudicate_silence(
                    conn, silence_started_at=time.monotonic()
                )
                if verdict is not None:
                    return verdict
                if self._conn is not conn:
                    return "hub disconnected"
        return "hub disconnected"

    async def _adjudicate_silence(
        self, conn: _Connection, *, silence_started_at: float
    ) -> str | None:
        """Status reports went silent past ``liveness_timeout``; adjudicate
        by what happens within ``liveness_grace`` of ``silence_started_at``.

        Disconnect callback arriving → ``None`` (the park loop falls through
        to the ordinary ``hub disconnected`` — power-off shape: farewell
        ``BLE_HOST_CONNECTED=False`` report, then the callback). Fresh
        reports resuming (``last_status_at`` past the silence start) →
        ``None`` (transient silence; the park loop re-arms). Neither → the
        takeover signature (kick shape: notifications stop mid-``True``
        AND the callback never fires — #24 evidence) → takeover reason.

        Session-scoped by identity like ``_disconnect_hook``: a session
        ended by another path while this coroutine sleeps (e.g. an agent
        reinstall forcing a reconnect) voids the verdict — a stale
        adjudication must never label a newer session's link.
        """
        deadline = silence_started_at + self._config.liveness_grace
        while True:
            if conn.disconnect.is_set() or self._conn is not conn:
                return None
            if conn.last_status_at >= silence_started_at:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 0.5))
        logger.warning("status reports silent with no disconnect callback; takeover")
        return EXTERNAL_TAKEOVER_REASON

    async def _end_session(self, reason: str) -> None:
        """Drop the session and land in OFFLINE (§4: every disconnect →
        OFFLINE). Cancels any in-flight agent reinstall, drops the parser
        (a buffered partial line is counted via its ``reset()``, D7), records
        the transition, then best-effort disconnects the transport
        (idempotent per the seam contract) so the single BLE central role is
        always released (F6).
        """
        if self._reinstall_task is not None:
            task, self._reinstall_task = self._reinstall_task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.parser.reset()
        self._program_running = False
        self._expected_running = False
        self._settling = False
        self._set_state(HubState.OFFLINE, reason)
        try:
            await self._transport.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "transport disconnect during session end failed", exc_info=True
            )

    def _disconnect_hook(self, conn: _Connection) -> DisconnectListener:
        """The ``on_disconnect`` callback for one connection: latch the
        session's event so the parked loop wakes. Stale/duplicate fires (a
        buggy adapter) are ignored via identity — they must never tear down
        a newer session."""

        def on_disconnect() -> None:
            if self._conn is not conn:
                logger.warning("stale on_disconnect notification ignored")
                return
            conn.disconnect.set()

        return on_disconnect

    # ------------------------------------------------------------------
    # Agent install / reinstall
    # ------------------------------------------------------------------

    async def _install_agent(self) -> None:
        """Install and start the agent wrapper — stop-before-install: the hub
        rejects program writes with ``CommandError.BUSY`` while a user
        program runs (D6), so ``stop()`` is always the safe first move
        (api-notes row 5).

        The expected-start latch is armed only after ``stop()`` resolves and
        immediately before ``install_and_start`` (an await-free window).
        Arming before the stop would swallow any program that grabs the hub
        while the stop write is in flight (status notifications can overtake
        it); arming only after install resolves would misread the agent's own
        start SET, which the hub can report while the install RPC is still
        in flight. A foreign grab inside the tiny armed window still
        self-heals: the hub rejects our write with BUSY, the reinstall
        fails, and the manager forces a full reconnect.
        """
        await self._transport.stop()
        self._expected_running = True
        await self._transport.install_and_start(self._config.agent_program)

    def _schedule_agent_reinstall(self) -> None:
        """Reinstall the agent after a program ends (§4 rule 2) — called
        from the sync status callback, so the work runs on the loop as a
        task. The reference is held and joined/cancelled at session end —
        never fire-and-forget. One reinstall in flight per session."""
        if self._reinstall_task is not None and not self._reinstall_task.done():
            return
        self._reinstall_task = asyncio.create_task(self._reinstall_agent())

    async def _reinstall_agent(self) -> None:
        try:
            await self._install_agent()
            logger.info("agent reinstalled after user program ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            # The session is unusable (agent can't be restored in place) —
            # force a full OFFLINE→reconnect cycle, the stronger recovery.
            logger.exception("agent reinstall failed; forcing reconnect")
            conn = self._conn
            if conn is not None:
                conn.disconnect.set()

    # ------------------------------------------------------------------
    # Hub pipes
    # ------------------------------------------------------------------

    def _on_stdout(self, data: bytes) -> None:
        """Raw-log-primary fan-out (D7 — the routing order lives in
        :class:`~brick_console.fanout.TelemetryRouter`): one split, raw line
        first, telemetry parsing attached on top — nothing the hub prints
        is ever dropped. The per-line DEBUG log stays here: a manager
        observability concern, not a pipeline one."""
        conn = self._conn
        if conn is None:
            logger.warning("stdout chunk with no connection; dropped: %r", data[:64])
            return
        pairs = conn.parser.feed_with_raw(data)
        self._router.route(pairs)
        for raw, _ in pairs:
            logger.debug("hub stdout: %r", raw)

    def _on_status(self, flags: StatusFlags) -> None:
        """Derive program-lifecycle edges from status snapshots (§4 rule 2)
        and stamp the liveness heartbeat (D8: reports at 2 Hz ARE the
        liveness signal — silence past ``liveness_timeout`` is what the
        park watchdog adjudicates).

        During setup (``_settling``) snapshots only update the running flag —
        e.g. a stale program still running at connect is stopped by the
        setup sequence itself. ``_expected_running`` distinguishes our own
        agent's start from an unexpected program.
        """
        conn = self._conn
        if conn is None:
            logger.debug("status report with no connection; ignored")
            return
        conn.last_status_at = time.monotonic()
        running = bool(flags & StatusFlags.USER_PROGRAM_RUNNING)
        if running == self._program_running:
            return  # snapshot, not an edge
        self._program_running = running
        if running:
            if self._expected_running:
                self._expected_running = False
                return  # our agent's own start — the agent, not a user program
            if self._settling:
                return
            self._set_state(HubState.PROGRAM, "user program started")
            return
        # CLEAR edge: whatever we armed the latch for did not start — the
        # hub was stopped or running something else. Disarm before anything
        # else so a stale latch can never swallow the next program report.
        self._expected_running = False
        if self._settling:
            return
        if self._state is HubState.PROGRAM:
            self._set_state(HubState.AGENT, "user program ended")
        self._schedule_agent_reinstall()

    def _log_malformed(self, raw: bytes, reason: str) -> None:
        """``on_malformed`` hook: malformed lines are expected data (D7) —
        logged with the raw bytes, never raised."""
        logger.info("malformed telemetry line (%s): %r", reason, raw[:120])

    # ------------------------------------------------------------------
    # State machine core
    # ------------------------------------------------------------------

    def _set_state(self, to: HubState, reason: str) -> None:
        """Record, log, and fan out a state change (§4 rule 4). A same-state
        call (e.g. OFFLINE→OFFLINE between scan misses) is not a transition
        and is not recorded — the rescan loop is what the OFFLINE state
        *means*."""
        if to == self._state:
            self._state_reason = f"still {to.value}: {reason}"
            logger.debug("state unchanged %s (%s)", to.value, reason)
            return
        from_state = self._state
        self._state = to
        self._state_reason = reason
        timestamp = self._clock()
        self._transitions.append(
            StateTransition(from_state, to, timestamp, reason, self._session)
        )
        logger.info("state %s -> %s (%s)", from_state.value, to.value, reason)
        fan_out(self._state_listeners, to, reason, timestamp)
