"""Shared pytest configuration: the hub-test gate.

Tests marked ``@pytest.mark.hub`` need the physical Pybricks hub (powered on,
reachable over BLE). They are skipped automatically unless the environment
variable ``BRICK_CONSOLE_HUB_TESTS`` is set to ``1``, so the full suite can
run safely while the hub is off (AGENTS.md, issue #1).
"""

import os

import pytest

_SKIP_REASON = (
    "needs the physical Pybricks hub over BLE; "
    "set BRICK_CONSOLE_HUB_TESTS=1 to run hub tests"
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get("BRICK_CONSOLE_HUB_TESTS") == "1":
        return
    skip_hub = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("hub") is not None:
            item.add_marker(skip_hub)
