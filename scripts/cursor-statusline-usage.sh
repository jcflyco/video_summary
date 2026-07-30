#!/usr/bin/env bash
# Cursor CLI statusLine helper: snapshot context_window token totals for video_summary.
# Installed by cursor_usage.py --ensure-hook; wired into ~/.cursor/cli-config.json.
set -euo pipefail

input=$(cat)
usage_script="$HOME/.cursor/hooks/video_summary-cursor_usage.py"
if [[ -f "$usage_script" ]]; then
  printf '%s' "$input" | python3 "$usage_script" --ingest-context >/dev/null 2>&1 || true
fi

# Keep status line minimal; users can chain their own script later.
exit 0
