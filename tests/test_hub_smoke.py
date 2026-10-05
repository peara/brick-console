"""The M1 hardware smoke test: the full chain on the real hub (issue #14).

Part 1 of the M1 finale — the gated pytest pass. With the physical hub
advertising, this drives the exact chain the milestone's exit criterion
rides on, through the Transport seam only (D6):

    discover by name (≤10 s) → connect → stop → install_and_start the
    idle agent (RAM-only, permanent slots untouched) → typed telemetry
    (hub_info + battery + imu) flows within 5 s of connect.

Gated behind ``@pytest.mark.hub`` (testing.md: the hub gate): runs only
with the hub powered on and ``BRICK_CONSOLE_HUB_TESTS=1`` via
``BRICK_CONSOLE_HUB_TESTS=1 uv run pytest -m hub``; the default suite
auto-skips it, so ``uv run pytest`` stays safe with the hub off.

Safety (AGENTS.md rules 2/3, testing.md "no motor tests, ever"): the
agent under test is the D9 passive wrapper — LED/print/sensor reads
only; nothing in this chain drives a Motor. The test must be the only
BLE central: no Pybricks Code session may hold the hub while it runs.
Teardown stops the agent and disconnects, leaving the hub clean.
"""

from __future__ import annotations

import asyncio
import time
import warnings
from pathlib import Path

import pytest

from brick_console.adapter import PybricksDevTransport
from brick_console.events import Battery, HubInfo, Imu, TelemetryEvent
from brick_console.parsing import TelemetryParser
from brick_console.transport import StatusFlags

HUB_NAME = "Pybricks Hub"
"""Advertised-name selector — the only stable one (the address drifts
after re-flashes; api-notes gotcha 2)."""

DISCOVER_WINDOW = 10.0
"""Issue #14 Part 1: ``Transport.discover`` must find the hub in ≤10 s."""

TELEMETRY_WINDOW = 5.0
"""Issue #14 Part 1: ≥1 hub_info + battery + imu within 5 s of connect
(install included — soak evidence: ~1.7 s for stop+install, first
telemetry ~0.2 s later)."""

AGENT_PROGRAM = Path(__file__).resolve().parents[1] / "agent" / "agent_main.py"
"""The console's idle agent wrapper (D9) — the same file the manager
installs (``ble_manager._DEFAULT_AGENT_PROGRAM`` resolves here)."""

_REQUIRED_KINDS = ("hub_info", "battery", "imu")


def _seen_kinds(events: list[TelemetryEvent]) -> set[str]:
    """The required telemetry kinds present in ``events`` so far."""
    seen: set[str] = set()
    for event in events:
        if isinstance(event, HubInfo):
            seen.add("hub_info")
        elif isinstance(event, Battery):
            seen.add("battery")
        elif isinstance(event, Imu):
            seen.add("imu")
    return seen


def _program_running(statuses: list[StatusFlags]) -> bool:
    """Whether any status report carried the user-program-running bit."""
    return any(flags & StatusFlags.USER_PROGRAM_RUNNING for flags in statuses)


@pytest.mark.hub
async def test_m1_chain_on_real_hub() -> None:
    """Discover → connect → install agent → telemetry, on real hardware."""
    transport = PybricksDevTransport()
    parser = TelemetryParser()
    events: list[TelemetryEvent] = []
    statuses: list[StatusFlags] = []

    # Discover by advertised name. A miss means the hub is off/asleep or
    # held by another central — report and stop (AGENTS.md rule 5).
    try:
        discovered = await transport.discover(HUB_NAME, timeout=DISCOVER_WINDOW)
    except TimeoutError as exc:
        pytest.fail(
            f"hub not advertising as {HUB_NAME!r} within {DISCOVER_WINDOW} s — "
            "power it on (or free it from the other central) and rerun; "
            f"discover error: {exc!r}"
        )
    assert discovered.name == HUB_NAME

    # Connect — the full pybricksdev handshake resolves here, so success
    # means "ready for I/O". A refusal usually means another central
    # holds the hub (AGENTS.md rule 3: one central at a time).
    try:
        await transport.connect(discovered, on_disconnect=lambda: None)
    except (ConnectionError, OSError) as exc:
        pytest.fail(
            f"connect to {discovered.name} failed — another BLE central "
            f"may hold the hub; free it and rerun; error: {exc!r}"
        )

    connected_at = time.monotonic()
    try:
        # Subscribe BEFORE install — the manager's own order — so the
        # agent's very first output (its hub_info line) cannot slip past.
        def on_stdout(data: bytes) -> None:
            events.extend(parser.feed(data))

        await transport.subscribe_stdout(on_stdout)
        await transport.subscribe_status(statuses.append)

        # Stop-before-install (D6 sequencing): a leftover program from an
        # earlier crashed session would make the write BUSY.
        await transport.stop()
        await transport.install_and_start(AGENT_PROGRAM)

        # The Part-1 criterion: all three kinds within 5 s of connect,
        # plus the agent confirmed running through the status channel.
        async def collect() -> None:
            while len(_seen_kinds(events)) < len(
                _REQUIRED_KINDS
            ) or not _program_running(statuses):
                await asyncio.sleep(0.05)

        remaining = TELEMETRY_WINDOW - (time.monotonic() - connected_at)
        try:
            await asyncio.wait_for(collect(), timeout=remaining)
        except TimeoutError:
            seen = sorted(_seen_kinds(events))
            missing = sorted(set(_REQUIRED_KINDS) - set(seen))
            pytest.fail(
                f"telemetry incomplete {TELEMETRY_WINDOW} s after connect: "
                f"missing {missing}, saw {seen}; events={len(events)}, "
                f"malformed={parser.malformed_count}, "
                f"program_running_seen={_program_running(statuses)}, "
                f"status_reports={len(statuses)}"
            )

        assert _seen_kinds(events) == set(_REQUIRED_KINDS)
        # End-to-end identity: the name the agent reports is the name
        # discovery selected on (both come from the hub, different paths).
        hub_info = next(e for e in events if isinstance(e, HubInfo))
        assert hub_info.name == HUB_NAME
    finally:
        # Leave the hub clean: stop the agent, release the central role
        # (the soak script's graceful ending). Two separate try blocks —
        # a failed stop must not skip the disconnect, or the hub stays
        # held (rule 3: one central). Best-effort — a dead link must not
        # mask the real failure — but loud, not silent.
        try:
            await transport.stop()
        except (ConnectionError, OSError) as exc:
            warnings.warn(f"smoke teardown: agent stop failed: {exc!r}", stacklevel=2)
        try:
            await transport.disconnect()
        except (ConnectionError, OSError) as exc:
            warnings.warn(
                f"smoke teardown: disconnect failed — the hub may stay held: {exc!r}",
                stacklevel=2,
            )
