"""Placeholder for the M1 hardware smoke test.

Its only job right now is proving the hub-test gate: skipped by default,
trivially green when BRICK_CONSOLE_HUB_TESTS=1. It touches no hardware.
The real hardware smoke test replaces this body in the M1 finale (issue #1).
"""

import pytest


@pytest.mark.hub
def test_hub_placeholder() -> None:
    assert True
