"""Shared pytest configuration: the hub-test gate and the Pybricks stubs.

Tests marked ``@pytest.mark.hub`` need the physical Pybricks hub (powered on,
reachable over BLE). They are skipped automatically unless the environment
variable ``BRICK_CONSOLE_HUB_TESTS`` is set to ``1``, so the full suite can
run safely while the hub is off (AGENTS.md, issue #1).

The stub packages under ``tests/stubs/`` provide CPython stand-ins for
``pybricks.*`` so the hub-side agent (``agent/brick_telemetry.py``) can be
imported and exercised on the host (testing.md hub-agent tier). They are
registered via ``sys.path`` here — before any test imports the agent —
and model only the surface the agent touches: construct-time probing
(``OSError(ENODEV)`` on empty/wrong ports), read methods returning plain
values, and the hub's battery/imu/system APIs.
"""

import os
import sys
from pathlib import Path

import pytest

_SKIP_REASON = (
    "needs the physical Pybricks hub over BLE; "
    "set BRICK_CONSOLE_HUB_TESTS=1 to run hub tests"
)

_STUBS = Path(__file__).resolve().parent / "stubs"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get("BRICK_CONSOLE_HUB_TESTS") == "1":
        return
    skip_hub = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("hub") is not None:
            item.add_marker(skip_hub)


# The pybricks stubs must be importable for agent tests to import the
# agent module at all. Put them at the very front so they win over any
# same-named package on the path (there is none on the host).
sys.path.insert(0, str(_STUBS))
