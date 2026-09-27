"""JSON-lines telemetry parser: raw stdout bytes in, typed events out (D7).

The hub-side ``brick_telemetry`` agent prints telemetry lines over hub
stdout; those arrive at the server as *push* notifications of arbitrary byte
chunks (``transport.Transport``'s ``StdoutListener``). A line can split across
notification boundaries, even between the ``\r`` and the ``\n`` of its CRLF
terminator, so framing is server-side (D7). This module owns two things:

- :class:`LineSplitter` — the raw-log-primary seam: it buffers byte chunks
  and yields complete *raw* lines (CRLF primary, a lone LF tolerated, a
  stray trailing ``\r`` stripped, empty lines skipped silently). It never
  drops a raw line; the raw-line log path (the BLE manager's pipeline and the
  telemetry store's raw-line ring, D7) consumes its output first.
- :class:`TelemetryParser` — attaches on top of the splitter and turns raw
  lines into typed :mod:`brick_console.events`. It is a class, not a
  generator: ``feed(bytes) -> list[TelemetryEvent]``, plus ``reset()`` and
  a ``malformed_count`` property (and an optional ``on_malformed`` callback
  so the server can log). One instance per connection; the BLE manager
  (architecture §4.5) constructs a fresh one per connect.

Robustness contract (D7): the parser never raises on bad input. Malformed
lines — invalid UTF-8/JSON, a non-object, a missing kind key, a known kind
with bad fields — are counted and skipped, and the stream survives; a
subsequent line still parses. A partial line is buffered until its CRLF; the
buffer is hard-capped at 4,096 bytes (overflow discards until the next line
terminator, counts one malformed, and resyncs). A partial still in the buffer
at ``reset()`` (reconnect) is dropped and counted, never emitted half.

Timestamps (D7): no wire timestamp — the hub has no wall clock. Every event
gets ``received_at`` (float UNIX seconds) stamped at successful decode, via
an injectable clock (default ``time.time``) so tests are deterministic.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from brick_console.events import EventDecodeError, TelemetryEvent, decode

__all__ = ["LineSplitter", "TelemetryParser"]

# Partial-line buffer hard cap (D7). Typical lines are <= 200 B; the cap
# bounds memory against a runaway unterminated line, not a legitimate one.
_MAX_PARTIAL_BYTES = 4096


class LineSplitter:
    """Splits a byte stream into complete raw lines (D7 framing).

    Buffers incoming chunks and yields each line as soon as its terminator
    arrives. CRLF is the primary terminator (MicroPython's ``print()`` line
    ending); a lone LF is tolerated; a stray trailing ``\r`` is stripped.
    Empty lines are skipped silently — they are framing noise, not content.

    The partial buffer is capped at :data:`_MAX_PARTIAL_BYTES`: on overflow
    the buffer is discarded and the splitter resyncs at the next line
    terminator, reporting the loss through ``on_loss`` (so the parser can
    count it as malformed). Nothing here is a gate: every raw line it yields
    is handed to the raw-log path (the primary consumer) before telemetry
    parsing attaches.
    """

    def __init__(self, *, on_loss: Callable[[str], None] | None = None) -> None:
        self._buffer = bytearray()
        self._discarding = False  # True between an overflow and the next CRLF
        self._on_loss = on_loss

    def feed(self, chunk: bytes) -> list[bytes]:
        """Feed one stdout chunk; return every complete raw line it produces.

        A returned line has no terminator. ``b"\\r\\n"`` inside a chunk is one
        terminator; a ``b"\\r"`` at a chunk boundary is held until the next
        byte decides whether it is a CRLF terminator or stray content.
        """
        self._buffer += chunk
        lines: list[bytes] = []
        while True:
            nl = self._buffer.find(b"\n")
            if nl == -1:
                break
            raw = bytes(self._buffer[:nl])
            del self._buffer[: nl + 1]
            if self._discarding:
                # This terminator ends the discarded over-cap span; resync.
                self._discarding = False
                continue
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            lines.append(raw)
        if not self._discarding and len(self._buffer) > _MAX_PARTIAL_BYTES:
            # Over-cap: discard the buffered partial and resync at the next
            # terminator. One loss event per overflow, not per byte.
            self._buffer.clear()
            self._discarding = True
            if self._on_loss is not None:
                self._on_loss(
                    "partial line exceeded 4096 bytes; discarded until next CRLF"
                )
        return lines

    def drop_partial(self) -> bytes | None:
        """Drop any buffered partial and return it (``None`` if none).

        Called on reconnect so a half-line is never emitted; the caller counts
        it as malformed and may log it.
        """
        if self._buffer:
            raw = bytes(self._buffer)
            self._buffer.clear()
            self._discarding = False
            return raw
        self._discarding = False
        return None


MalformedListener = Callable[[bytes, str], None]
"""Notified with (raw line, reason) for each malformed line so the server can
log it. The raw line is the bytes as split (already terminator-stripped)."""


class TelemetryParser:
    """Parses raw stdout bytes into typed telemetry events (D7).

    Wraps a :class:`LineSplitter`; ``feed`` decodes each raw line it yields,
    stamps ``received_at`` at successful decode, and returns the events in
    order. Malformed lines are counted (``malformed_count``) and skipped —
    never raised. Unknown kinds become :class:`~brick_console.events.UnknownEvent`
    (via the events module), unknown fields are ignored there.

    ``clock`` is injectable (default ``time.time``) so receipt times are
    deterministic in tests; ``on_malformed`` is an optional callback the
    server wires to its logger. One instance per BLE connection.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        on_malformed: MalformedListener | None = None,
    ) -> None:
        self._clock = clock
        self._on_malformed = on_malformed
        self._malformed = 0
        self._splitter = LineSplitter(on_loss=self._note_loss)

    def feed(self, data: bytes) -> list[TelemetryEvent]:
        """Feed a stdout chunk; return the telemetry events decoded from it.

        Never raises: any bad line is counted as malformed and skipped, and
        subsequent lines in the same chunk still parse.
        """
        return [event for _, event in self.feed_with_raw(data) if event is not None]

    def feed_with_raw(self, data: bytes) -> list[tuple[bytes, TelemetryEvent | None]]:
        """Feed a chunk; return ``(raw line, decoded event or None)`` per line.

        The raw-log-primary seam (D7 fan-out, architecture §4.5): the BLE
        manager routes each raw line to the raw-line log path (primary) and
        to the telemetry pipeline off one split — no double-framing.
        Malformed lines appear with ``None`` as their event, counted here
        exactly like chunk-level :meth:`feed`; empty/whitespace-only lines
        are skipped silently and never appear. The raw line is yielded
        verbatim (terminator-stripped) whether it parsed or not.
        """
        pairs: list[tuple[bytes, TelemetryEvent | None]] = []
        for raw in self._splitter.feed(data):
            if not raw.strip():
                continue  # empty / whitespace-only line: skipped silently, uncounted
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._note_malformed(raw, "invalid UTF-8")
                pairs.append((raw, None))
                continue
            try:
                event = decode(line)
            except EventDecodeError as exc:
                self._note_malformed(raw, str(exc))
                pairs.append((raw, None))
                continue
            event = _stamp(event, self._clock())
            pairs.append((raw, event))
        return pairs

    def reset(self) -> None:
        """Drop any buffered partial line, counting it as malformed.

        Called on reconnect so a half-line is never emitted; a fresh parser is
        constructed per connect (the BLE manager owns that lifecycle,
        architecture §4.5), so this is belt-and-braces, not a
        replacement for instance lifecycle.
        """
        raw = self._splitter.drop_partial()
        if raw is not None:
            self._note_malformed(raw, "partial line dropped on reset (reconnect)")

    @property
    def malformed_count(self) -> int:
        """Number of malformed lines skipped so far (cumulative)."""
        return self._malformed

    def _note_malformed(self, raw: bytes, reason: str) -> None:
        self._malformed += 1
        if self._on_malformed is not None:
            self._on_malformed(raw, reason)

    def _note_loss(self, reason: str) -> None:
        self._malformed += 1
        if self._on_malformed is not None:
            self._on_malformed(b"", reason)


def _stamp(event: TelemetryEvent, received_at: float) -> TelemetryEvent:
    """Stamp ``received_at`` onto a decoded event (frozen dataclass → replace).

    ``received_at`` is excluded from event equality (``compare=False``), so
    stamping is metadata side-effect-free: the event's wire content is intact.
    """
    return replace(event, received_at=received_at)
