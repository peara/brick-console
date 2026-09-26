"""Telemetry ring buffer + hub_info snapshot cache (issue #9, decision D7).

D7 policies this store implements:
- ``received_at`` is stamped by the parser upstream — the store never rewrites it (D7: "Timestamps and ordering").
- Replay ordering is per-connection, the parser instance is the reset boundary (D7: "the parser instance is per-connection and resets on reconnect").
- ``hub_info`` is a snapshot event cached outside the event ring, latest-wins (D7: "the server caches the latest one outside the ring buffer").
- Separate retention bounds for parsed events vs raw lines (D7: "Raw-log-primary fan-out" — "the telemetry store's raw-line ring").
- The ring owns replay indices; the append counter is never reset (D7: "the telemetry store (ring buffer) owns replay indices").

Architecture reference: architecture.md §2.1 — "Ring buffer (bounded deque) of parsed events; replay to late-joining clients".

Pure stdlib, synchronous — no async, no BLE.  The BLE manager writes; the WS
gateway reads.  ``received_at`` is receipt metadata (``compare=False`` on
every event dataclass); equality comparisons between an event sitting in the
ring and a later copy of the same wire content will match regardless of when
each was stamped.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brick_console.events import HubInfo, TelemetryEvent

__all__ = ["TelemetryStore"]

_DEFAULT_EVENT_CAPACITY = 5000
_DEFAULT_RAW_LINE_CAPACITY = 5000


class TelemetryStore:
    """Bounded in-memory store of parsed telemetry events and raw stdout lines.

    Two independent rings:

    - *Event ring* — bounded ``collections.deque`` of parsed
      ``TelemetryEvent``\\s.  Append assigns an ordinal (monotonic counter,
      never reset) which becomes the event's only replay key.  D7: the ring
      owns replay indices; the parser stamping ``received_at`` is the
      arrival-time authority.
    - *Raw-line ring* — bounded deque of raw stdout lines (bytes) retained
      verbatim per D7's raw-log-primary rule, independently bounded.

    Plus a ``hub_info`` snapshot cache outside both rings — latest-wins,
    survives ring eviction.

    Connection marker: ``mark_connection_start()`` records the ordinal of the
    next event that will be appended.  ``replay_events(count)`` defaults to the
    *current* connection's slice; pre-connection history is still reachable via
    ``replay_events_from(ordinal)`` with absolute ordinals.

    All mutations are synchronous, thread-unsafe by design — the BLE manager
    writes on the asyncio event loop; the WS gateway reads on the same
    thread via ``asyncio.to_thread`` or direct in-loop access.
    """

    __slots__ = (
        "_capacity",
        "_conn_start_ordinal",
        "_counter",
        "_events",
        "_hub_info",
        "_raw_capacity",
        "_raw_lines",
    )

    def __init__(
        self,
        *,
        event_capacity: int = _DEFAULT_EVENT_CAPACITY,
        raw_line_capacity: int = _DEFAULT_RAW_LINE_CAPACITY,
    ) -> None:
        if event_capacity < 1:
            raise ValueError(f"event_capacity must be >= 1, got {event_capacity}")
        if raw_line_capacity < 1:
            raise ValueError(f"raw_line_capacity must be >= 1, got {raw_line_capacity}")
        self._events: deque[tuple[int, TelemetryEvent]] = deque()
        self._capacity = event_capacity
        self._counter = 0
        self._hub_info: HubInfo | None = None
        self._raw_lines: deque[bytes] = deque()
        self._raw_capacity = raw_line_capacity
        self._conn_start_ordinal: int | None = None

    # ------------------------------------------------------------------
    # Append — the write side (BLE manager)
    # ------------------------------------------------------------------

    def append_event(self, event: TelemetryEvent) -> int:
        """Push one parsed telemetry event into the event ring.

        Returns the ordinal assigned to this event.

        If the ring is at capacity the oldest event is dropped.  The ordinal
        counter (return value) is never reset across appends or reconnects:
        absolute ordinals are always addressable via ``replay_events_from``,
        even after older entries have been evicted.
        """
        self._counter += 1
        ordinal = self._counter
        self._events.append((ordinal, event))
        if len(self._events) > self._capacity:
            self._events.popleft()
        return ordinal

    def set_hub_info(self, hub_info: HubInfo) -> None:
        """Cache the latest ``hub_info`` snapshot (latest-wins, replaceable)."""
        self._hub_info = hub_info

    def append_raw_line(self, line: bytes) -> None:
        """Push a raw stdout line (verbatim bytes) into the raw-line ring."""
        self._raw_lines.append(line)
        if len(self._raw_lines) > self._raw_capacity:
            self._raw_lines.popleft()

    def mark_connection_start(self) -> None:
        """Record that the *next* append starts a new connection slice.

        Called by the BLE manager on each fresh connect.  Replay via
        ``replay_events(count)`` defaults to the current connection slice;
        pre-connection events remain addressable with ``replay_events_from``.
        """
        self._conn_start_ordinal = self._counter + 1

    # ------------------------------------------------------------------
    # Read / replay — the read side (WS gateway)
    # ------------------------------------------------------------------

    @property
    def event_capacity(self) -> int:
        """Configured maximum number of events in the ring."""
        return self._capacity

    @property
    def event_count(self) -> int:
        """Number of events currently in the ring."""
        return len(self._events)

    @property
    def hub_info(self) -> HubInfo | None:
        """The latest ``hub_info`` snapshot, or ``None`` if none received yet."""
        return self._hub_info

    @property
    def raw_line_capacity(self) -> int:
        """Configured maximum number of raw lines in the ring."""
        return self._raw_capacity

    @property
    def raw_line_count(self) -> int:
        """Number of raw lines currently in the ring."""
        return len(self._raw_lines)

    @property
    def connection_start_ordinal(self) -> int | None:
        """The ordinal of the first event of the current connection, or ``None``."""
        return self._conn_start_ordinal

    @property
    def next_ordinal(self) -> int:
        """The ordinal that will be assigned to the *next* appended event."""
        return self._counter + 1

    def replay_events(
        self, count: int, *, from_ordinal: int | None = None
    ) -> list[tuple[int, TelemetryEvent]]:
        """Return up to ``count`` events in append order.

        When ``from_ordinal`` is ``None`` (the default), the *current*
        connection slice is used as the start: events from
        ``connection_start_ordinal`` up to the most recent append, capped at
        ``count`` (the newest ``count`` within this connection).  If no
        connection marker has been set yet, returns the newest ``count`` events
        from the whole ring.

        When ``from_ordinal`` is given, replay starts at that absolute ordinal
        — any event whose ordinal >= ``from_ordinal`` that is still in the
        ring, capped at ``count``.

        Guarantees:
        - Replay order == append order == arrival order within a connection.
        - Count cap is always respected.
        - Eviction drops oldest; the newest append is always available.
        """
        if count <= 0:
            return []

        start = from_ordinal if from_ordinal is not None else self._conn_start_ordinal

        if start is None:
            # No connection marker: newest *count* events from the ring.
            items = list(self._events)
            return items[-count:] if count < len(items) else items

        # Binary-search the first event whose ordinal >= start.
        events = self._events
        lo, hi = 0, len(events)
        while lo < hi:
            mid = (lo + hi) // 2
            if events[mid][0] < start:
                lo = mid + 1
            else:
                hi = mid
        idx = lo
        if idx >= len(events):
            return []
        result: list[tuple[int, TelemetryEvent]] = []
        for i in range(idx, len(events)):
            if len(result) >= count:
                break
            result.append(events[i])
        return result

    def replay_events_from(
        self, from_ordinal: int, *, count: int | None = None
    ) -> list[tuple[int, TelemetryEvent]]:
        """Return events starting at *absolute* ``from_ordinal``, in append order.

        If ``count`` is ``None`` all available events from that ordinal onward
        are returned; otherwise at most ``count`` are returned.

        This is the absolute-ordinal entry point — used when the caller wants
        pre-connection history or a specific range regardless of the current
        connection marker.
        """
        limit = count if count is not None else len(self._events)
        return self.replay_events(limit, from_ordinal=from_ordinal)

    def replay_raw_lines(self, count: int) -> list[bytes]:
        """Return up to ``count`` most recent raw stdout lines, in order."""
        if count <= 0:
            return []
        return (
            list(self._raw_lines)[-count:]
            if count < len(self._raw_lines)
            else list(self._raw_lines)
        )
