"""Stub of ``pybricks.pupdevices``: construct-probe and read methods.

Construction is the discovery mechanism: it succeeds only when
``ATTACHED[port]`` names this class's device, else raises ``OSError`` with
``ENODEV`` (19) — the documented probe shape on Pybricks v4.0.1.

Test registries (all module-level, reset via ``reset()``):

- ``ATTACHED`` — port → device name string (which class constructs there).
- ``READS`` — port → {attr: value} per-read overrides.
- ``READ_ERRORS`` — ports whose reads raise ``OSError`` (detach shape).
- ``CONSTRUCTED`` — every construction attempt, for probe tests.
"""

from __future__ import annotations

from typing import ClassVar

from pybricks import parameters

ENODEV = 19

ATTACHED: dict = {}
READS: dict = {}
READ_ERRORS: set = set()
CONSTRUCTED: list = []


def reset() -> None:
    ATTACHED.clear()
    READS.clear()
    READ_ERRORS.clear()
    CONSTRUCTED.clear()


class Hsv:
    def __init__(self, h: int, s: int, v: int) -> None:
        self.h = h
        self.s = s
        self.v = v


class _PUPDevice:
    NAME = "?"
    DEFAULTS: ClassVar[dict] = {}

    def __init__(self, port) -> None:
        CONSTRUCTED.append((self.NAME, port))
        if ATTACHED.get(port) != self.NAME:
            raise OSError(ENODEV)
        self.port = port

    def _read(self, attr: str):
        if self.port in READ_ERRORS:
            raise OSError(ENODEV)
        override = READS.get(self.port, {})
        if attr in override:
            return override[attr]
        return self.DEFAULTS[attr]


class Motor(_PUPDevice):
    NAME = "Motor"
    DEFAULTS: ClassVar[dict] = {"angle": 0, "speed": 0, "load": 0}

    def angle(self) -> int:
        return self._read("angle")

    def speed(self) -> int:
        return self._read("speed")

    def load(self) -> int:
        return self._read("load")


class ColorSensor(_PUPDevice):
    NAME = "ColorSensor"
    DEFAULTS: ClassVar[dict] = {
        "refl": 0,
        "amb": 0,
        "hsv": None,
        "col": None,
    }

    def reflection(self) -> int:
        return self._read("refl")

    def ambient(self) -> int:
        return self._read("amb")

    def hsv(self) -> Hsv:
        value = self._read("hsv")
        if value is None:
            return Hsv(0, 0, 0)
        return value

    def color(self):
        value = self._read("col")
        if value is None:
            return parameters.Color.NONE
        return value


class UltrasonicSensor(_PUPDevice):
    NAME = "UltrasonicSensor"
    DEFAULTS: ClassVar[dict] = {"d": 2000, "pr": False}

    def distance(self) -> int:
        return self._read("d")

    def presence(self) -> bool:
        return self._read("pr")


class ForceSensor(_PUPDevice):
    NAME = "ForceSensor"
    DEFAULTS: ClassVar[dict] = {"f": 0.0, "d": 0.0, "pressed": False}

    def force(self) -> float:
        return self._read("f")

    def distance(self) -> float:
        return self._read("d")

    def pressed(self) -> bool:
        return self._read("pressed")


class ColorDistanceSensor(_PUPDevice):
    NAME = "ColorDistanceSensor"
    DEFAULTS: ClassVar[dict] = {
        "d": 0,
        "refl": 0,
        "amb": 0,
        "hsv": None,
        "col": None,
    }

    def distance(self) -> int:
        return self._read("d")

    def reflection(self) -> int:
        return self._read("refl")

    def ambient(self) -> int:
        return self._read("amb")

    def hsv(self) -> Hsv:
        value = self._read("hsv")
        if value is None:
            return Hsv(0, 0, 0)
        return value

    def color(self):
        value = self._read("col")
        if value is None:
            return parameters.Color.NONE
        return value


class TiltSensor(_PUPDevice):
    NAME = "TiltSensor"
    DEFAULTS: ClassVar[dict] = {"tilt": (0, 0)}

    def tilt(self) -> tuple:
        return self._read("tilt")


class InfraredSensor(_PUPDevice):
    NAME = "InfraredSensor"
    DEFAULTS: ClassVar[dict] = {"d": 0}

    def distance(self) -> int:
        return self._read("d")
