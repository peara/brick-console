#!/usr/bin/env python3
"""Q3 soak watcher (issue #14): hours-long observation of the live console.

The Q3 obligation is box-side ops work, not pytest (testing.md — the hub
gate): start the console, leave the connection up for hours, record hours
survived and every BlueZ disconnect. This script is the observing half;
the product under test is the real server with its own bounded
rescan/reconnect loop (the sanctioned F6 exemption, AGENTS.md rule 5):

    server:   uv run brick-console
    watcher:  uv run python scripts/soak_watch.py <run_dir>

The watcher never touches BLE — it polls ``GET /healthz`` (default 1 s)
and records the manager's hub state + reason into
``<run_dir>/timeline.tsv``. A state edge ``agent`` -> anything else is one
disconnect; ``server_down`` rows (watcher lost the server, e.g. a
restart) are counted separately so a server restart is never misread as
a hub drop. SIGINT/SIGTERM end the watch and print + write the summary:
sessions, per-session durations and drop reasons, reconnect latencies,
agent-held fraction.

Resolution is one poll interval: a disconnect+reconnect cycle faster
than the interval collapses into a single row. The manager's reconnect
is never that fast (backoff + BLE handshake take seconds), so disconnect
counts are safe at the default 1 s.

Runbook for the soak itself (hub on): start both under tmux, watch for
hours, ``tmux send-keys -t q3-watch C-c``, post ``<run_dir>/timeline.tsv``
+ ``summary.txt`` to the issue.
"""

from __future__ import annotations

import argparse
import itertools
import json
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HUB_DISCONNECT_STATES = ("offline", "advertising")
"""Session-end states that mean the *hub* link dropped (vs server_down,
which means the watcher lost sight of the server itself)."""

DEFAULT_BASE_URL = "http://127.0.0.1:8300"
DEFAULT_INTERVAL = 1.0
DEFAULT_HEARTBEAT = 60.0


@dataclass(frozen=True)
class Row:
    """One observation of the manager's hub state (or ``server_down``)."""

    mono: float
    epoch: float
    state: str
    reason: str


@dataclass
class Session:
    """A maximal run of ``agent`` rows; ``dropped`` is the first non-agent
    row after it (``None`` = still agent when the watch ended)."""

    started: Row
    dropped: Row | None = None


def poll_healthz(base_url: str, timeout: float) -> tuple[str, str]:
    """Return ``(hub.state, hub.reason)`` from ``GET /healthz``."""
    url = f"{base_url.rstrip('/')}/healthz"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    hub = payload["hub"]
    return str(hub["state"]), str(hub["reason"])


def extract_sessions(rows: list[Row]) -> list[Session]:
    """Group the timeline into agent sessions.

    A session opens at the first ``agent`` row following a non-agent row
    (or the start of the watch) and closes at the first non-agent row.
    """
    sessions: list[Session] = []
    for row in rows:
        if row.state == "agent":
            if not sessions or sessions[-1].dropped is not None:
                sessions.append(Session(started=row))
        elif sessions and sessions[-1].dropped is None:
            sessions[-1].dropped = row
    return sessions


def summary_text(
    rows: list[Row],
    started_wall: float,
    ended_wall: float,
    *,
    interval: float,
    changes: int,
    server_down_events: int,
) -> str:
    """The soak summary: hours, sessions, disconnects, reconnect latencies."""
    duration = ended_wall - started_wall
    sessions = extract_sessions(rows)
    hub_drops = [
        s
        for s in sessions
        if s.dropped is not None and s.dropped.state in HUB_DISCONNECT_STATES
    ]
    censored = [s for s in sessions if s.dropped is None]
    agent_time = sum(
        (s.dropped.mono if s.dropped is not None else duration) - s.started.mono
        for s in sessions
    )
    lines: list[str] = []
    a = lines.append
    a("Q3 soak watch summary")
    a(
        f"watched: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_wall))}"
        f" -> {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ended_wall))}"
        f"  ({duration / 3600:.2f} h)"
    )
    a(
        f"poll interval: {interval:g} s; timeline rows: {len(rows)};"
        f" state changes: {changes}; server-down events: {server_down_events}"
    )
    a(
        f"agent sessions: {len(sessions)}"
        f" — hub disconnects: {len(hub_drops)}"
        f" — censored at watch end: {len(censored)}"
    )
    for i, s in enumerate(sessions, 1):
        start = s.started.mono
        end = s.dropped.mono if s.dropped is not None else duration
        if s.dropped is not None:
            drop = f" -> {s.dropped.state}: {s.dropped.reason}"
        else:
            drop = " (still agent at watch end)"
        a(
            f"  #{i}  +{start:8.1f}s .. +{end:8.1f}s ({(end - start) / 60:9.1f} min){drop}"
        )
    latencies = [
        s.started.mono - prev.dropped.mono
        for prev, s in itertools.pairwise(sessions)
        if prev.dropped is not None
    ]
    if latencies:
        a(
            "reconnect latencies (drop -> agent, s): "
            + ", ".join(f"{x:.1f}" for x in latencies)
        )
    if duration > 0:
        a(
            f"agent-held time: {agent_time / 3600:.2f} h / {duration / 3600:.2f} h"
            f" ({100 * agent_time / duration:.1f}%)"
        )
    return "\n".join(lines)


def _selftest() -> int:
    """Session math over a synthetic timeline — asserts, exits 0 on pass."""
    t = 0.0

    def row(state: str, reason: str = "") -> Row:
        nonlocal t
        t += 1.0
        return Row(mono=t, epoch=t, state=state, reason=reason)

    rows = [
        row("offline", "starting"),
        row("advertising", "discovered"),
        row("agent", "installed"),
        row("agent", ""),
        row("offline", "hub disconnected"),
        row("offline", "scan timeout"),
        row("agent", "installed again"),
        row("server_down", "connection refused"),
        row("agent", "back"),
        row("agent", "still"),
    ]
    sessions = extract_sessions(rows)
    assert len(sessions) == 3, f"expected 3 sessions, got {len(sessions)}"
    assert sessions[0].started.mono == 3.0 and sessions[0].dropped is not None
    assert sessions[0].dropped.state == "offline", "first drop must be the offline row"
    assert (
        sessions[1].dropped is not None and sessions[1].dropped.state == "server_down"
    )
    assert sessions[2].dropped is None, "last session must be censored"
    hub_drops = [
        s
        for s in sessions
        if s.dropped is not None and s.dropped.state in HUB_DISCONNECT_STATES
    ]
    assert len(hub_drops) == 1, "server_down must not count as a hub disconnect"
    text = summary_text(rows, 0.0, 10.0, interval=1.0, changes=8, server_down_events=1)
    assert "hub disconnects: 1" in text
    assert "censored at watch end: 1" in text
    assert "reconnect latencies" in text
    print("selftest OK — session math, censoring, server_down separation verified")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "run_dir", nargs="?", help="output dir (timeline.tsv, summary.txt)"
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=DEFAULT_HEARTBEAT,
        help="seconds between keep-alive timeline rows when nothing changes",
    )
    parser.add_argument(
        "--selftest", action="store_true", help="run built-in assertions and exit"
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.run_dir:
        parser.error("run_dir is required (unless --selftest)")

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    stop = {"flag": False}

    def request_stop(signum: int, frame: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    started_wall = time.time()
    rows: list[Row] = []
    changes = 0
    server_down_events = 0
    last_key: tuple[str, str] | None = None
    last_write = 0.0

    with (run_dir / "timeline.tsv").open("w", encoding="utf-8") as timeline:
        timeline.write("unix_s\tmono_s\tstate\treason\n")
        while not stop["flag"]:
            now = time.time()
            try:
                state, reason = poll_healthz(
                    args.base_url, timeout=max(args.interval, 2.0)
                )
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                KeyError,
                ValueError,
            ) as exc:
                state, reason = "server_down", repr(exc)[:200]
            row = Row(
                mono=now - started_wall,
                epoch=now,
                state=state,
                reason=reason.replace("\t", " ").replace("\n", " "),
            )
            key = (row.state, row.reason)
            changed = key != last_key
            if changed:
                changes += 1
                was_down = last_key is not None and last_key[0] == "server_down"
                if row.state == "server_down" and not was_down:
                    # One event per transition INTO server_down — however
                    # the error text varies while an outage lasts, one
                    # outage (or restart) stays one event; and a
                    # server_down row is never counted as a hub drop.
                    server_down_events += 1
            if changed or now - last_write >= args.heartbeat:
                timeline.write(
                    f"{row.epoch:.3f}\t{row.mono:.3f}\t{row.state}\t{row.reason}\n"
                )
                timeline.flush()
                last_write = now
            last_key = key
            rows.append(row)
            # Sleep in short slices so SIGTERM lands within ~0.2 s even
            # mid-interval (tmux kill must not wedge the watcher).
            deadline = time.monotonic() + args.interval
            while not stop["flag"] and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    ended_wall = time.time()
    text = summary_text(
        rows,
        started_wall,
        ended_wall,
        interval=args.interval,
        changes=changes,
        server_down_events=server_down_events,
    )
    print(text)
    (run_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
