"""Stub of ``pybricks.parameters``: Port, Side, and Color members.

Members are unique sentinel objects (like the firmware's C enums — ``str()``
of a real member prints ``"Side.TOP"``, not ``"top"``), so the agent's
member→wire-string mappings are exercised for real.
"""


class _Member:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


class Port:
    A = _Member("Port.A")
    B = _Member("Port.B")
    C = _Member("Port.C")
    D = _Member("Port.D")
    E = _Member("Port.E")
    F = _Member("Port.F")


class Side:
    TOP = _Member("Side.TOP")
    BOTTOM = _Member("Side.BOTTOM")
    LEFT = _Member("Side.LEFT")
    RIGHT = _Member("Side.RIGHT")
    FRONT = _Member("Side.FRONT")
    BACK = _Member("Side.BACK")


class Color:
    RED = _Member("Color.RED")
    BROWN = _Member("Color.BROWN")
    ORANGE = _Member("Color.ORANGE")
    YELLOW = _Member("Color.YELLOW")
    GREEN = _Member("Color.GREEN")
    CYAN = _Member("Color.CYAN")
    BLUE = _Member("Color.BLUE")
    MAGENTA = _Member("Color.MAGENTA")
    VIOLET = _Member("Color.VIOLET")
    BLACK = _Member("Color.BLACK")
    GRAY = _Member("Color.GRAY")
    WHITE = _Member("Color.WHITE")
    NONE = _Member("Color.NONE")
