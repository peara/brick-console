"""Parser tests (issue #3): raw bytes in, typed events out, never raising.

Covers the framing + streaming contract: chunked feeds (mid-line, mid-CRLF,
multi-line, malformed-then-good), the never-raises guarantee, the 4096-byte
partial cap, reset() dropping + counting a partial, and receipt-time
stamping via an injectable clock.
"""

from __future__ import annotations

from brick_console.events import Battery, HubInfo, UnknownEvent
from brick_console.parsing import LineSplitter, TelemetryParser

_CAP = 4096


def _parser(clock=None, on_malformed=None) -> TelemetryParser:
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    if on_malformed is not None:
        kwargs["on_malformed"] = on_malformed
    return TelemetryParser(**kwargs)


HUB = b'{"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}'
BAT = b'{"t":"battery","v":8085,"c":42,"pct":87}'
IMU = b'{"t":"imu","ax":120,"ay":-980,"az":9810,"gx":0,"gy":0,"gz":3,"up":"top"}'


def test_single_line() -> None:
    p = _parser(clock=lambda: 1.0)
    events = p.feed(HUB + b"\r\n")
    assert isinstance(events[0], HubInfo)
    assert events[0].received_at == 1.0
    assert p.malformed_count == 0


def test_multi_line_single_chunk() -> None:
    p = _parser()
    chunk = HUB + b"\r\n" + BAT + b"\r\n" + IMU + b"\r\n"
    events = p.feed(chunk)
    assert [type(e).__name__ for e in events] == ["HubInfo", "Battery", "Imu"]
    assert p.malformed_count == 0


def test_split_mid_line() -> None:
    p = _parser()
    line = HUB + b"\r\n"
    events = []
    for byte in line:  # feed one byte at a time: every split point is exercised
        events += p.feed(bytes([byte]))
    assert len(events) == 1
    assert isinstance(events[0], HubInfo)
    assert p.malformed_count == 0


def test_split_mid_crlf() -> None:
    p = _parser()
    first = p.feed(HUB + b"\r")
    assert first == []  # the \r is held — could be CRLF or stray content
    second = p.feed(b"\n" + BAT + b"\r\n")
    assert len(second) == 2
    assert isinstance(second[0], HubInfo)
    assert isinstance(second[1], Battery)


def test_lone_lf_tolerated() -> None:
    p = _parser()
    events = p.feed(HUB + b"\n")
    assert len(events) == 1
    assert isinstance(events[0], HubInfo)


def test_stray_cr_stripped() -> None:
    p = _parser()
    events = p.feed(HUB + b"\r\r\n")  # stray \r before the CRLF
    assert len(events) == 1
    assert isinstance(events[0], HubInfo)


def test_empty_lines_skipped_silently() -> None:
    p = _parser()
    events = p.feed(b"\r\n\r\n   \r\n" + HUB + b"\r\n")
    assert len(events) == 1
    assert p.malformed_count == 0  # empty lines are not malformed


def test_malformed_then_good_survives() -> None:
    p = _parser()
    events = p.feed(b"Traceback (most recent call last):\r\n" + BAT + b"\r\n")
    assert len(events) == 1
    assert isinstance(events[0], Battery)
    assert p.malformed_count == 1  # the traceback line is counted, not fatal


def test_never_raises_on_garbage() -> None:
    p = _parser()
    garbage = [
        b"\xff\xfe\x00\x01\r\n",  # invalid UTF-8
        b"{not json}\r\n",
        b"[1,2,3]\r\n",
        b'"just a string"\r\n',
        b'{"t":42}\r\n',
        b'{"t":"battery","v":"high","c":42,"pct":87}\r\n',
    ]
    for chunk in garbage:
        p.feed(chunk)  # must not raise
    assert p.malformed_count == len(garbage)
    # And the stream still parses afterward.
    assert isinstance(p.feed(BAT + b"\r\n")[0], Battery)


def test_unknown_kind_via_parser() -> None:
    p = _parser()
    events = p.feed(b'{"t":"future","x":1}\r\n')
    assert len(events) == 1
    assert isinstance(events[0], UnknownEvent)
    assert events[0].data == {"t": "future", "x": 1}


def test_partial_buffer_cap_overflow() -> None:
    p = _parser()
    # 4096 bytes is the cap; a partial line of 4096 bytes is held, one more
    # byte overflows: discard + one malformed + resync.
    p.feed(b"x" * _CAP)
    assert p.malformed_count == 0  # exactly at cap: still held
    p.feed(b"y")  # exceeds cap with no terminator in sight
    assert p.malformed_count == 1
    # Resync: the next terminator ends the discarded span; a good line parses.
    events = p.feed(b"\r\n" + BAT + b"\r\n")
    assert isinstance(events[0], Battery)
    assert p.malformed_count == 1  # no extra count for resync


def test_reset_drops_and_counts_partial() -> None:
    p = _parser()
    p.feed(b'{"t":"battery","v":')  # partial, no terminator
    assert p.malformed_count == 0
    p.reset()
    assert p.malformed_count == 1  # dropped partial counted, never emitted half
    # A fresh line still parses after reset.
    assert isinstance(p.feed(BAT + b"\r\n")[0], Battery)


def test_fake_clock_stamps_received_at() -> None:
    stamps = iter([100.0, 200.0, 300.0])
    p = _parser(clock=lambda: next(stamps))
    events = p.feed(HUB + b"\r\n" + BAT + b"\r\n" + IMU + b"\r\n")
    assert [e.received_at for e in events] == [100.0, 200.0, 300.0]


def test_default_clock_is_time_time() -> None:
    p = TelemetryParser()
    events = p.feed(BAT + b"\r\n")
    assert isinstance(events[0].received_at, float)


def test_on_malformed_callback() -> None:
    seen: list[tuple[bytes, str]] = []
    p = _parser(on_malformed=lambda raw, reason: seen.append((raw, reason)))
    p.feed(b"not json\r\n")
    assert len(seen) == 1
    raw, reason = seen[0]
    assert raw == b"not json"
    assert "valid" in reason


def test_on_malformed_called_on_reset_drop() -> None:
    seen: list[tuple[bytes, str]] = []
    p = _parser(on_malformed=lambda raw, reason: seen.append((raw, reason)))
    p.feed(b'{"t":"battery","v":')
    p.reset()
    assert len(seen) == 1
    assert seen[0][0] == b'{"t":"battery","v":'


def test_line_splitter_yields_raw_lines() -> None:
    s = LineSplitter()
    assert s.feed(b"one\r\ntwo\nthree\r\n") == [b"one", b"two", b"three"]


def test_line_splitter_strips_stray_cr_only_at_end() -> None:
    s = LineSplitter()
    # A stray \r mid-line is content; a trailing \r before LF is stripped.
    assert s.feed(b"a\rb\n") == [b"a\rb"]


def test_feed_with_raw_pairs_raw_and_event() -> None:
    p = _parser(clock=lambda: 7.0)
    pairs = p.feed_with_raw(BAT + b"\r\n")
    assert pairs == [
        (BAT, Battery(voltage_mv=8085, current_ma=42, percent=87, received_at=7.0))
    ]


def test_feed_with_raw_yields_malformed_line_with_none_event() -> None:
    p = _parser()
    pairs = p.feed_with_raw(b"Traceback (most recent call last):\r\n" + BAT + b"\r\n")
    assert [raw for raw, _ in pairs] == [b"Traceback (most recent call last):", BAT]
    assert pairs[0][1] is None
    assert isinstance(pairs[1][1], Battery)
    assert p.malformed_count == 1


def test_feed_with_raw_skips_empty_lines_uncounted() -> None:
    p = _parser()
    pairs = p.feed_with_raw(b"\r\n  \r\n" + BAT + b"\r\n")
    assert len(pairs) == 1
    assert p.malformed_count == 0


def test_feed_with_raw_matches_feed_parity() -> None:
    p1, p2 = _parser(), _parser()
    chunk = b"junk\r\n" + HUB + b"\r\n" + b"{bad json\r\n" + IMU + b"\r\n"
    from_feed = p1.feed(chunk)
    from_pairs = [e for _, e in p2.feed_with_raw(chunk) if e is not None]
    assert from_feed == from_pairs
    assert p1.malformed_count == p2.malformed_count == 2
