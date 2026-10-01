"""Hub-side telemetry agent library (issue #15, decisions D7 + D9).

Prints D7-canonical JSON lines on stdout: ``hub_info`` once per start,
``battery`` about once a second, and ``imu`` plus one line per attached
device at ~10 Hz. Lines are byte-compatible with the server reference
(:mod:`brick_console.events`): compact separators, canonical wire key
order, CRLF added by ``print()`` on the hub.

Freshness (D9): same-mode fields are read fresh every cycle; cross-mode
secondary fields (ColorSensor ``amb``, UltrasonicSensor ``pr``,
ColorDistanceSensor ``d``/``amb``) re-read every ``SECONDARY_REFRESH``
cycles with the last value cached between — a PUP mode switch costs
30-60 ms, and four per cycle would cap the loop at ~4 Hz with the full
kit attached. Caches seed at attach, so the first line after discovery
carries measured values.

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
    "SECONDARY_REFRESH",
    "TelemetryAgent",
    "battery_pct",
    "letter_offset",
    "run",
]

HUB_MODEL = "technichub"
BATTERY_PERIOD = 10
"""Cycles between battery lines: the cycle runs at ~10 Hz, so every
10th cycle is ~1 Hz (D7)."""
SECONDARY_REFRESH = 10
"""Cycles between cross-mode secondary reads (D9): ~1 s at 10 Hz, the
same cadence class as battery. Per-port staggered offsets spread the
mode-switch chains evenly across the window."""


def letter_offset(letter):
    """A=0 .. F=5 — the D9 per-port refresh stagger."""
    return ord(letter) - 65


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


class _Reader:
    """Per-device field reader (D9): primary fields fresh every cycle
    (same-mode reads, 0 ms), secondary fields re-read every
    ``SECONDARY_REFRESH`` cycles with the last value cached between.
    The cache seeds at attach (the one-time mode chain is paid inside
    discovery's blocking construct), so the first line after discovery
    carries genuinely measured secondaries — never placeholder zeros.
    """

    def __init__(self, dev):
        self.dev = dev
        self.amb = dev.ambient()

    def read(self, cycle):
        hsv = self.dev.hsv()
        col = _COLOR_NAMES.get(self.dev.color(), "none")
        return f'"refl":{self.dev.reflection()},"amb":{self.amb},"h":{hsv.h},"s":{hsv.s},"v":{hsv.v},"col":"{col}"'

    def refresh(self):
        # Secondaries first: the cycle ends on the primary mode, so
        # non-refresh cycles pay zero mode switches.
        self.amb = self.dev.ambient()
        self.dev.reflection()


class _ColorReader(_Reader):
    pass


class _UltrasonicReader(_Reader):
    def __init__(self, dev):
        self.dev = dev
        self.pr = dev.presence()

    def read(self, cycle):
        pr = "true" if self.pr else "false"
        return f'"d":{self.dev.distance()},"pr":{pr}'

    def refresh(self):
        self.pr = self.dev.presence()
        self.dev.distance()


class _ForceReader(_Reader):
    def __init__(self, dev):
        self.dev = dev

    def read(self, cycle):
        f = json.dumps(self.dev.force())
        d = json.dumps(self.dev.distance())
        pressed = "true" if self.dev.pressed() else "false"
        return f'"f":{f},"d":{d},"pressed":{pressed}'

    def refresh(self):
        pass


class _ColorDistanceReader(_Reader):
    def __init__(self, dev):
        self.dev = dev
        self.d = dev.distance()
        self.amb = dev.ambient()

    def read(self, cycle):
        hsv = self.dev.hsv()
        col = _COLOR_NAMES.get(self.dev.color(), "none")
        return f'"d":{self.d},"refl":{self.dev.reflection()},"amb":{self.amb},"h":{hsv.h},"s":{hsv.s},"v":{hsv.v},"col":"{col}"'

    def refresh(self):
        self.d = self.dev.distance()
        self.amb = self.dev.ambient()
        self.dev.reflection()


class _TiltReader(_Reader):
    def __init__(self, dev):
        self.dev = dev

    def read(self, cycle):
        pitch, roll = self.dev.tilt()
        return f'"pitch":{pitch},"roll":{roll}'

    def refresh(self):
        pass


class _InfraredReader(_Reader):
    def __init__(self, dev):
        self.dev = dev

    def read(self, cycle):
        return f'"d":{self.dev.distance()}'

    def refresh(self):
        pass


class _MotorReader(_Reader):
    def __init__(self, dev):
        self.dev = dev

    def read(self, cycle):
        return f'"angle":{self.dev.angle()},"speed":{self.dev.speed()},"load":{self.dev.load()}'

    def refresh(self):
        pass


_READERS = {
    "Motor": _MotorReader,
    "ColorSensor": _ColorReader,
    "UltrasonicSensor": _UltrasonicReader,
    "ForceSensor": _ForceReader,
    "ColorDistanceSensor": _ColorDistanceReader,
    "TiltSensor": _TiltReader,
    "InfraredSensor": _InfraredReader,
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
        reader = self._devices.get(port)
        if reader is None:
            self._probe(port)
            reader = self._devices.get(port)
        if reader is None:
            return
        try:
            if self._cycle % SECONDARY_REFRESH == letter_offset(letter):
                reader.refresh()
            fields = reader.read(self._cycle)
        except OSError:
            # Detach transition (D7): the only time "none" goes on the
            # wire. The failed reader is dropped; later cycles re-probe
            # the empty port and stay silent (constructing on an empty
            # port raises immediately), so "none" prints once.
            del self._devices[port]
            if self._last_dev.get(port) != "none":
                self._last_dev[port] = "none"
                self._out(f'{{"t":"port","p":"{letter}","dev":"none"}}')
            return
        self._last_dev[port] = reader.name
        self._out(f'{{"t":"port","p":"{letter}","dev":"{reader.name}",{fields}}}')

    def _probe(self, port):
        for cls, name in _CLASSES:
            try:
                dev = cls(port)
            except OSError:
                continue
            reader = _READERS[name](dev)
            reader.name = name
            self._devices[port] = reader
            return


def run():
    """Loop forever at ~10 Hz (the wrapper's entry point)."""
    agent = TelemetryAgent(InventorHub(), out=print)
    agent.start()
    watch = StopWatch()
    while True:
        agent.cycle()
        wait(100 - watch.time() % 100)
