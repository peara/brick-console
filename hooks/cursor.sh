#!/usr/bin/env bash
# cursor.sh — Cursor beforeShellExecution adapter → shared uv policy (hooks/uv-check.sh).
#
# stdin:  {"command":"…","cwd":"…","sandbox":bool}
# stdout: {"permission":"deny", …} on violation; nothing on allow (falls through to
#         Cursor's own permission flow — this hook never widens permissions).
# Both snake_case and camelCase message keys are emitted for Cursor version compatibility.
set -euo pipefail
command -v jq >/dev/null || exit 0

CMD=$(jq -r '.command? // empty' 2>/dev/null || true)
[ -z "$CMD" ] && exit 0

REASON=$(bash "$(dirname "$0")/uv-check.sh" "$CMD") || {
  jq -n --arg r "$REASON" '{permission:"deny",user_message:$r,agent_message:$r,userMessage:$r,agentMessage:$r}'
  exit 0
}
exit 0