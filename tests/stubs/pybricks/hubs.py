"""Stub of ``pybricks.hubs``: the InventorHub battery/imu/system surface.

Test registries: ``BATTERY`` (mV/mA), ``IMU`` (accel/gyro/up tuples),
``SYSTEM_INFO`` — and ``CONSTRUCTED`` logs every hub construction, so the
import-safety test can prove module import builds no hub.
"""

from __future__ import annotations

from pybricks import parameters

BATTERY: dict = {"v": 8085, "c": 42}
IMU: dict = {
    "acc": (120, -980, 9810),
    "gyro": (0, 0, 3),
    "up": parameters.Side.TOP,
}
SYSTEM_INFO: dict = {"name": "Pybricks Hub"}
CONSTRUCTED: list = []


class Battery:
    def voltage(self) -> int:
        return BATTERY["v"]

    def current(self) -> int:
        return BATTERY["c"]


class Imu:
    def acceleration(self) -> tuple:
        return IMU["acc"]

    def angular_velocity(self) -> tuple:
        return IMU["gyro"]

    def up(self):
        return IMU["up"]


class System:
    def info(self) -> dict:
        return dict(SYSTEM_INFO)


class InventorHub:
    def __init__(self) -> None:
        CONSTRUCTED.append("InventorHub")
        self.battery = Battery()
        self.imu = Imu()
        self.system = System()
