#!/usr/bin/env bash
# Accumulate Cursor agent token usage per conversation for skill stats.
# Installed to ~/.cursor/hooks/ by cursor_usage.py --ensure-hook
# Delegates to Python for locked ledger writes, dedupe, and pending flush.
set -euo pipefail

HOOKS_DIR="${HOME}/.cursor/hooks"
DEBUG_LOG="${HOOKS_DIR}/usage/hook-debug.log"
mkdir -p "${HOOKS_DIR}/usage"

SCRIPT="${HOOKS_DIR}/video_summary-cursor_usage.py"
if [[ ! -f "$SCRIPT" ]]; then
  # Fall back to skill checkout when hook copy is missing.
  for cand in \
    "${HOME}/.agents/skills/video_summary/scripts/cursor_usage.py" \
    "${HOME}/.claude/skills/video_summary/scripts/cursor_usage.py"; do
    if [[ -f "$cand" ]]; then
      SCRIPT="$cand"
      break
    fi
  done
fi
[[ -f "$SCRIPT" ]] || exit 0

# Resolve runtime_stats_lib even if the hooks directory copy is stale/missing.
export PYTHONPATH="${HOOKS_DIR}:${HOME}/.agents/skills/video_summary/scripts:${HOME}/.claude/skills/video_summary/scripts:${PYTHONPATH:-}"

# Always ingest (even when token fields are absent) so pending markdown can flush.
# Log failures — silent `|| true` previously hid ModuleNotFoundError and left Token「不可用」.
if ! out="$(python3 "$SCRIPT" --ingest-hook 2>&1)"; then
  printf '%s ingest_hook_error=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-500)" \
    >>"$DEBUG_LOG" 2>/dev/null || true
fi
exit 0
