#!/usr/bin/env bash
# uv-check.sh — shared policy core for all agent hooks in this repo.
#
# Policy: every Python invocation goes through uv (`uv run python …`,
# `uv run pytest`, `uv pip …`). Bare `python`/`python3`/`pip`/`pip3`/`pytest`
# commands are DENIED: the interpreters on PATH are not system interpreters —
# they live in a virtualenv OUTSIDE this repo, so a plain `pip install` or
# `python script.py` silently mutates/uses a foreign environment.
#
# Usage: uv-check.sh "<full command>"
#   exit 0        → allowed (no output)
#   exit 1        → denied  (reason on stdout)
#
# This is a guardrail against accidents, not a sandbox: Python invoked inside
# command substitution, quotes, or heredoc bodies is not reliably detected.
# Comply with the deny message instead of working around the hook.

set -euo pipefail

deny() { echo "$1"; exit 1; }

CMD="$1"

# First word of every command segment (split on ; | && || and newlines),
# after stripping leading whitespace, `sudo`, `env`, and VAR=… assignments.
FIRST_WORDS=$(
  printf '%s\n' "$CMD" \
    | sed -e 's/&&/\n/g' -e 's/||/\n/g' -e 's/[;|]/\n/g' \
    | sed -E -e 's/^[[:space:]]*//' \
             -e 's/^(sudo|env)[[:space:]]+//' \
             -e 's/^([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*//' \
    | awk '{print $1}'
)

while read -r w; do
  case "$w" in
    python|python3|python3.*|pip|pip3|pytest)
      deny "Bare '$w' is blocked in this repo: the python/pip on PATH live OUTSIDE the repo. Run 'uv run $w …' instead (packages: 'uv add …' / 'uv pip …'). See AGENTS.md."
      ;;
  esac
done <<< "$FIRST_WORDS"

# Backstop: obvious indirect invocations.
if printf '%s' "$CMD" | grep -Eq '\$\([[:space:]]*(sudo[[:space:]]+)?(python|python3|pip)|<[[:space:]]*\([[:space:]]*(python|pip)|`[[:space:]]*(python|pip)|xargs[[:space:]]+(python|pip)'; then
  deny "Indirect bare-Python invocation detected. Use 'uv run python …' instead."
fi

exit 0