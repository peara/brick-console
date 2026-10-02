"""Agent wrapper (issue #15, decision D9): the console's idle agent —
all policy, no mechanism.

Co-located with brick_telemetry.py because pybricksdev's multi-file
compile resolves imports from this directory (docs-review finding 14) —
the library is bundled into the downloaded image.

The library (brick_telemetry) is passive mechanism only; this wrapper
owns every policy decision: the ~10 Hz loop, the ~1 Hz battery cadence,
the hub_info snapshot at start, per-port discovery and re-probing, and
the detach "none" transitions (D7 — only on a transition, never per
cycle). Park-at-attach is emergent, not explicit: discovery constructs
each device (the ColorSensor constructor blips its light once —
identification, not telemetry; D9), the first resting-mode read parks
the sensor, and every subsequent read is same-mode — measured 0 ms —
so the idle robot sits dark and silent in steady state.
"""

import brick_telemetry as bt
from pybricks.hubs import InventorHub
from pybricks.tools import StopWatch, wait

BATTERY_PERIOD = 10

_devices = {}
_last_dev = {}


def main(out=print):
    hub = InventorHub()
    out(bt.hub_info_line(hub))
    watch = StopWatch()
    cycle = 0
    while True:
        if cycle % BATTERY_PERIOD == 0:
            out(bt.battery_line(hub))
        out(bt.imu_line(hub))
        for port, letter in bt.PORTS:
            _port_cycle(port, letter, out)
        cycle += 1
        wait(100 - watch.time() % 100)


def _port_cycle(port, letter, out):
    entry = _devices.get(port)
    if entry is None:
        entry = bt.probe(port)
        if entry is None:
            return
        _devices[port] = entry
    name, dev = entry
    try:
        out(bt.port_line(name, letter, dev))
    except OSError:
        # Detach transition (D7): the only time "none" goes on the wire.
        # The failed device is dropped; later cycles re-probe the empty
        # port and stay silent (constructing on an empty port raises
        # immediately), so "none" prints once per detach.
        del _devices[port]
        if _last_dev.get(port) != "none":
            _last_dev[port] = "none"
            out(bt.none_line(letter))
    else:
        _last_dev[port] = name


main()
