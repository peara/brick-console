"""Hub-side telemetry agent library (issue #15, decision D7).

Prints D7-canonical JSON lines on stdout: ``hub_info`` once per start,
``battery`` about once a second, and ``imu`` plus one line per attached
device at ~10 Hz. Lines are byte-compatible with the server reference
(:mod:`brick_console.events`): compact separators, canonical wire key
order, CRLF added by ``print()`` on the hub.

Byte-exactness is by construction, not dict luck: MicroPython dicts are
hash-ordered (no OrderedDict on this firmware), so lines are built by
string concatenation in the D7 key order; ``json.dumps`` is used only to
JSON-escape the two free-form strings (hub name, firmware version).

Import-safe: importing performs no hardware I/O — the hub is injected
(``TelemetryAgent(hub)``); ``run()`` builds the real one. Device
discovery is a construct-ladder per port (an empty port or a wrong
device type raises ``OSError(ENODEV)``, the documented probe shape), so
ports holding devices outside the D7 set emit nothing.
"""

import json

import pybricks
from pybricks.hubs import InventorHub
from pybricks.parameters import Color, Port, Side
from pybricks.pupdevices import (
    ColorDistanceSensor,
    ColorSensor,
    ForceSensor,
    InfraredSensor,
    Motor,
    TiltSensor,
    UltrasonicSensor,
)
from pybricks.tools import StopWatch, wait

__all__ = [
    "BATTERY_PERIOD",
    "HUB_MODEL",
    "TelemetryAgent",
    "battery_pct",
    "run",
]

HUB_MODEL = "technichub"
BATTERY_PERIOD = 10
"""Cycles between battery lines: the cycle runs at ~10 Hz, so every
10th cycle is ~1 Hz (D7)."""

_PORTS = (
    (Port.A, "A"),
    (Port.B, "B"),
    (Port.C, "C"),
    (Port.D, "D"),
    (Port.E, "E"),
    (Port.F, "F"),
)

_CLASSES = (
    (Motor, "Motor"),
    (ColorSensor, "ColorSensor"),
    (UltrasonicSensor, "UltrasonicSensor"),
    (ForceSensor, "ForceSensor"),
    (ColorDistanceSensor, "ColorDistanceSensor"),
    (TiltSensor, "TiltSensor"),
    (InfraredSensor, "InfraredSensor"),
)

_SIDE_NAMES = {
    Side.TOP: "top",
    Side.BOTTOM: "bottom",
    Side.LEFT: "left",
    Side.RIGHT: "right",
    Side.FRONT: "front",
    Side.BACK: "back",
}

_COLOR_NAMES = {
    Color.RED: "red",
    Color.BROWN: "brown",
    Color.ORANGE: "orange",
    Color.YELLOW: "yellow",
    Color.GREEN: "green",
    Color.CYAN: "cyan",
    Color.BLUE: "blue",
    Color.MAGENTA: "magenta",
    Color.VIOLET: "violet",
    Color.BLACK: "black",
    Color.GRAY: "gray",
    Color.WHITE: "white",
    Color.NONE: "none",
}

# 2S Li-ion discharge approximation (D7): piecewise-linear mV -> %,
# steep near the ends, flatter through the knee, clamped to 0..100
# outside 6000..8400 mV. Approximate under load by design — the raw
# mV/mA ride every battery line.
_BATTERY_CURVE = (
    (8400, 100),
    (8200, 92),
    (8000, 84),
    (7800, 75),
    (7500, 60),
    (7200, 47),
    (6900, 34),
    (6600, 22),
    (6300, 10),
    (6000, 0),
)


def battery_pct(voltage_mv):
    """Approximate charge percent from pack voltage (D7)."""
    if voltage_mv >= 8400:
        return 100
    if voltage_mv <= 6000:
        return 0
    i = 0
    while _BATTERY_CURVE[i][0] > voltage_mv:
        i += 1
    low_mv, low_pct = _BATTERY_CURVE[i]
    if low_mv == voltage_mv:
        return low_pct
    high_mv, high_pct = _BATTERY_CURVE[i - 1]
    return low_pct + (high_pct - low_pct) * (voltage_mv - low_mv) // (high_mv - low_mv)


def _read_motor(dev):
    return f'"angle":{dev.angle()},"speed":{dev.speed()},"load":{dev.load()}'


def _read_color_sensor(dev):
    hsv = dev.hsv()
    col = _COLOR_NAMES.get(dev.color(), "none")
    return f'"refl":{dev.reflection()},"amb":{dev.ambient()},"h":{hsv.h},"s":{hsv.s},"v":{hsv.v},"col":"{col}"'


def _read_ultrasonic(dev):
    pr = "true" if dev.presence() else "false"
    return f'"d":{dev.distance()},"pr":{pr}'


def _read_force(dev):
    f = json.dumps(dev.force())
    d = json.dumps(dev.distance())
    pressed = "true" if dev.pressed() else "false"
    return f'"f":{f},"d":{d},"pressed":{pressed}'


def _read_color_distance(dev):
    hsv = dev.hsv()
    col = _COLOR_NAMES.get(dev.color(), "none")
    return f'"d":{dev.distance()},"refl":{dev.reflection()},"amb":{dev.ambient()},"h":{hsv.h},"s":{hsv.s},"v":{hsv.v},"col":"{col}"'


def _read_tilt(dev):
    pitch, roll = dev.tilt()
    return f'"pitch":{pitch},"roll":{roll}'


def _read_infrared(dev):
    return f'"d":{dev.distance()}'


_READERS = {
    "Motor": _read_motor,
    "ColorSensor": _read_color_sensor,
    "UltrasonicSensor": _read_ultrasonic,
    "ForceSensor": _read_force,
    "ColorDistanceSensor": _read_color_distance,
    "TiltSensor": _read_tilt,
    "InfraredSensor": _read_infrared,
}


def _imu_line(hub):
    ax, ay, az = hub.imu.acceleration()
    gx, gy, gz = hub.imu.angular_velocity()
    up = _SIDE_NAMES[hub.imu.up()]
    return f'{{"t":"imu","ax":{int(ax)},"ay":{int(ay)},"az":{int(az)},"gx":{int(gx)},"gy":{int(gy)},"gz":{int(gz)},"up":"{up}"}}'


class TelemetryAgent:
    """Collects and prints telemetry lines for one hub session."""

    def __init__(self, hub, out=print):
        self._hub = hub
        self._out = out
        self._cycle = 0
        self._devices = {}
        self._last_dev = {}

    def start(self):
        """Emit the ``hub_info`` snapshot (once per session, D7)."""
        info = self._hub.system.info()
        name = json.dumps(info["name"])
        fw = json.dumps(pybricks.version[1])
        self._out(f'{{"t":"hub_info","name":{name},"fw":{fw},"model":"{HUB_MODEL}"}}')

    def cycle(self):
        """One ~10 Hz tick: battery (every Nth), imu, one line per port."""
        if self._cycle % BATTERY_PERIOD == 0:
            voltage_mv = self._hub.battery.voltage()
            current_ma = self._hub.battery.current()
            pct = battery_pct(voltage_mv)
            self._out(
                f'{{"t":"battery","v":{voltage_mv},"c":{current_ma},"pct":{pct}}}'
            )
        self._out(_imu_line(self._hub))
        for port, letter in _PORTS:
            self._port_cycle(port, letter)
        self._cycle += 1

    def _port_cycle(self, port, letter):
        entry = self._devices.get(port)
        if entry is None:
            self._probe(port)
            entry = self._devices.get(port)
        if entry is None:
            return
        name, dev = entry
        try:
            fields = _READERS[name](dev)
        except OSError:
            # Detach transition (D7): the only time "none" goes on the
            # wire. The failed device object is dropped; later cycles
            # re-probe the empty port and stay silent (constructing on an
            # empty port raises immediately), so "none" prints once.
            del self._devices[port]
            if self._last_dev.get(port) != "none":
                self._last_dev[port] = "none"
                self._out(f'{{"t":"port","p":"{letter}","dev":"none"}}')
            return
        self._last_dev[port] = name
        self._out(f'{{"t":"port","p":"{letter}","dev":"{name}",{fields}}}')

    def _probe(self, port):
        for cls, name in _CLASSES:
            try:
                dev = cls(port)
            except OSError:
                continue
            self._devices[port] = (name, dev)
            return


def run():
    """Loop forever at ~10 Hz (the wrapper's entry point)."""
    agent = TelemetryAgent(InventorHub(), out=print)
    agent.start()
    watch = StopWatch()
    while True:
        agent.cycle()
        wait(100 - watch.time() % 100)
