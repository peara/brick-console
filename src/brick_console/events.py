"""Telemetry event model (issue #3, decision D7): the dashboard's data shape.

Everything the dashboard ever shows — battery, ports, IMU, hub identity —
travels as *telemetry events* (D7 terminology): small JSON lines the
hub-side ``brick_telemetry`` agent prints over hub stdout, one JSON object
per ``print()``. This module is the contract both sides code against: the
hub-side encoder (a later issue) emits the canonical lines below; the server
decodes them (:func:`decode`) into typed events and can re-emit
(:func:`encode`) byte-exactly. Turning raw stdout *bytes* into lines and
lines into events — framing, chunk buffering, the malformed counter — is
:mod:`brick_console.parsing`'s job; this module is pure wire ⇄ Python.

The wire (D7): UTF-8 JSON, compact separators, **CRLF-terminated** lines
(MicroPython's ``print()`` line ending). The kind key is ``"t"`` with short
wire keys throughout; the Python side uses full-word names (``"v"`` →
``voltage_mv``). There is no wire timestamp and no sequence number — the hub
has no wall clock, ordering is per-connection, and the parser stamps
``received_at`` (float UNIX seconds, injectable clock) at successful decode.
``received_at`` is receipt metadata, not wire content: :func:`encode` never
emits it, and it is excluded from event equality (events compare by wire
content alone). ``hub_info`` is a *snapshot event*: the server caches the
latest one outside the event ring and the WS gateway (architecture §2.1)
replays it to joining clients before the live stream — this module only fixes
that classification; the caching belongs to the telemetry store and the WS
gateway (D7).

Canonical examples — the whole wire format on one page (CRLF terminators
omitted; :func:`encode` appends them)::

    {"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}
    {"t":"battery","v":8085,"c":42,"pct":87}
    {"t":"imu","ax":120,"ay":-980,"az":9810,"gx":0,"gy":0,"gz":3,"up":"top"}
    {"t":"port","p":"A","dev":"Motor","angle":12,"speed":0,"load":0}
    {"t":"port","p":"B","dev":"ColorSensor","refl":34,"amb":12,"h":10,"s":80,"v":90,"col":"red"}
    {"t":"port","p":"C","dev":"ForceSensor","f":0.0,"d":0.0,"pressed":false}
    {"t":"port","p":"D","dev":"UltrasonicSensor","d":245,"pr":false}
    {"t":"port","p":"E","dev":"TiltSensor","pitch":3,"roll":-2}
    {"t":"port","p":"F","dev":"none"}

Units and meanings (D7 field dictionary):

- ``hub_info`` — ``name`` BLE name · ``fw`` firmware string · ``model`` hub
  model literal ``"technichub"``. Emitted once per connect.
- ``battery`` (~1 Hz) — ``v`` mV · ``c`` mA · ``pct`` 0–100 %, derived
  hub-side from ``v`` (Pybricks v4 exposes no battery-percent API; raw mV/mA
  always accompany the approximation).
- ``imu`` (~10 Hz) — ``ax``/``ay``/``az`` mm/s² (TechnicHub mounting +Z top,
  +X front; ≈ +9810 on the up axis at rest) · ``gx``/``gy``/``gz`` °/s
  (right-hand rule) · ``up`` side string (top/bottom/left/right/front/back).
- ``port`` (~10 Hz, one line per attached device, unconditional per cycle;
  ``dev:"none"`` only on a detach transition) — ``p`` A–F · ``dev`` device
  string from an **open set** (kept as a plain passthrough string, never a
  closed Python enum) · per-device keys:

  - ``Motor`` — ``angle`` ° (output shaft) · ``speed`` °/s (100 ms window) ·
    ``load`` **mNm** (torque estimate, not %).
  - ``ColorSensor`` — ``refl`` % · ``amb`` % · ``h`` 0–360° · ``s`` % ·
    ``v`` % · ``col`` color name or ``"none"``.
  - ``ForceSensor`` — ``f`` N (~0–10) · ``d`` mm (~0–8) · ``pressed`` bool
    (3 N threshold).
  - ``UltrasonicSensor`` — ``d`` mm (2000 = no echo) · ``pr`` bool.
  - ``ColorDistanceSensor`` — ``d`` **%** (BOOST-era unit trap, not mm),
    plus ``refl``/``amb``/``h``/``s``/``v``/``col``.
  - ``TiltSensor`` — ``pitch`` ° · ``roll`` °.
  - ``InfraredSensor`` — ``d`` % relative (WeDo motion sensor).
  - ``none`` — empty port, no extra keys.

Unknown/malformed policy (D7): a ``"t"`` other than the four known kinds
wraps as :class:`UnknownEvent` carrying the parsed JSON object verbatim
(forward compatibility is versioned by addition — new kinds and new fields
on known kinds are safely ignorable). Unknown fields on known kinds are
ignored, as are the unmapped extra keys of an unknown ``dev`` string; the
raw line (retained by the raw-log-primary path — the telemetry store's
raw-line ring, D7) remains the verbatim record. Wire-level failures — invalid JSON/UTF-8, a non-object payload, a
missing or non-string ``"t"``, or a known kind missing required fields or
carrying wrongly-typed ones — raise :class:`EventDecodeError`, which the
parser counts as malformed and skips; decoding never guesses. Numbers keep
their JSON type (``12`` stays ``int``, never ``12.0``) so the canonical
lines re-encode byte-exactly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = [
    "Battery",
    "EventDecodeError",
    "HubInfo",
    "Imu",
    "JsonNumber",
    "JsonValue",
    "Port",
    "TelemetryEvent",
    "UnknownEvent",
    "decode",
    "encode",
]

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
"""Any JSON value, exactly as ``json.loads`` produces it (verbatim)."""

type JsonNumber = int | float
"""A JSON number. ``int``-ness is preserved (``12`` stays ``12``, never
``12.0``) so canonical lines re-encode byte-exactly; ``bool`` — a subclass
of ``int`` in Python — is never accepted where a number is required."""


class EventDecodeError(ValueError):
    """A line that cannot be decoded into a telemetry event.

    Raised by :func:`decode` for wire-level problems: invalid JSON or UTF-8,
    a non-object JSON payload, a missing or non-string ``"t"`` key, or a
    known kind missing required fields or carrying wrongly-typed ones. The
    parser (:mod:`brick_console.parsing`) catches these, counts the line as
    malformed, and keeps the stream alive — decode errors are expected data,
    not bugs.
    """


@dataclass(frozen=True)
class HubInfo:
    """Snapshot telemetry event: hub identity, emitted once per connect (D7).

    The server caches the latest one outside the event ring (the telemetry
    store, D7) and the WS gateway (architecture §2.1) replays it to joining
    clients before the live stream; this module only classifies the event, it
    does not implement that caching.
    """

    name: str  # wire "name": advertised BLE name (e.g. "Pybricks Hub")
    firmware: str  # wire "fw": Pybricks version string (e.g. "4.0.1")
    model: str  # wire "model": hub model literal, "technichub"
    received_at: float | None = field(
        default=None, compare=False
    )  # stamped by the parser


@dataclass(frozen=True)
class Battery:
    """Telemetry event: battery, ~1 Hz."""

    voltage_mv: JsonNumber  # wire "v": millivolts
    current_ma: JsonNumber  # wire "c": milliamps
    percent: (
        JsonNumber  # wire "pct": 0-100, derived hub-side from "v" (2S Li-ion curve)
    )
    received_at: float | None = field(
        default=None, compare=False
    )  # stamped by the parser


@dataclass(frozen=True)
class Imu:
    """Telemetry event: inertial measurement unit, ~10 Hz."""

    accel: tuple[
        JsonNumber, JsonNumber, JsonNumber
    ]  # wire "ax"/"ay"/"az": mm/s² (+Z top, +X front; ≈ +9810 up at rest)
    gyro: tuple[
        JsonNumber, JsonNumber, JsonNumber
    ]  # wire "gx"/"gy"/"gz": °/s (right-hand rule)
    up: str  # wire "up": side string top/bottom/left/right/front/back
    received_at: float | None = field(
        default=None, compare=False
    )  # stamped by the parser


@dataclass(frozen=True)
class Port:
    """Telemetry event: one hub port, ~10 Hz, one line per attached device.

    Fields are flat per-device values; only the fields belonging to
    ``device`` (per ``_PORT_SPECS``) are ever set, the rest stay ``None``.
    Unknown device strings pass through with no typed fields (open set,
    never an error); their extra wire keys are ignored like any unknown
    field on a known kind, and the raw line remains the verbatim record.
    """

    port: str  # wire "p": port letter A-F
    device: str  # wire "dev": device string, open set (passthrough, never an enum)
    received_at: float | None = field(
        default=None, compare=False
    )  # stamped by the parser

    angle_deg: JsonNumber | None = None  # Motor "angle": output-shaft angle
    speed_dps: JsonNumber | None = None  # Motor "speed": °/s, 100 ms window
    load_mnm: JsonNumber | None = (
        None  # Motor "load": torque estimate in mNm — NOT a percentage
    )

    reflection_pct: JsonNumber | None = (
        None  # "refl": ColorSensor / ColorDistanceSensor
    )
    ambient_pct: JsonNumber | None = None  # "amb"
    hue_deg: JsonNumber | None = None  # "h": 0-360
    saturation_pct: JsonNumber | None = None  # "s"
    value_pct: JsonNumber | None = None  # "v"
    color: str | None = None  # "col": color name or "none"

    distance_mm: JsonNumber | None = None  # "d" on ForceSensor / UltrasonicSensor
    distance_pct: JsonNumber | None = (
        None  # "d" on ColorDistanceSensor / InfraredSensor (%, not mm)
    )
    force_n: JsonNumber | None = None  # ForceSensor "f": newtons, ~0-10
    pressed: bool | None = None  # ForceSensor "pressed": 3 N threshold
    presence: bool | None = None  # UltrasonicSensor "pr"; "d" == 2000 mm means no echo
    pitch_deg: JsonNumber | None = None  # TiltSensor "pitch"
    roll_deg: JsonNumber | None = None  # TiltSensor "roll"


@dataclass(frozen=True)
class UnknownEvent:
    """Telemetry event whose ``"t"`` is not one of the four known kinds (D7).

    Forward compatibility: new kinds are versioned in by addition, and an old
    server wraps them verbatim instead of erroring. ``data`` is the parsed
    JSON object exactly as ``json.loads`` produced it — including the ``"t"``
    key — nothing filtered, nothing renamed, so :func:`encode` re-emits it
    unchanged.
    """

    kind: str  # the unknown "t" value
    data: dict[str, JsonValue]  # the parsed JSON object, verbatim
    received_at: float | None = field(
        default=None, compare=False
    )  # stamped by the parser


type TelemetryEvent = HubInfo | Battery | Imu | Port | UnknownEvent
"""A parsed telemetry event — one hub-emitted JSON line (D7 terminology)."""


# Port per-device wire dictionary (D7): dev -> ((wire key, Python field, JSON
# type), ...) in canonical emission order — the single source of truth for
# both decode and encode. Known devices require every listed key: emission is
# unconditional per cycle, so a missing key is an agent bug surfaced as
# EventDecodeError (counted malformed, never guessed). "none" is the empty
# port (no keys); an unknown dev string maps to () — passthrough, no fields.
_PORT_SPECS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "Motor": (
        ("angle", "angle_deg", "number"),
        ("speed", "speed_dps", "number"),
        ("load", "load_mnm", "number"),
    ),
    "ColorSensor": (
        ("refl", "reflection_pct", "number"),
        ("amb", "ambient_pct", "number"),
        ("h", "hue_deg", "number"),
        ("s", "saturation_pct", "number"),
        ("v", "value_pct", "number"),
        ("col", "color", "string"),
    ),
    "ForceSensor": (
        ("f", "force_n", "number"),
        ("d", "distance_mm", "number"),
        ("pressed", "pressed", "boolean"),
    ),
    "UltrasonicSensor": (
        ("d", "distance_mm", "number"),
        ("pr", "presence", "boolean"),
    ),
    "ColorDistanceSensor": (
        ("d", "distance_pct", "number"),  # % — BOOST-era unit trap (D7)
        ("refl", "reflection_pct", "number"),
        ("amb", "ambient_pct", "number"),
        ("h", "hue_deg", "number"),
        ("s", "saturation_pct", "number"),
        ("v", "value_pct", "number"),
        ("col", "color", "string"),
    ),
    "TiltSensor": (
        ("pitch", "pitch_deg", "number"),
        ("roll", "roll_deg", "number"),
    ),
    "InfraredSensor": (("d", "distance_pct", "number"),),
    "none": (),
}


def _number(value: JsonValue, context: str) -> JsonNumber:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventDecodeError(
            f"{context}: expected a JSON number, got {type(value).__name__}"
        )
    return value


def _string(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        raise EventDecodeError(
            f"{context}: expected a JSON string, got {type(value).__name__}"
        )
    return value


def _boolean(value: JsonValue, context: str) -> bool:
    if not isinstance(value, bool):
        raise EventDecodeError(
            f"{context}: expected a JSON boolean, got {type(value).__name__}"
        )
    return value


type Validator = Callable[[JsonValue, str], JsonValue]
_VALIDATORS: dict[str, Validator] = {
    "number": _number,
    "string": _string,
    "boolean": _boolean,
}


def _take[T](
    obj: dict[str, JsonValue],
    key: str,
    validate: Callable[[JsonValue, str], T],
    context: str,
) -> T:
    """Fetch a required wire key and validate its JSON type.

    A missing key, or one carrying the wrong JSON type, raises
    :class:`EventDecodeError` — decoding never guesses or fills defaults.
    """
    if key not in obj:
        raise EventDecodeError(f"{context}: missing required key {key!r}")
    return validate(obj[key], f"{context}.{key}")


def _decode_hub_info(obj: dict[str, JsonValue]) -> HubInfo:
    return HubInfo(
        name=_take(obj, "name", _string, "hub_info"),
        firmware=_take(obj, "fw", _string, "hub_info"),
        model=_take(obj, "model", _string, "hub_info"),
    )


def _decode_battery(obj: dict[str, JsonValue]) -> Battery:
    return Battery(
        voltage_mv=_take(obj, "v", _number, "battery"),
        current_ma=_take(obj, "c", _number, "battery"),
        percent=_take(obj, "pct", _number, "battery"),
    )


def _decode_imu(obj: dict[str, JsonValue]) -> Imu:
    return Imu(
        accel=(
            _take(obj, "ax", _number, "imu"),
            _take(obj, "ay", _number, "imu"),
            _take(obj, "az", _number, "imu"),
        ),
        gyro=(
            _take(obj, "gx", _number, "imu"),
            _take(obj, "gy", _number, "imu"),
            _take(obj, "gz", _number, "imu"),
        ),
        up=_take(obj, "up", _string, "imu"),
    )


def _decode_port(obj: dict[str, JsonValue]) -> Port:
    p = _take(obj, "p", _string, "port")
    dev = _take(obj, "dev", _string, "port")
    fields: dict[str, JsonValue] = {}
    for wire_key, py_name, type_tag in _PORT_SPECS.get(dev, ()):
        fields[py_name] = _take(obj, wire_key, _VALIDATORS[type_tag], f"port {dev}")
    return Port(port=p, device=dev, **fields)


def decode(line: str | bytes) -> TelemetryEvent:
    """Decode one telemetry line into a typed event.

    Accepts the line with or without its trailing terminator (JSON whitespace
    is tolerated). Raises :class:`EventDecodeError` for wire-level failures —
    bad JSON/UTF-8, a non-object payload, a missing or non-string ``"t"``, or
    a known kind with missing or wrongly-typed required fields. Unknown kinds
    wrap as :class:`UnknownEvent`; unknown fields on known kinds are ignored.
    ``received_at`` is left ``None`` here — the parser stamps it.
    """
    try:
        obj = json.loads(line)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise EventDecodeError(f"not valid JSON or UTF-8: {exc}") from exc
    if not isinstance(obj, dict):
        raise EventDecodeError(f"expected a JSON object, got {type(obj).__name__}")
    kind = obj.get("t")
    if not isinstance(kind, str):
        raise EventDecodeError('missing or non-string "t" kind key')
    if kind == "hub_info":
        return _decode_hub_info(obj)
    if kind == "battery":
        return _decode_battery(obj)
    if kind == "imu":
        return _decode_imu(obj)
    if kind == "port":
        return _decode_port(obj)
    return UnknownEvent(kind=kind, data=obj)


def encode(event: TelemetryEvent) -> bytes:
    """Encode an event back to its CRLF-terminated wire line.

    Key order follows the field dictionary and separators are compact, so the
    canonical example lines round-trip byte-identically. ``received_at`` is
    server-side receipt metadata and is never emitted; an :class:`UnknownEvent`
    re-emits its verbatim parsed object.
    """
    if isinstance(event, HubInfo):
        obj: dict[str, JsonValue] = {
            "t": "hub_info",
            "name": event.name,
            "fw": event.firmware,
            "model": event.model,
        }
    elif isinstance(event, Battery):
        obj = {
            "t": "battery",
            "v": event.voltage_mv,
            "c": event.current_ma,
            "pct": event.percent,
        }
    elif isinstance(event, Imu):
        obj = {
            "t": "imu",
            "ax": event.accel[0],
            "ay": event.accel[1],
            "az": event.accel[2],
            "gx": event.gyro[0],
            "gy": event.gyro[1],
            "gz": event.gyro[2],
            "up": event.up,
        }
    elif isinstance(event, Port):
        obj = {"t": "port", "p": event.port, "dev": event.device}
        for wire_key, py_name, _type_tag in _PORT_SPECS.get(event.device, ()):
            value = getattr(event, py_name)
            if value is not None:
                obj[wire_key] = value
    elif isinstance(event, UnknownEvent):
        obj = event.data
    else:
        raise TypeError(f"not a telemetry event: {type(event).__name__}")
    return json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\r\n"
