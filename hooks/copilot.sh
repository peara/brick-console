#!/usr/bin/env bash
# copilot.sh — GitHub Copilot preToolUse adapter → shared uv policy (hooks/uv-check.sh).
#
# stdin:  {"toolName":"bash","toolInput":{"command":"…"}}   (camelCase)
# stdout: {"permissionDecision":"deny","permissionDecisionReason":"…"} on violation;
#         nothing on allow ({} / empty falls through to the normal permission flow).
# Also runs in the Copilot cloud agent, where only repo-committed .github/hooks/*.json
# load — jq may be absent there, so we fail open rather than brick the sandbox.
set -euo pipefail
command -v jq >/dev/null || exit 0

CMD=$(jq -r '.toolInput?.command? // empty' 2>/dev/null || true)
[ -z "$CMD" ] && exit 0

REASON=$(bash "$(dirname "$0")/uv-check.sh" "$CMD") || {
  jq -n --arg r "$REASON" '{permissionDecision:"deny",permissionDecisionReason:$r}'
  exit 0
}
exit 0