"""Stub of ``pybricks.tools``: wait() and StopWatch (v4.0.1 method name)."""

from __future__ import annotations

WAITED: list = []


class StopWatch:
    def __init__(self) -> None:
        self.now = 0

    def time(self) -> int:
        return self.now


def wait(ms: int) -> None:
    WAITED.append(ms)
