#!/usr/bin/env python3
"""Render docs/badges/coverage.svg from a coverage.py JSON report (issue #31).

Reads the coverage.json that ``pytest --cov-report=json`` leaves at the repo
root and writes a flat SVG badge — green >=90 / yellow >=75 / red below —
that CI commits back to the branch when it changes. Stdlib-only by design:
the badge pipeline must not grow dependencies beyond the report it reads.

Usage: coverage_badge.py [coverage.json] [out.svg]
"""

import json
import sys
from html import escape
from pathlib import Path

LABEL = "coverage"
GREEN, YELLOW, RED = "#97ca00", "#dfb317", "#e05d44"  # shields.io flat palette
CHAR_W, PAD = 7, 6  # Verdana 11px char advance + text side padding, px


def band(percent: int) -> str:
    """Thresholds apply to the displayed (rounded) figure — a badge that
    reads ``90%`` must never wear the yellow of an 89.9 underneath."""
    if percent >= 90:
        return GREEN
    if percent >= 75:
        return YELLOW
    return RED


def render(percent: float) -> str:
    rounded = round(percent)
    value = f"{rounded}%"
    label_w = len(LABEL) * CHAR_W + 2 * PAD
    value_w = len(value) * CHAR_W + 2 * PAD
    total_w = label_w + value_w
    label, value = escape(LABEL), escape(value)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="20" role="img" aria-label="{label}: {value}">
  <title>{label}: {value}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="{total_w}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_w}" height="20" fill="#555"/>
    <rect x="{label_w}" width="{value_w}" height="20" fill="{band(rounded)}"/>
    <rect width="{total_w}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="{label_w / 2}" y="15">{label}</text>
    <text x="{label_w + value_w / 2}" y="15">{value}</text>
  </g>
</svg>
"""


def main(argv: list[str]) -> int:
    if len(argv) > 3:
        print(
            f"usage: {Path(__file__).name} [coverage.json] [out.svg]", file=sys.stderr
        )
        return 2
    json_path = Path(argv[1]) if len(argv) > 1 else Path("coverage.json")
    out_path = Path(argv[2]) if len(argv) > 2 else Path("docs/badges/coverage.svg")
    try:
        data = json.loads(json_path.read_text())
        percent = float(data["totals"]["percent_covered"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            f"coverage_badge: cannot read coverage from {json_path}: {exc}",
            file=sys.stderr,
        )
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(percent))
    print(f"coverage_badge: {out_path} — {round(percent)}% {band(round(percent))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
