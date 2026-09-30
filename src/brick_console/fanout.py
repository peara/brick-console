"""Fan-out plumbing: the shared listener discipline for the server package.

One home for the three identical patterns that previously lived as
per-module copies in the manager, the WS gateway, and the adapter
(consolidation issue; D6/D7 still govern):

- :func:`guarded_call` — invoke one fan-out listener; a raising consumer
  is logged (under this module's logger) and never kills the feed.
- :func:`fan_out` — snapshot the registry and guarded-call every listener
  in order.
- :func:`subscribe_into` — the append-always registry pattern with an
  idempotent unsubscribe (list remove, ``ValueError`` suppressed).
- :class:`TelemetryRouter` — the D7 raw-log-primary pipeline that routes
  one parser split to the store and the live listener fan-outs; both the
  BLE manager's stdout pipe and the WS gateway's mock source delegate to
  it, so the routing order exists exactly once.

The adapters' equality-dedupe subscription (``if listener not in …``) is
deliberately NOT here: it is a different pattern serving a different
invariant (re-subscribe with the same bound methods stays one entry) and
stays in the adapter. Only the guarded call and the fan-out loops are
shared with it.

Import direction: this module imports neither ``ble_manager`` nor ``ws``
(no cycles); it sits below both. :class:`TelemetrySink` lives here (the
router is its consumer) and is re-exported by ``ble_manager`` so existing
imports keep working unchanged.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable
from typing import Protocol

from brick_console.events import HubInfo, TelemetryEvent

__all__ = [
    "TelemetryRouter",
    "TelemetrySink",
    "fan_out",
    "guarded_call",
    "subscribe_into",
]

logger = logging.getLogger(__name__)


class TelemetrySink(Protocol):
    """The store seam the router feeds — structurally satisfied by the
    telemetry store (ring buffer + raw-line ring + ``hub_info`` cache)."""

    def append_event(self, event: TelemetryEvent) -> int: ...
    def append_raw_line(self, line: bytes) -> None: ...
    def set_hub_info(self, hub_info: HubInfo) -> None: ...
    def mark_connection_start(self) -> None: ...


def guarded_call(fn: Callable[..., None], /, *args: object) -> None:
    """Invoke one fan-out listener; a broken consumer must never kill the
    feed — log (this module's logger) and continue."""
    try:
        fn(*args)
    except Exception:
        logger.exception("event listener failed; continuing")


def fan_out(listeners: Iterable[Callable[..., None]], /, *args: object) -> None:
    """Snapshot the registry and guarded-call every listener in order.

    The snapshot (``tuple``) isolates iteration from concurrent subscribe/
    unsubscribe — a listener removed mid-flight is still delivered at most
    once; a raising listener never reaches the others (``guarded_call``)."""
    for listener in tuple(listeners):
        guarded_call(listener, *args)


def subscribe_into[Listener](
    listeners: list[Listener], listener: Listener
) -> Callable[[], None]:
    """The append-always subscription pattern: idempotent unsubscribe via
    list remove (``ValueError`` suppressed)."""
    listeners.append(listener)

    def unsubscribe() -> None:
        with contextlib.suppress(ValueError):
            listeners.remove(listener)

    return unsubscribe


class TelemetryRouter:
    """The D7 raw-log-primary pipeline (routing order lives here and only
    here): for each ``(raw, event)`` pair from one ``feed_with_raw`` split —

    1. raw line → sink ``append_raw_line`` + raw fan-out (malformed lines
       included: raw retention is primary, D7 — nothing the hub prints is
       ever dropped);
    2. then, for a parsed event → sink ``append_event``, ``set_hub_info``
       on :class:`~brick_console.events.HubInfo`, telemetry fan-out.

    Constructed over a telemetry sink and the caller's two *live* listener
    registries: it shares the caller's list objects, so subscriptions stay
    owned by the manager/mock and keep working after construction.
    Malformed lines (``event is None``) stop after step 1 — counted and
    logged by the parser, never raised here.
    """

    def __init__(
        self,
        sink: TelemetrySink,
        *,
        raw_listeners: list[Callable[[bytes], None]],
        telemetry_listeners: list[Callable[[TelemetryEvent], None]],
    ) -> None:
        self._sink = sink
        self._raw_listeners = raw_listeners
        self._telemetry_listeners = telemetry_listeners

    def route(self, pairs: Iterable[tuple[bytes, TelemetryEvent | None]]) -> None:
        """Route the parser's ``(raw, event)`` pairs (D7 order — raw first)."""
        for raw, event in pairs:
            self._sink.append_raw_line(raw)
            fan_out(self._raw_listeners, raw)
            if event is None:
                continue  # malformed: counted and logged by the parser
            self._sink.append_event(event)
            if isinstance(event, HubInfo):
                self._sink.set_hub_info(event)
            fan_out(self._telemetry_listeners, event)
