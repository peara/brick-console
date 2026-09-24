"""Scaffolding smoke test: the server package imports without packaging changes."""

import brick_console


def test_import_brick_console() -> None:
    # pyproject sets pytest pythonpath=["src"], so src/brick_console is importable.
    assert brick_console.__path__ is not None
