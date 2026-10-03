"""Host-side stub of the ``pybricks`` package for agent tests (not shipped).

Models only the surface ``agent/brick_telemetry.py`` touches, faithful to
Pybricks v4.0.1: construct-time probing raises ``OSError(ENODEV)`` on empty
or wrong-type ports; read methods return plain values. Tests configure the
submodule registries (``pybricks.pupdevices.ATTACHED``, ``pybricks.hubs.
BATTERY``/``IMU``/``SYSTEM_INFO``) and inspect the call logs.
"""

version = ("pybricks", "4.0.1", "v4.0.1 on 2026-01-01")
