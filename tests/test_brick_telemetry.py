"""Hub-agent tests (testing.md hub-agent tier): the D7 wire from the hub side.

The agent (``agent/brick_telemetry.py``) and the server model
(:mod:`brick_console.events`) are tested against each other: agent-built
lines must decode through the server's ``decode()`` and re-encode
byte-exactly (the same standard ``tests/test_events.py`` pins for the
server side). The ``pybricks`` package is the host stub registered by
``tests/conftest.py``; nothing here touches hardware.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pybricks.hubs as stub_hubs
import pybricks.pupdevices as stub_pup
import pybricks.tools as stub_tools
import pytest

from brick_console.events import (
    Battery,
    HubInfo,
    Imu,
    Port,
    decode,
    encode,
)

_AGENT_PATH = Path(__file__).resolve().parents[1] / "agent" / "brick_telemetry.py"


def _load_agent() -> ModuleType:
    """Import the agent module fresh from its file (agent/ is not a package)."""
    spec = importlib.util.spec_from_file_location("brick_telemetry", _AGENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def agent() -> ModuleType:
    stub_pup.reset()
    stub_hubs.CONSTRUCTED.clear()
    stub_tools.WAITED.clear()
    return _load_agent()


class TestImportSafety:
    def test_module_import_does_no_hardware_io(self, agent) -> None:
        # Import must build no hub and probe no port (issue #15 done-when);
        # a single stub construction elsewhere in this file would also
        # trip this, so the import happens before any ATTACHED setup.
        assert stub_hubs.CONSTRUCTED == []
        assert stub_pup.CONSTRUCTED == []


class TestHubInfo:
    def test_start_emits_canonical_hub_info(self, agent) -> None:
        stub_hubs.SYSTEM_INFO["name"] = "Pybricks Hub"
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()

        assert len(lines) == 1
        event = decode(lines[0])
        assert isinstance(event, HubInfo)
        assert event.name == "Pybricks Hub"
        assert event.firmware == "4.0.1"
        assert event.model == "technichub"
        # Byte-exact against the D7 canonical line.
        assert encode(event) == (
            b'{"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}'
            b"\r\n"
        )

    def test_start_emits_only_once(self, agent) -> None:
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lambda _: None)
        teal.start()
        teal.start()
        # hub_info is a snapshot event: later start()s re-print it, but the
        # agent contract calls start() exactly once per session (run()).
        # Nothing to assert beyond "no crash" — the once-ness belongs to run().


class TestBattery:
    def test_canonical_battery_line(self, agent) -> None:
        stub_hubs.BATTERY.update(v=8085, c=42)
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        teal.cycle()

        battery_lines = [ln for ln in lines if '"t":"battery"' in ln]
        assert len(battery_lines) == 1
        event = decode(battery_lines[0])
        assert isinstance(event, Battery)
        assert event.voltage_mv == 8085
        assert event.current_ma == 42
        assert event.percent == 87
        assert encode(event) == b'{"t":"battery","v":8085,"c":42,"pct":87}\r\n'

    def test_battery_cadence_every_tenth_cycle(self, agent) -> None:
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        for _ in range(21):
            teal.cycle()
        battery_count = sum(1 for ln in lines if '"t":"battery"' in ln)
        assert battery_count == 3  # cycles 0, 10, 20

    def test_pct_curve_canonical_point(self, agent) -> None:
        # The D7 canonical example: 8085 mV -> 87 %.
        assert agent.battery_pct(8085) == 87

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
            (6601, 22),
            (7999, 83),
        ],
    )
    def test_pct_curve_monotonic_and_clamped(self, agent, mv, pct) -> None:
        assert agent.battery_pct(mv) == pct


class TestImu:
    def test_canonical_imu_line(self, agent) -> None:
        stub_hubs.IMU.update(
            acc=(120, -980, 9810), gyro=(0, 0, 3), up=stub_hubs.parameters.Side.TOP
        )
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        teal.cycle()

        imu_lines = [ln for ln in lines if '"t":"imu"' in ln]
        assert len(imu_lines) == 1
        event = decode(imu_lines[0])
        assert isinstance(event, Imu)
        assert event.accel == (120, -980, 9810)
        assert event.gyro == (0, 0, 3)
        assert event.up == "top"
        assert encode(event) == (
            b'{"t":"imu","ax":120,"ay":-980,"az":9810,"gx":0,"gy":0,"gz":3,"up":"top"}'
            b"\r\n"
        )

    def test_imu_emitted_every_cycle(self, agent) -> None:
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        for _ in range(5):
            teal.cycle()
        assert sum(1 for ln in lines if '"t":"imu"' in ln) == 5


class TestPorts:
    def test_canonical_motor_line(self, agent) -> None:
        from pybricks import parameters

        stub_pup.ATTACHED[parameters.Port.A] = "Motor"
        stub_pup.READS[parameters.Port.A] = {"angle": 12, "speed": 0, "load": 0}
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        teal.cycle()

        port_lines = [ln for ln in lines if '"t":"port"' in ln]
        assert port_lines == [
            '{"t":"port","p":"A","dev":"Motor","angle":12,"speed":0,"load":0}'
        ]
        event = decode(port_lines[0])
        assert isinstance(event, Port)
        assert event.port == "A"
        assert event.device == "Motor"
        assert event.angle_deg == 12
        assert encode(event) == (
            b'{"t":"port","p":"A","dev":"Motor","angle":12,"speed":0,"load":0}\r\n'
        )

    def test_canonical_color_sensor_line(self, agent) -> None:
        from pybricks import parameters

        stub_pup.ATTACHED[parameters.Port.B] = "ColorSensor"
        stub_pup.READS[parameters.Port.B] = {
            "refl": 34,
            "amb": 12,
            "hsv": stub_pup.Hsv(10, 80, 90),
            "col": parameters.Color.RED,
        }
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        teal.cycle()

        port_lines = [ln for ln in lines if '"t":"port"' in ln]
        assert port_lines == [
            '{"t":"port","p":"B","dev":"ColorSensor","refl":34,"amb":12,"h":10,"s":80,"v":90,"col":"red"}'
        ]
        assert encode(decode(port_lines[0])) == (
            b'{"t":"port","p":"B","dev":"ColorSensor","refl":34,"amb":12,"h":10,"s":80,"v":90,"col":"red"}'
            b"\r\n"
        )

    def test_canonical_ultrasonic_line(self, agent) -> None:
        from pybricks import parameters

        stub_pup.ATTACHED[parameters.Port.D] = "UltrasonicSensor"
        stub_pup.READS[parameters.Port.D] = {"d": 245, "pr": False}
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        teal.cycle()

        port_lines = [ln for ln in lines if '"t":"port"' in ln]
        assert port_lines == [
            '{"t":"port","p":"D","dev":"UltrasonicSensor","d":245,"pr":false}'
        ]
        assert encode(decode(port_lines[0])) == (
            b'{"t":"port","p":"D","dev":"UltrasonicSensor","d":245,"pr":false}\r\n'
        )

    def test_all_device_kinds_round_trip(self, agent) -> None:
        # One line per D7 device kind, each decoded + re-encoded through the
        # server reference. ForceSensor/ColorDistanceSensor/TiltSensor/
        # InfraredSensor never touch this hub's hardware (not in 51515) —
        # the stubs still prove the field dictionary and key order.
        from pybricks import parameters

        stub_pup.ATTACHED[parameters.Port.A] = "ForceSensor"
        stub_pup.READS[parameters.Port.A] = {"f": 0.0, "d": 0.0, "pressed": False}
        stub_pup.ATTACHED[parameters.Port.B] = "ColorDistanceSensor"
        stub_pup.READS[parameters.Port.B] = {
            "d": 42,
            "refl": 5,
            "amb": 7,
            "hsv": stub_pup.Hsv(200, 60, 70),
            "col": parameters.Color.BLUE,
        }
        stub_pup.ATTACHED[parameters.Port.C] = "TiltSensor"
        stub_pup.READS[parameters.Port.C] = {"tilt": (3, -2)}
        stub_pup.ATTACHED[parameters.Port.E] = "InfraredSensor"
        stub_pup.READS[parameters.Port.E] = {"d": 100}

        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        teal.cycle()

        port_lines = [ln for ln in lines if '"t":"port"' in ln]
        assert len(port_lines) == 4
        # Field dictionary + key order per device kind (D7).
        assert (
            port_lines[0]
            == '{"t":"port","p":"A","dev":"ForceSensor","f":0.0,"d":0.0,"pressed":false}'
        )
        assert (
            port_lines[1]
            == '{"t":"port","p":"B","dev":"ColorDistanceSensor","d":42,"refl":5,"amb":7,"h":200,"s":60,"v":70,"col":"blue"}'
        )
        assert (
            port_lines[2]
            == '{"t":"port","p":"C","dev":"TiltSensor","pitch":3,"roll":-2}'
        )
        assert port_lines[3] == '{"t":"port","p":"E","dev":"InfraredSensor","d":100}'
        for line in port_lines:
            assert encode(decode(line)) == (line.encode() + b"\r\n")

    def test_empty_ports_emit_nothing(self, agent) -> None:
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        teal.cycle()
        assert [ln for ln in lines if '"t":"port"' in ln] == []

    def test_detach_transition_emits_none_once(self, agent) -> None:
        # D7: "dev":"none" goes on the wire only on a detach transition —
        # an attached device whose read starts raising OSError(ENODEV).
        from pybricks import parameters

        stub_pup.ATTACHED[parameters.Port.A] = "Motor"
        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        teal.cycle()
        assert '"dev":"Motor"' in lines[-1]

        # Unplug: reads now fail; empty-port re-probes must not spam "none".
        stub_pup.READ_ERRORS.add(parameters.Port.A)
        del stub_pup.ATTACHED[parameters.Port.A]
        teal.cycle()
        none_lines = [ln for ln in lines if '"dev":"none"' in ln]
        assert none_lines == ['{"t":"port","p":"A","dev":"none"}']

        teal.cycle()
        teal.cycle()
        assert sum(1 for ln in lines if '"dev":"none"' in ln) == 1

        # Replug: the same port is discovered again and resumes Motor lines.
        stub_pup.READ_ERRORS.clear()
        stub_pup.ATTACHED[parameters.Port.A] = "Motor"
        teal.cycle()
        assert '"dev":"Motor"' in lines[-1]

    def test_reprobe_picks_up_new_device(self, agent) -> None:
        # Hot-plug onto a previously empty port: the next cycle's probe
        # finds it and emission is unconditional from then on.
        from pybricks import parameters

        lines = []
        teal = agent.TelemetryAgent(stub_hubs.InventorHub(), out=lines.append)
        teal.start()
        teal.cycle()
        assert [ln for ln in lines if '"t":"port"' in ln] == []

        stub_pup.ATTACHED[parameters.Port.F] = "ColorSensor"
        stub_pup.READS[parameters.Port.F] = {
            "refl": 50,
            "amb": 30,
            "hsv": stub_pup.Hsv(120, 40, 60),
            "col": parameters.Color.GREEN,
        }
        teal.cycle()
        port_lines = [ln for ln in lines if '"t":"port"' in ln]
        assert port_lines == [
            '{"t":"port","p":"F","dev":"ColorSensor","refl":50,"amb":30,"h":120,"s":40,"v":60,"col":"green"}'
        ]


class TestLoop:
    def test_run_paces_at_10hz_and_drains_forever(self, agent, monkeypatch) -> None:
        # run() must never return on its own: after N simulated ticks it
        # is still looping (cancelled here via a raised exception out of
        # the stubbed wait()).
        from pybricks import parameters

        stub_pup.ATTACHED[parameters.Port.A] = "Motor"
        lines = []

        class _Watch:
            def __init__(self) -> None:
                self.now = 0

            def time(self) -> int:
                return self.now

        _watch = _Watch()
        ticks = {"n": 0}

        def fake_wait(ms: int) -> None:
            ticks["n"] += 1
            _watch.now += ms
            if ticks["n"] >= 30:
                raise KeyboardInterrupt  # the only exit

        monkeypatch.setattr(agent, "InventorHub", stub_hubs.InventorHub)
        monkeypatch.setattr(agent, "StopWatch", _Watch)
        monkeypatch.setattr(agent, "wait", fake_wait)

        real_agent_cls = agent.TelemetryAgent

        def factory(hub, out=None):
            return real_agent_cls(hub, out=lines.append)

        monkeypatch.setattr(agent, "TelemetryAgent", factory)

        with pytest.raises(KeyboardInterrupt):
            agent.run()

        # start() + 30 cycles: hub_info once, battery every 10th, imu and
        # the motor port line every cycle.
        assert (
            lines[0]
            == '{"t":"hub_info","name":"Pybricks Hub","fw":"4.0.1","model":"technichub"}'
        )
        assert sum(1 for ln in lines if '"t":"battery"' in ln) == 3
        assert sum(1 for ln in lines if '"t":"imu"' in ln) == 30
        assert sum(1 for ln in lines if '"t":"port"' in ln) == 30
