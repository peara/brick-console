#!/usr/bin/env bash
# claude.sh — Claude Code PreToolUse adapter → shared uv policy (hooks/uv-check.sh).
#
# stdin:  {"tool_name":"Bash","tool_input":{"command":"…"}}
# stdout: deny JSON (hookSpecificOutput.permissionDecision) on violation; nothing on allow.
# Hooks only narrow permissions — never emit "allow" here; fall through to the
# normal permission flow instead. Unexpected input shape → exit 0 (fail open, by design).
set -euo pipefail
command -v jq >/dev/null || exit 0

CMD=$(jq -r '.tool_input?.command? // empty' 2>/dev/null || true)
[ -z "$CMD" ] && exit 0

REASON=$(bash "$(dirname "$0")/uv-check.sh" "$CMD") || {
  jq -n --arg r "$REASON" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $r
    }
  }'
  exit 0
}
exit 0