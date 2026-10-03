"""Hub-side telemetry library (issue #15, decisions D7 + D9) — passive
mechanism only, no policy.

This module is what both hub-side programs import: the console's idle
agent (``agent_main.py``) and, in M2, user programs that opt into
telemetry (BRD R5). It provides exactly the passive pieces of the D7
wire contract:

- ``battery_pct`` — the pct derivation curve;
- one builder per telemetry line — byte-exact against the server
  reference (:mod:`brick_console.events`): compact separators,
  canonical wire key order, CRLF added by ``print()`` on the hub;
- the per-port discovery probe.

Per D9 the idle agent is a passive guest: it never illuminates, never
pings, never reads a sensor's active fields. Concretely, this library
contains **no active reads** — no ``reflection``/``hsv``/``color`` on a
color sensor, no ``distance`` on an ultrasonic sensor — and no
``run()``/loop/cadence: policy belongs to the calling program. The
InfraredSensor is absent even from the probe ladder: it is an active IR
emitter (LEGO-documented, 7 kHz pulsed) with no passive mode, so the
idle agent neither detects nor reads it.

Reading-mode tags (D9): mode-dependent sensor lines carry a ``mode`` key
asserting which reading the values belong to — ColorSensor
``"ambient"`` (light off) / ``"surface"`` (light on), UltrasonicSensor
``"presence"`` (transmitter off) / ``"distance"`` (active ping). The
publisher asserts the tag because it must: the LUMP driver tracks the
current mode, but v4.0.1's Python API cannot read it — the one who
drives the sensor is the only one who can declare what was read. This
library emits only the passive tags; the surface/distance builders
arrive with M2 (#36) when programs need them.

Byte-exactness is by construction, not dict luck: MicroPython dicts are
hash-ordered (no OrderedDict on this firmware), so lines are built by
string concatenation in the D7 key order; ``json.dumps`` is used only to
JSON-escape free-form strings (hub name, firmware version) and to keep
float repr fidelity on ForceSensor values.

Import-safe: importing performs no hardware I/O — every builder takes
the hub/device as an argument; the probe constructs on explicit call.
"""

import json

import pybricks
from pybricks.parameters import Port, Side
from pybricks.pupdevices import (
    ColorDistanceSensor,
    ColorSensor,
    ForceSensor,
    Motor,
    TiltSensor,
    UltrasonicSensor,
)

__all__ = [
    "HUB_MODEL",
    "PORTS",
    "battery_line",
    "battery_pct",
    "color_ambient_line",
    "color_distance_ambient_line",
    "force_line",
    "hub_info_line",
    "imu_line",
    "motor_line",
    "none_line",
    "port_line",
    "probe",
    "tilt_line",
    "ultrasonic_presence_line",
]

HUB_MODEL = "technichub"

PORTS = (
    (Port.A, "A"),
    (Port.B, "B"),
    (Port.C, "C"),
    (Port.D, "D"),
    (Port.E, "E"),
    (Port.F, "F"),
)

# Discovery ladder (the documented probe shape): constructing a device
# class on an empty port or a port holding a different device type
# raises OSError(ENODEV), so first constructor wins. InfraredSensor is
# deliberately absent — an active IR emitter with no passive mode is
# not the idle agent's business (D9); programs that own one (M2) may
# still publish its single-mode `d` under the wire dictionary.
_CLASSES = (
    (Motor, "Motor"),
    (ColorSensor, "ColorSensor"),
    (UltrasonicSensor, "UltrasonicSensor"),
    (ForceSensor, "ForceSensor"),
    (ColorDistanceSensor, "ColorDistanceSensor"),
    (TiltSensor, "TiltSensor"),
)

_SIDE_NAMES = {
    Side.TOP: "top",
    Side.BOTTOM: "bottom",
    Side.LEFT: "left",
    Side.RIGHT: "right",
    Side.FRONT: "front",
    Side.BACK: "back",
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


def probe(port):
    """Identify the device on ``port`` — ``(dev name, device)`` or ``None``.

    The typed construct-ladder: the ColorSensor constructor's own
    blocking RGB_I read blips its light once (~60 ms, the one
    attach-time actuation — identification, not telemetry; D9); the
    other constructors are verified clean.
    """
    for cls, name in _CLASSES:
        try:
            return name, cls(port)
        except OSError:
            continue
    return None


def hub_info_line(hub):
    """The session snapshot line (D7): name, firmware, model literal."""
    info = hub.system.info()
    name = json.dumps(info["name"])
    fw = json.dumps(pybricks.version[1])
    return f'{{"t":"hub_info","name":{name},"fw":{fw},"model":"{HUB_MODEL}"}}'


def battery_line(hub):
    """The ~1 Hz battery line: v mV, c mA, pct derived via battery_pct."""
    voltage_mv = hub.battery.voltage()
    current_ma = hub.battery.current()
    pct = battery_pct(voltage_mv)
    return f'{{"t":"battery","v":{voltage_mv},"c":{current_ma},"pct":{pct}}}'


def imu_line(hub):
    """The ~10 Hz IMU line: accel mm/s², gyro °/s, up side string."""
    ax, ay, az = hub.imu.acceleration()
    gx, gy, gz = hub.imu.angular_velocity()
    up = _SIDE_NAMES[hub.imu.up()]
    return f'{{"t":"imu","ax":{int(ax)},"ay":{int(ay)},"az":{int(az)},"gx":{int(gx)},"gy":{int(gy)},"gz":{int(gz)},"up":"{up}"}}'


def motor_line(letter, dev):
    """Motor port line — single mode, no reading-mode tag (all passive)."""
    return f'{{"t":"port","p":"{letter}","dev":"Motor","angle":{dev.angle()},"speed":{dev.speed()},"load":{dev.load()}}}'


def color_ambient_line(letter, dev):
    """ColorSensor resting reading (D9): light off, ambient only."""
    return f'{{"t":"port","p":"{letter}","dev":"ColorSensor","mode":"ambient","amb":{dev.ambient()}}}'


def ultrasonic_presence_line(letter, dev):
    """UltrasonicSensor resting reading (D9): transmitter off, listen only."""
    pr = "true" if dev.presence() else "false"
    return f'{{"t":"port","p":"{letter}","dev":"UltrasonicSensor","mode":"presence","pr":{pr}}}'


def force_line(letter, dev):
    """ForceSensor port line — single mode, no tag; floats keep repr fidelity."""
    f = json.dumps(dev.force())
    d = json.dumps(dev.distance())
    pressed = "true" if dev.pressed() else "false"
    return f'{{"t":"port","p":"{letter}","dev":"ForceSensor","f":{f},"d":{d},"pressed":{pressed}}}'


def tilt_line(letter, dev):
    """TiltSensor port line — single mode, no tag."""
    pitch, roll = dev.tilt()
    return f'{{"t":"port","p":"{letter}","dev":"TiltSensor","pitch":{pitch},"roll":{roll}}}'


def color_distance_ambient_line(letter, dev):
    """ColorDistanceSensor resting reading (D9): ambient only."""
    return f'{{"t":"port","p":"{letter}","dev":"ColorDistanceSensor","mode":"ambient","amb":{dev.ambient()}}}'


def none_line(letter):
    """The detach-transition line (D7): emitted only on attach → detach."""
    return f'{{"t":"port","p":"{letter}","dev":"none"}}'


_LINES = {
    "Motor": motor_line,
    "ColorSensor": color_ambient_line,
    "UltrasonicSensor": ultrasonic_presence_line,
    "ForceSensor": force_line,
    "ColorDistanceSensor": color_distance_ambient_line,
    "TiltSensor": tilt_line,
}


def port_line(dev_name, letter, dev):
    """Dispatch to the passive line builder for a discovered device."""
    return _LINES[dev_name](letter, dev)
