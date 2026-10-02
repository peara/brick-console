"""Event model tests (issue #3): the D7 wire schema pinned as typed Python.

Covers the decode/encode contract: happy path per kind, UnknownEvent
verbatim, unknown-device passthrough, unknown-fields-ignored, and round-trip
(semantic for arbitrary lines, byte-exact for the canonical examples).
"""

from __future__ import annotations

import pytest

from brick_console.events import (
    Battery,
    EventDecodeError,
    HubInfo,
    Imu,
    Port,
    UnknownEvent,
    decode,
    encode,
)

# The D7 canonical example lines, exactly as in the events.py docstring,
# minus their CRLF terminators. encode() re-adds the terminator, so the
# round-trip assertion compares encode(decode(line)) against line + CRLF.
# Idle-agent lines first (D9: the agent emits only the passive subset),
# then the program-mode lines the parser already accepts (M2 opt-in).
CANONICAL = [
    b'{"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}',
    b'{"t":"battery","v":8085,"c":42,"pct":87}',
    b'{"t":"imu","ax":120,"ay":-980,"az":9810,"gx":0,"gy":0,"gz":3,"up":"top"}',
    b'{"t":"port","p":"A","dev":"Motor","angle":12,"speed":0,"load":0}',
    b'{"t":"port","p":"B","dev":"ColorSensor","mode":"ambient","amb":12}',
    b'{"t":"port","p":"C","dev":"ForceSensor","f":0.0,"d":0.0,"pressed":false}',
    b'{"t":"port","p":"D","dev":"UltrasonicSensor","mode":"presence","pr":false}',
    b'{"t":"port","p":"E","dev":"TiltSensor","pitch":3,"roll":-2}',
    b'{"t":"port","p":"F","dev":"none"}',
    b'{"t":"port","p":"B","dev":"ColorSensor","mode":"surface","refl":34,"h":10,"s":80,"v":90,"col":"red"}',
    b'{"t":"port","p":"D","dev":"UltrasonicSensor","mode":"distance","d":245}',
]


def test_hub_info_happy_path() -> None:
    e = decode(
        b'{"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}'
    )
    assert isinstance(e, HubInfo)
    assert e.name == "Pybricks Hub"
    assert e.firmware == "4.0.1"
    assert e.model == "technichub"


def test_battery_happy_path() -> None:
    e = decode(b'{"t":"battery","v":8085,"c":42,"pct":87}')
    assert isinstance(e, Battery)
    assert e.voltage_mv == 8085
    assert e.current_ma == 42
    assert e.percent == 87


def test_battery_full_word_fields() -> None:
    # Short wire keys map to full-word Python names (D7).
    e = decode(b'{"t":"battery","v":1,"c":2,"pct":3}')
    assert e.voltage_mv == 1
    assert e.current_ma == 2
    assert e.percent == 3


def test_imu_happy_path() -> None:
    e = decode(
        b'{"t":"imu","ax":120,"ay":-980,"az":9810,"gx":0,"gy":0,"gz":3,"up":"top"}'
    )
    assert isinstance(e, Imu)
    assert e.accel == (120, -980, 9810)
    assert e.gyro == (0, 0, 3)
    assert e.up == "top"


def test_port_motor() -> None:
    e = decode(b'{"t":"port","p":"A","dev":"Motor","angle":12,"speed":0,"load":0}')
    assert isinstance(e, Port)
    assert e.port == "A"
    assert e.device == "Motor"
    assert e.angle_deg == 12
    assert e.speed_dps == 0
    assert e.load_mnm == 0


def test_port_color_sensor_ambient() -> None:
    # The idle agent's ColorSensor line (D9): light off, ambient only.
    e = decode(b'{"t":"port","p":"B","dev":"ColorSensor","mode":"ambient","amb":12}')
    assert isinstance(e, Port)
    assert e.port == "B"
    assert e.device == "ColorSensor"
    assert e.mode == "ambient"
    assert e.ambient_pct == 12
    # The ambient spec extracts nothing else — a stray refl key is ignored
    # per D7's addition versioning, never decoded into a typed field.
    assert e.reflection_pct is None


def test_port_color_sensor_surface() -> None:
    # A program-mode line (M2 opt-in): the program owns the sensor, its
    # light is on, and it publishes the surface reading under the tag.
    e = decode(
        b'{"t":"port","p":"B","dev":"ColorSensor","mode":"surface","refl":34,"h":10,"s":80,"v":90,"col":"red"}'
    )
    assert isinstance(e, Port)
    assert e.mode == "surface"
    assert e.reflection_pct == 34
    assert e.hue_deg == 10
    assert e.saturation_pct == 80
    assert e.value_pct == 90
    assert e.color == "red"
    assert e.ambient_pct is None


def test_port_color_sensor_untagged_is_malformed() -> None:
    # A mode-dependent device must tag its line (D9): untagged, the fields
    # cannot say which reading they belong to — decode never guesses.
    with pytest.raises(EventDecodeError):
        decode(
            b'{"t":"port","p":"B","dev":"ColorSensor","refl":34,"amb":12,"h":10,"s":80,"v":90,"col":"red"}'
        )


def test_unknown_device_mode_pair_passthrough() -> None:
    # Unknown (dev, mode) pairs pass through like unknown dev strings: the
    # tag is kept, no typed field is decoded, the raw line stays the record.
    e = decode(b'{"t":"port","p":"B","dev":"ColorSensor","mode":"weird","x":42}')
    assert isinstance(e, Port)
    assert e.device == "ColorSensor"
    assert e.mode == "weird"
    assert e.ambient_pct is None


def test_port_ultrasonic_presence_and_distance() -> None:
    # Idle agent: presence (transmitter off). Program mode: distance (ping).
    pr = decode(
        b'{"t":"port","p":"D","dev":"UltrasonicSensor","mode":"presence","pr":false}'
    )
    assert isinstance(pr, Port)
    assert pr.mode == "presence"
    assert pr.presence is False
    assert pr.distance_mm is None
    d = decode(
        b'{"t":"port","p":"D","dev":"UltrasonicSensor","mode":"distance","d":245}'
    )
    assert isinstance(d, Port)
    assert d.mode == "distance"
    assert d.distance_mm == 245
    assert d.presence is None


def test_port_force_sensor_preserves_float() -> None:
    e = decode(
        b'{"t":"port","p":"C","dev":"ForceSensor","f":0.0,"d":0.0,"pressed":false}'
    )
    assert e.force_n == 0.0
    assert e.distance_mm == 0.0
    assert e.pressed is False


def test_port_none_has_no_fields() -> None:
    e = decode(b'{"t":"port","p":"F","dev":"none"}')
    assert e.port == "F"
    assert e.device == "none"
    # No per-device fields set.
    assert e.angle_deg is None
    assert e.color is None


def test_unknown_kind_is_unknown_event_verbatim() -> None:
    line = b'{"t":"future_kind","x":1,"nested":{"y":[1,2]}}'
    e = decode(line)
    assert isinstance(e, UnknownEvent)
    assert e.kind == "future_kind"
    assert e.data == {"t": "future_kind", "x": 1, "nested": {"y": [1, 2]}}


def test_unknown_device_passthrough() -> None:
    e = decode(b'{"t":"port","p":"A","dev":"SuperSensor9000","magic":42}')
    assert isinstance(e, Port)
    assert e.device == "SuperSensor9000"  # passthrough, never an error
    assert e.port == "A"
    # Unmapped keys are ignored like unknown fields; no typed field is set.
    assert e.angle_deg is None


def test_unknown_fields_on_known_kind_ignored() -> None:
    e = decode(b'{"t":"battery","v":8085,"c":42,"pct":87,"zzz":1,"future":true}')
    assert isinstance(e, Battery)
    assert e.voltage_mv == 8085
    assert e.current_ma == 42
    assert e.percent == 87
    # The unknown fields simply do not exist on the typed event.
    assert not hasattr(e, "zzz")


def test_decode_accepts_str() -> None:
    e = decode('{"t":"battery","v":1,"c":2,"pct":3}')
    assert isinstance(e, Battery)


def test_decode_rejects_non_object() -> None:
    with pytest.raises(EventDecodeError):
        decode(b"[1,2,3]")


def test_decode_rejects_missing_kind() -> None:
    with pytest.raises(EventDecodeError):
        decode(b'{"v":8085}')


def test_decode_rejects_non_string_kind() -> None:
    with pytest.raises(EventDecodeError):
        decode(b'{"t":42}')


def test_decode_rejects_invalid_json() -> None:
    with pytest.raises(EventDecodeError):
        decode(b"not json")


def test_decode_rejects_missing_required_field() -> None:
    with pytest.raises(EventDecodeError):
        decode(b'{"t":"battery","v":8085,"c":42}')  # missing pct


def test_decode_rejects_wrong_type() -> None:
    with pytest.raises(EventDecodeError):
        decode(b'{"t":"battery","v":"high","c":42,"pct":87}')


def test_decode_rejects_bool_where_number_expected() -> None:
    # bool is a subclass of int; must be rejected where a number is required.
    with pytest.raises(EventDecodeError):
        decode(b'{"t":"battery","v":true,"c":42,"pct":87}')


@pytest.mark.parametrize("line", CANONICAL)
def test_canonical_round_trip_byte_exact(line: bytes) -> None:
    assert encode(decode(line)) == line + b"\r\n"


def test_round_trip_semantic_arbitrary() -> None:
    # decode(encode(event)) == event for every kind, with arbitrary values.
    events = [
        HubInfo(name="MyHub", firmware="4.0.1", model="technichub"),
        Battery(voltage_mv=7500, current_ma=10, percent=50),
        Imu(accel=(1, 2, 3), gyro=(4.5, 5, 6), up="bottom"),
        Port(
            port="B",
            device="ColorSensor",
            mode="surface",
            reflection_pct=1,
            hue_deg=3,
            saturation_pct=4,
            value_pct=5,
            color="blue",
        ),
        Port(port="C", device="ForceSensor", force_n=1.5, distance_mm=2, pressed=True),
        UnknownEvent(kind="future", data={"t": "future", "a": [1, 2]}),
    ]
    for event in events:
        assert decode(encode(event)) == event


def test_received_at_excluded_from_equality() -> None:
    a = decode(b'{"t":"battery","v":8085,"c":42,"pct":87}')
    b = decode(b'{"t":"battery","v":8085,"c":42,"pct":87}')
    assert a == b  # both have received_at=None; content-equal regardless


def test_encode_ends_with_crlf() -> None:
    assert encode(Battery(voltage_mv=1, current_ma=2, percent=3)).endswith(b"\r\n")


def test_unknown_event_round_trips_verbatim() -> None:
    line = b'{"t":"future_kind","x":1,"nested":{"y":[1,2]}}'
    assert encode(decode(line)) == line + b"\r\n"
