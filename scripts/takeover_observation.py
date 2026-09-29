#!/usr/bin/env python3
"""Evidence harness for issue #24 — what the server sees when an external
BLE central takes the hub (F6).

Mirrors the manager's code path with pybricksdev directly (the real manager
needs agent_main.py, issue #15, not built yet). Two phases, run separately:

    hold    scan -> connect -> install+start hello.py (the pre-approved
            LED/print bring-up program) -> 2 Hz benign GATT read in flight
            -> WAIT for the user to connect with the official app ->
            record the spontaneous disconnect verbatim (callback timing,
            in-flight exception type/message) -> 90 s post-drop
            advertising-visibility loop.

    probe   while the user HOLDS the hub with the official app: visibility
            scan + one connect attempt, recording the exact failure (or
            success) — the held-hub path evidence.

All output is line-logged with millisecond session-relative timestamps.
No motors involved anywhere.
"""

# ruff: noqa: BLE001 — an observation harness records every failure
# verbatim; blanket catches ARE the tool's job, not a smell here.

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from bleak import BleakScanner
from pybricksdev.ble import find_device
from pybricksdev.ble.pybricks import PYBRICKS_SERVICE_UUID, StatusFlag
from pybricksdev.connections import ConnectionState
from pybricksdev.connections.pybricks import FW_REV_UUID, PybricksHubBLE

REPO = Path(__file__).resolve().parents[1]
HELLO = REPO / "hello.py"

_T0 = time.time()


def log(msg: str) -> None:
    t = time.time() - _T0
    print(f"[{t:9.3f}s] {msg}", flush=True)


def _match(device: object, adv: object) -> bool:
    """Visible if the Pybricks service UUID or a 'pybricks' name shows up —
    catches the hub under either advertisement shape (name may be missing
    before SCAN_RSP, service list may be absent on some adv packets)."""
    local_name = getattr(adv, "local_name", None) or getattr(device, "name", None) or ""
    if "pybricks" in str(local_name).lower():
        return True
    return PYBRICKS_SERVICE_UUID in getattr(adv, "service_uuids", [])


def subscribe_all(hub: PybricksHubBLE, disconnected: asyncio.Event) -> None:
    """Log every observable event verbatim — the pre-drop chatter is
    evidence too. The DISCONNECTED arm rule mirrors the repo's
    ``_DisconnectBridge``: the BehaviorSubject replays its current value on
    subscribe (a stale DISCONNECTED or an in-progress CONNECTING), so the
    latch counts a drop only after a real CONNECTED report was seen."""

    armed = [False]

    def on_conn_state(state: ConnectionState) -> None:
        log(f"conn-state: {state.name}")
        if state is ConnectionState.CONNECTED:
            armed[0] = True
        elif state is ConnectionState.DISCONNECTED and armed[0]:
            disconnected.set()

    def on_status(flags: StatusFlag) -> None:
        running = bool(flags & StatusFlag.USER_PROGRAM_RUNNING)
        host = bool(flags & StatusFlag.BLE_HOST_CONNECTED)
        log(f"status: USER_PROGRAM_RUNNING={running} BLE_HOST_CONNECTED={host}")

    def on_stdout(data: bytes) -> None:
        log(f"stdout: {data!r}")

    hub.connection_state_observable.subscribe(on_next=on_conn_state)
    hub.status_observable.subscribe(on_next=on_status)
    hub.stdout_observable.subscribe(on_next=on_stdout)


async def _periodic_read(hub: PybricksHubBLE) -> None:
    """Keep one benign GATT read in flight (~2 Hz) — the exact exception
    type/message an in-flight operation sees at the kick moment."""
    while True:
        try:
            value = await hub.read_gatt_char(FW_REV_UUID)
            log(f"gatt-read: fw={bytes(value).decode(errors='replace')!r}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"gatt-read EXCEPTION: {type(exc).__name__}: {exc}")
            return
        await asyncio.sleep(0.5)


async def _observe_advertising(duration: float, cadence: float) -> None:
    """Post-drop visibility loop: is the hub advertising while (presumably)
    another central holds it? Each sample records present/absent + name +
    address; absence for the full window is the World-3 signal."""
    end = time.monotonic() + duration
    n = 0
    while time.monotonic() < end:
        n += 1
        t_start = time.monotonic()
        device = await BleakScanner.find_device_by_filter(_match, timeout=cadence)
        elapsed = time.monotonic() - t_start
        if device is None:
            log(f"scan[{n}]: not advertising")
        else:
            log(f"scan[{n}]: ADVERTISING name={device.name!r} address={device.address}")
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(max(0.0, min(cadence - elapsed, remaining)))
    log(f"advertising observation done ({n} samples)")


async def hold() -> None:
    log("phase=hold: scanning for 'Pybricks Hub' (25 s window)")
    try:
        device = await find_device("Pybricks Hub", timeout=25)
    except TimeoutError:
        log("SCAN MISS — hub not advertising (auto-powered off?). Wake it and rerun.")
        return
    log(f"discovered: name={device.name!r} address={device.address}")

    hub = PybricksHubBLE(device)
    hub.print_output = False
    disconnected = asyncio.Event()
    subscribe_all(hub, disconnected)

    log("connecting…")
    await asyncio.wait_for(hub.connect(), timeout=30)
    log("CONNECTED (handshake complete)")

    try:
        await asyncio.wait_for(
            hub.run(str(HELLO), wait=False, print_output=False, line_handler=False),
            timeout=30,
        )
        log("hello.py installed and started")
    except Exception as exc:
        log(f"run() raised {type(exc).__name__}: {exc}")

    reader = asyncio.create_task(_periodic_read(hub))
    log("=" * 60)
    log("READY — user: connect with the official app NOW")
    log("=" * 60)

    try:
        await asyncio.wait_for(disconnected.wait(), timeout=1200)
    except TimeoutError:
        log(
            "no disconnect within 20 min — hub auto-power-off or app never "
            "connected; NOT takeover evidence. Ending."
        )
        reader.cancel()
        await hub.disconnect()
        return

    # Give the in-flight read a beat to surface its kick-moment exception.
    await asyncio.sleep(1.0)
    reader.cancel()
    log("=" * 60)
    log("DISCONNECT recorded — starting 90 s advertising-visibility loop")
    log("=" * 60)
    await _observe_advertising(duration=90.0, cadence=2.0)
    try:
        await hub.disconnect()
    except Exception as exc:
        log(f"final disconnect: {type(exc).__name__}: {exc}")
    log("phase=hold complete")


async def probe() -> None:
    log("phase=probe: user HOLDS the hub with the official app")
    log("scanning for visibility (25 s window)…")
    device = await BleakScanner.find_device_by_filter(_match, timeout=25)
    if device is None:
        log("NOT VISIBLE while held — hub does not advertise while a central holds it")
        return
    log(f"visible while held: name={device.name!r} address={device.address}")

    hub = PybricksHubBLE(device)
    hub.print_output = False
    disconnected = asyncio.Event()
    subscribe_all(hub, disconnected)

    log("attempting connect while held…")
    try:
        await asyncio.wait_for(hub.connect(), timeout=30)
        log("CONNECT SUCCEEDED WHILE HELD — last-central-wins (holder was kicked)")
    except Exception as exc:
        log(f"CONNECT FAILED: {type(exc).__name__}: {exc}")
    finally:
        try:
            await hub.disconnect()
        except Exception as exc:
            log(f"cleanup disconnect: {type(exc).__name__}: {exc}")
    log("phase=probe complete")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("hold", "probe"):
        sys.exit(f"usage: {Path(sys.argv[0]).name} hold|probe")
    phase = sys.argv[1]
    asyncio.run(hold() if phase == "hold" else probe())


if __name__ == "__main__":
    main()
