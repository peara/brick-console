"""Hub-agent tests (testing.md hub-agent tier): the D7+D9 wire from the
hub side, pinned as the library/wrapper split.

The passive library (``agent/brick_telemetry.py``) is imported directly
(conftest puts ``agent/`` on ``sys.path`` — import-safe by design). The
wrapper (``agent/agent_main.py``) runs ``main()`` at module top on the
hub (the entry shape the first soak proved), so host tests exec its
source with that final call stripped and drive ``main`` with fakes.

Everything is checked against the server model
(:mod:`brick_console.events`): built lines must decode through the
server's ``decode()`` and re-encode byte-exactly. The D9 actuation ban
is executable here: stub call-counting asserts the idle agent never
calls an active read (``reflection``/``hsv``/``color`` on a color
sensor, ``distance`` on an ultrasonic) and never probes an
InfraredSensor. The ``pybricks`` package is the host stub registered by
``tests/conftest.py``; nothing here touches hardware.
"""

from __future__ import annotations

from pathlib import Path

import brick_telemetry as bt
import pybricks.hubs as stub_hubs
import pybricks.pupdevices as stub_pup
import pytest

from brick_console.events import Battery, HubInfo, Imu, Port, decode, encode

_AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"

# D9's actuation ban is per device: UltrasonicSensor's `d` read is an
# active ping (DISTL) and ColorSensor's refl/hsv/col reads keep its
# light on (RGB_I) — but ForceSensor's `d` is passive plunger travel
# (FRAW): the device, not the read's name, is the discriminator. Keys
# are the stub's READS-dict attr names, which CALLS records.
ACTIVE_ATTRS_BY_DEVICE = {
    "ColorSensor": ("refl", "hsv", "col"),
    "UltrasonicSensor": ("d",),
}


def assert_no_active_reads():
    for port, attr in stub_pup.CALLS:
        device = stub_pup.ATTACHED.get(port)
        forbidden = ACTIVE_ATTRS_BY_DEVICE.get(device, ())
        assert attr not in forbidden, f"active read {attr} on {device} port {port}"


@pytest.fixture(autouse=True)
def _clean_stubs():
    stub_pup.reset()
    stub_hubs.CONSTRUCTED.clear()


def _load_wrapper():
    """Exec the wrapper with its module-top ``main()`` entry stripped.

    On the hub that call is the program (proven by the first soak); on
    the host it would loop forever. Stripping only that line leaves every
    policy function testable against the real library import, exactly
    as on-hub.
    """
    src = (_AGENT_DIR / "agent_main.py").read_text()
    lines = src.rstrip().splitlines()
    assert lines[-1] == "main()", "wrapper must end with its entry call"
    ns = {"__name__": "agent_main_test"}
    exec("\n".join(lines[:-1]), ns)  # noqa: S102 - exec of repo-local source
    return ns


def _attach_kit():
    """The 51515 kit: Motor A, ColorSensor C, UltrasonicSensor E."""
    from pybricks import parameters

    stub_pup.ATTACHED[parameters.Port.A] = "Motor"
    stub_pup.READS[parameters.Port.A] = {"angle": 12, "speed": 0, "load": 0}
    stub_pup.ATTACHED[parameters.Port.C] = "ColorSensor"
    stub_pup.READS[parameters.Port.C] = {"amb": 12}
    stub_pup.ATTACHED[parameters.Port.E] = "UltrasonicSensor"
    stub_pup.READS[parameters.Port.E] = {"d": 245, "pr": False}
    return parameters


def _run_wrapper(ns, cycles, hub=None):
    """Drive wrapper ``main`` for ``cycles`` ticks, collecting lines."""
    lines = []
    ticks = {"n": 0}

    class _Watch:
        def __init__(self):
            self.now = 0

        def time(self):
            return self.now

    watch = _Watch()

    def fake_wait(ms):
        ticks["n"] += 1
        watch.now += ms
        if ticks["n"] >= cycles:
            raise KeyboardInterrupt  # the only exit — the loop never ends

    ns["InventorHub"] = hub if hub is not None else stub_hubs.InventorHub
    ns["StopWatch"] = lambda: watch
    ns["wait"] = fake_wait
    with pytest.raises(KeyboardInterrupt):
        ns["main"](out=lines.append)
    return lines


# ---------------------------------------------------------------------------
# Library: import safety and the split itself
# ---------------------------------------------------------------------------


class TestLibrarySplit:
    def test_import_does_no_hardware_io(self) -> None:
        # Reload AFTER the fixture's stub reset: import-time hardware I/O
        # would append to the stubs' construction logs where this sees it.
        # (Asserting on the module-top import alone is vacuous — the
        # autouse reset clears the logs before any test body runs.)
        import importlib

        importlib.reload(bt)
        assert stub_hubs.CONSTRUCTED == []
        assert stub_pup.CONSTRUCTED == []

    def test_library_is_policy_free(self) -> None:
        # D9's split, pinned: no run()/loop in the library, and no
        # builders for active readings (surface color, ultrasonic
        # distance) — those arrive with M2 (#36) when programs need them.
        assert not hasattr(bt, "run")
        assert not hasattr(bt, "color_surface_line")
        assert not hasattr(bt, "ultrasonic_distance_line")


# ---------------------------------------------------------------------------
# battery_pct — the D7 derivation curve
# ---------------------------------------------------------------------------


class TestBatteryPct:
    def test_canonical_point(self) -> None:
        assert bt.battery_pct(8085) == 87  # the D7 canonical example

    @pytest.mark.parametrize(
        ("mv", "pct"),
        [
            (8400, 100),
            (8450, 100),
            (8200, 92),
            (8000, 84),
            (7800, 75),
            (7500, 60),
            (7200, 47),
            (6900, 34),
            (6600, 22),
            (6300, 10),
            (6000, 0),
            (5900, 0),
            (6301, 10),
            (7999, 83),
        ],
    )
    def test_curve_monotonic_and_clamped(self, mv, pct) -> None:
        assert bt.battery_pct(mv) == pct


# ---------------------------------------------------------------------------
# Line builders — byte-identical to the server reference
# ---------------------------------------------------------------------------


class TestBuilders:
    def test_hub_info_line(self) -> None:
        line = bt.hub_info_line(stub_hubs.InventorHub())
        event = decode(line)
        assert isinstance(event, HubInfo)
        assert event.name == "Pybricks Hub"
        assert event.firmware == "4.0.1"
        assert event.model == "technichub"
        assert encode(event) == (
            b'{"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}'
            b"\r\n"
        )

    def test_battery_line(self) -> None:
        stub_hubs.BATTERY.update(v=8085, c=42)
        line = bt.battery_line(stub_hubs.InventorHub())
        event = decode(line)
        assert isinstance(event, Battery)
        assert (event.voltage_mv, event.current_ma, event.percent) == (8085, 42, 87)
        assert encode(event) == b'{"t":"battery","v":8085,"c":42,"pct":87}\r\n'

    def test_imu_line(self) -> None:
        from pybricks import parameters

        stub_hubs.IMU.update(
            acc=(120, -980, 9810), gyro=(0, 0, 3), up=parameters.Side.TOP
        )
        line = bt.imu_line(stub_hubs.InventorHub())
        event = decode(line)
        assert isinstance(event, Imu)
        assert event.accel == (120, -980, 9810)
        assert event.up == "top"
        assert encode(event) == (
            b'{"t":"imu","ax":120,"ay":-980,"az":9810,"gx":0,"gy":0,"gz":3,"up":"top"}'
            b"\r\n"
        )

    def test_motor_line(self) -> None:
        parameters = _attach_kit()
        line = bt.motor_line("A", stub_pup.Motor(parameters.Port.A))
        event = decode(line)
        assert isinstance(event, Port)
        assert event.mode is None  # single mode: no tag (D9)
        assert (event.angle_deg, event.speed_dps, event.load_mnm) == (12, 0, 0)
        assert encode(event) == (
            b'{"t":"port","p":"A","dev":"Motor","angle":12,"speed":0,"load":0}\r\n'
        )

    def test_color_ambient_line(self) -> None:
        parameters = _attach_kit()
        line = bt.color_ambient_line("C", stub_pup.ColorSensor(parameters.Port.C))
        event = decode(line)
        assert isinstance(event, Port)
        assert event.mode == "ambient"
        assert event.ambient_pct == 12
        assert encode(event) == (
            b'{"t":"port","p":"C","dev":"ColorSensor","mode":"ambient","amb":12}\r\n'
        )

    def test_ultrasonic_presence_line(self) -> None:
        parameters = _attach_kit()
        line = bt.ultrasonic_presence_line(
            "E", stub_pup.UltrasonicSensor(parameters.Port.E)
        )
        event = decode(line)
        assert isinstance(event, Port)
        assert event.mode == "presence"
        assert event.presence is False
        assert encode(event) == (
            b'{"t":"port","p":"E","dev":"UltrasonicSensor","mode":"presence","pr":false}\r\n'
        )

    def test_force_line_keeps_float_repr(self) -> None:
        from pybricks import parameters

        stub_pup.ATTACHED[parameters.Port.A] = "ForceSensor"
        stub_pup.READS[parameters.Port.A] = {"f": 1.5, "d": 2.0, "pressed": True}
        line = bt.force_line("A", stub_pup.ForceSensor(parameters.Port.A))
        event = decode(line)
        assert isinstance(event, Port)
        assert event.force_n == 1.5
        assert encode(event) == (
            b'{"t":"port","p":"A","dev":"ForceSensor","f":1.5,"d":2.0,"pressed":true}\r\n'
        )

    def test_tilt_line(self) -> None:
        from pybricks import parameters

        stub_pup.ATTACHED[parameters.Port.A] = "TiltSensor"
        stub_pup.READS[parameters.Port.A] = {"tilt": (3, -2)}
        line = bt.tilt_line("A", stub_pup.TiltSensor(parameters.Port.A))
        event = decode(line)
        assert isinstance(event, Port)
        assert (event.pitch_deg, event.roll_deg) == (3, -2)
        assert encode(event) == (
            b'{"t":"port","p":"A","dev":"TiltSensor","pitch":3,"roll":-2}\r\n'
        )

    def test_color_distance_ambient_line(self) -> None:
        from pybricks import parameters

        stub_pup.ATTACHED[parameters.Port.B] = "ColorDistanceSensor"
        stub_pup.READS[parameters.Port.B] = {"amb": 7, "d": 42}
        line = bt.color_distance_ambient_line(
            "B", stub_pup.ColorDistanceSensor(parameters.Port.B)
        )
        event = decode(line)
        assert isinstance(event, Port)
        assert event.mode == "ambient"
        assert event.ambient_pct == 7
        assert encode(event) == (
            b'{"t":"port","p":"B","dev":"ColorDistanceSensor","mode":"ambient","amb":7}\r\n'
        )

    def test_none_line(self) -> None:
        event = decode(bt.none_line("F"))
        assert isinstance(event, Port)
        assert event.device == "none"
        assert encode(event) == b'{"t":"port","p":"F","dev":"none"}\r\n'


# ---------------------------------------------------------------------------
# probe — discovery, ladder order, and the InfraredSensor exclusion
# ---------------------------------------------------------------------------


class TestProbe:
    def test_finds_the_attached_device_per_port(self) -> None:
        parameters = _attach_kit()
        assert bt.probe(parameters.Port.A)[0] == "Motor"
        assert bt.probe(parameters.Port.C)[0] == "ColorSensor"
        assert bt.probe(parameters.Port.E)[0] == "UltrasonicSensor"

    def test_empty_port_returns_none(self) -> None:
        parameters = _attach_kit()
        assert bt.probe(parameters.Port.F) is None

    def test_infrared_sensor_is_not_probed(self) -> None:
        # D9: an active IR emitter (LEGO-documented, 7 kHz pulsed) has no
        # passive mode — the idle agent neither detects nor reads it.
        parameters = _attach_kit()
        stub_pup.ATTACHED[parameters.Port.F] = "InfraredSensor"
        stub_pup.READS[parameters.Port.F] = {"d": 50}
        assert bt.probe(parameters.Port.F) is None
        assert all(name != "InfraredSensor" for name, _ in stub_pup.CONSTRUCTED)


# ---------------------------------------------------------------------------
# The actuation ban — D9's principle as an executable test
# ---------------------------------------------------------------------------


class TestActuationBan:
    def test_library_never_performs_active_reads(self) -> None:
        # Every builder against the full kit: only passive attrs touched.
        parameters = _attach_kit()
        hub = stub_hubs.InventorHub()
        bt.hub_info_line(hub)
        bt.battery_line(hub)
        bt.imu_line(hub)
        bt.port_line("Motor", "A", stub_pup.Motor(parameters.Port.A))
        bt.port_line("ColorSensor", "C", stub_pup.ColorSensor(parameters.Port.C))
        bt.port_line(
            "UltrasonicSensor", "E", stub_pup.UltrasonicSensor(parameters.Port.E)
        )
        assert_no_active_reads()

    def test_wrapper_never_performs_active_reads(self) -> None:
        parameters = _attach_kit()
        # A ForceSensor on D pins the per-device nuance: its passive `d`
        # read (plunger travel, FRAW) must not trip the ban that the
        # UltrasonicSensor's `d` (an active ping) does.
        stub_pup.ATTACHED[parameters.Port.D] = "ForceSensor"
        stub_pup.READS[parameters.Port.D] = {"f": 1.5, "d": 2.0, "pressed": False}
        ns = _load_wrapper()
        _run_wrapper(ns, cycles=30)
        assert_no_active_reads()
        # The passive reads DID happen — every cycle, resting mode only.
        assert stub_pup.CALLS[(parameters.Port.C, "amb")] == 30
        assert stub_pup.CALLS[(parameters.Port.E, "pr")] == 30
        assert stub_pup.CALLS[(parameters.Port.D, "d")] == 30


# ---------------------------------------------------------------------------
# The wrapper's policy — cadence, tags on the wire, pacing
# ---------------------------------------------------------------------------


class TestWrapperLoop:
    def test_structure_and_cadence_over_30_cycles(self) -> None:
        _attach_kit()
        ns = _load_wrapper()
        lines = _run_wrapper(ns, cycles=30)

        assert lines[0] == (
            '{"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}'
        )
        assert sum('"t":"hub_info"' in ln for ln in lines) == 1  # snapshot once
        assert sum('"t":"battery"' in ln for ln in lines) == 3  # cycles 0, 10, 20
        assert sum('"t":"imu"' in ln for ln in lines) == 30  # every cycle
        motor = [ln for ln in lines if '"dev":"Motor"' in ln]
        ambient = [ln for ln in lines if '"dev":"ColorSensor"' in ln]
        presence = [ln for ln in lines if '"dev":"UltrasonicSensor"' in ln]
        assert len(motor) == 30 and len(ambient) == 30 and len(presence) == 30
        # No active fields ever on the agent-mode wire (D9).
        assert not any('"refl"' in ln for ln in lines)
        assert not any('"col"' in ln for ln in lines)
        assert not any(
            '"dev":"UltrasonicSensor","mode":"distance"' in ln for ln in lines
        )

    def test_wire_lines_decode_and_reencode_byte_exact(self) -> None:
        _attach_kit()
        ns = _load_wrapper()
        lines = _run_wrapper(ns, cycles=3)
        for ln in lines:
            assert encode(decode(ln)) == ln.encode() + b"\r\n"

    def test_mode_tags_on_the_wire(self) -> None:
        _attach_kit()
        ns = _load_wrapper()
        lines = _run_wrapper(ns, cycles=2)
        ambient = next(ln for ln in lines if '"dev":"ColorSensor"' in ln)
        presence = next(ln for ln in lines if '"dev":"UltrasonicSensor"' in ln)
        assert (
            ambient
            == '{"t":"port","p":"C","dev":"ColorSensor","mode":"ambient","amb":12}'
        )
        assert (
            presence
            == '{"t":"port","p":"E","dev":"UltrasonicSensor","mode":"presence","pr":false}'
        )

    def test_pacing_sleeps_to_the_next_100ms_boundary(self) -> None:
        # Drift-free pacing: wait(100 - elapsed % 100) — a slow cycle
        # shortens the next sleep to the boundary, never a blind 100.
        _attach_kit()
        ns = _load_wrapper()

        waits = []

        class _Watch:
            def __init__(self):
                self.now = 0

            def time(self):
                return self.now

        watch = _Watch()

        def fake_wait(ms):
            waits.append(ms)
            watch.now += ms
            watch.now += 30  # each cycle's work eats into the next budget
            if len(waits) >= 3:
                raise KeyboardInterrupt

        ns["InventorHub"] = stub_hubs.InventorHub
        ns["StopWatch"] = lambda: watch
        ns["wait"] = fake_wait
        with pytest.raises(KeyboardInterrupt):
            ns["main"](out=lambda _ln: None)
        assert waits == [100, 70, 70]


# ---------------------------------------------------------------------------
# Detach / replug / hot-plug — the D7 transition discipline
# ---------------------------------------------------------------------------


class TestDetachTransitions:
    def _wrapper_with_kit(self):
        parameters = _attach_kit()
        ns = _load_wrapper()
        return parameters, ns

    def test_detach_emits_none_once_and_replug_recovers(self) -> None:
        parameters, ns = self._wrapper_with_kit()
        lines = []
        ticks = {"n": 0}

        class _Watch:
            now = 0

            def time(self):
                return self.now

        watch = _Watch()

        def fake_wait(ms):
            ticks["n"] += 1
            watch.now += ms
            if ticks["n"] == 3:  # unplug the color sensor mid-run
                stub_pup.READ_ERRORS.add(parameters.Port.C)
                del stub_pup.ATTACHED[parameters.Port.C]
            if ticks["n"] == 6:  # plug it back in
                stub_pup.READ_ERRORS.clear()
                stub_pup.ATTACHED[parameters.Port.C] = "ColorSensor"
            if ticks["n"] >= 8:
                raise KeyboardInterrupt

        ns["InventorHub"] = stub_hubs.InventorHub
        ns["StopWatch"] = lambda: watch
        ns["wait"] = fake_wait
        with pytest.raises(KeyboardInterrupt):
            ns["main"](out=lines.append)

        none_lines = [ln for ln in lines if '"dev":"none"' in ln]
        assert none_lines == ['{"t":"port","p":"C","dev":"none"}']  # once, D7
        ambient_after = [ln for ln in lines if '"dev":"ColorSensor"' in ln]
        assert len(ambient_after) >= 2  # before detach and after replug
        assert lines[-1] != '{"t":"port","p":"C","dev":"none"}'  # recovered

    def test_hot_plug_is_picked_up_by_reprobe(self) -> None:
        parameters = _attach_kit()  # Port.F starts empty (not in the kit)
        ns = _load_wrapper()
        lines = []
        ticks = {"n": 0}

        class _Watch:
            now = 0

            def time(self):
                return self.now

        watch = _Watch()

        def fake_wait(ms):
            ticks["n"] += 1
            watch.now += ms
            if ticks["n"] == 3:  # hot-plug a tilt sensor mid-run
                stub_pup.ATTACHED[parameters.Port.F] = "TiltSensor"
                stub_pup.READS[parameters.Port.F] = {"tilt": (3, -2)}
            if ticks["n"] >= 5:
                raise KeyboardInterrupt

        ns["InventorHub"] = stub_hubs.InventorHub
        ns["StopWatch"] = lambda: watch
        ns["wait"] = fake_wait
        with pytest.raises(KeyboardInterrupt):
            ns["main"](out=lines.append)

        tilt_lines = [ln for ln in lines if '"dev":"TiltSensor"' in ln]
        assert tilt_lines  # re-probing found it once attached
        assert (
            tilt_lines[0]
            == '{"t":"port","p":"F","dev":"TiltSensor","pitch":3,"roll":-2}'
        )

    def test_infrared_port_stays_invisible(self) -> None:
        # A plugged IR sensor is not detected (D9) — no line for port F.
        parameters = _attach_kit()
        stub_pup.ATTACHED[parameters.Port.F] = "InfraredSensor"
        stub_pup.READS[parameters.Port.F] = {"d": 50}
        ns = _load_wrapper()
        lines = _run_wrapper(ns, cycles=4)
        assert not any('"dev":"InfraredSensor"' in ln for ln in lines)
        assert not any('"p":"F"' in ln for ln in lines)
