#!/usr/bin/env python3
"""Read cumulative token usage from the OpenCode SQLite DB; finalize Markdown stats."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from runtime_stats_lib import (  # noqa: E402
    compose_model_label,
    format_llm_speed,
    format_skill_version,
    format_stats_section,
    normalize_effort,
    normalize_phase_timings,
    patch_markdown_stats,
    phase_timings_from_args,
    print_doctor,
    read_skill_version,
    speed_window_seconds,
    subtract_totals,
    unavailable_totals,
)

RECENT_SECONDS = 6 * 3600


def number(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def db_path() -> Path:
    override = (os.environ.get("OPENCODE_DB") or "").strip()
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local/share"
    return root / "opencode" / "opencode.db"


def connect(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def find_session_id(conn: sqlite3.Connection, session_id: str | None) -> str | None:
    """Locate the active OpenCode session.

    OpenCode does not export a session id to shell-tool children, so fall back to
    the newest recently-updated session whose directory matches cwd (the shell tool
    that is running this very script keeps that session's time_updated fresh),
    then to the newest recent session overall — mirroring codex_usage.py.
    """
    if session_id:
        row = conn.execute("SELECT id FROM session WHERE id = ?", (session_id,)).fetchone()
        return row["id"] if row else None
    env_sid = (os.environ.get("OPENCODE_SESSION_ID") or "").strip()
    if env_sid:
        row = conn.execute("SELECT id FROM session WHERE id = ?", (env_sid,)).fetchone()
        if row:
            return row["id"]
    cutoff = int((time.time() - RECENT_SECONDS) * 1000)
    try:
        cwd = str(Path.cwd().resolve())
    except OSError:
        cwd = ""
    if cwd:
        row = conn.execute(
            "SELECT id FROM session WHERE directory = ? AND time_updated >= ?"
            " ORDER BY time_updated DESC LIMIT 1",
            (cwd, cutoff),
        ).fetchone()
        if row:
            return row["id"]
    row = conn.execute(
        "SELECT id FROM session WHERE time_updated >= ? ORDER BY time_updated DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    return row["id"] if row else None


def read_latest_usage(
    conn: sqlite3.Connection, session_id: str
) -> tuple[dict[str, int], str | None, str | None, int | None]:
    """Return (totals, model, effort, last_usage_event_epoch_seconds).

    Session-row counters are running sums of every assistant request (verified:
    they equal the per-message sums).  OpenCode's `input` is nonCachedInputTokens
    and `output` is visibleOutputTokens (reasoning kept separately), so 输入 is
    used as-is and 输出 = output + reasoning to match the Claude/Codex口径 where
    thinking tokens count as output.
    """
    row = conn.execute(
        "SELECT tokens_input, tokens_output, tokens_reasoning, tokens_cache_read,"
        " tokens_cache_write, model FROM session WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return unavailable_totals(), None, None, None

    totals = {
        "input_total": number(row["tokens_input"]),
        "output": number(row["tokens_output"]) + number(row["tokens_reasoning"]),
        "cache_read": number(row["tokens_cache_read"]),
        "cache_write": number(row["tokens_cache_write"]),
    }
    totals["total"] = sum(totals[k] for k in ("input_total", "output", "cache_read", "cache_write"))

    model: str | None = None
    effort: str | None = None
    try:
        session_model = json.loads(row["model"] or "{}")
    except (TypeError, json.JSONDecodeError):
        session_model = {}
    if isinstance(session_model, dict):
        sid_model = session_model.get("id")
        if isinstance(sid_model, str) and sid_model.strip():
            model = sid_model.strip()
        effort = normalize_effort(session_model.get("variant"))

    # The message that actually produced the summary can differ from the session's
    # currently-selected model, and its time.completed is the last usage event
    # (the LLM-speed window端点).
    last_event: int | None = None
    for (data,) in conn.execute(
        "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 200",
        (session_id,),
    ):
        try:
            record = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or record.get("role") != "assistant":
            continue
        tokens = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
        if not (number(tokens.get("total")) or number(tokens.get("output")) or number(tokens.get("input"))):
            continue
        mid = record.get("modelID")
        if isinstance(mid, str) and mid.strip():
            model = mid.strip()
        completed = (record.get("time") or {}).get("completed")
        if isinstance(completed, (int, float)) and completed > 0:
            last_event = int(completed / 1000)
        break

    return totals, model, effort, last_event


def print_totals(label: str, totals: dict[str, int]) -> None:
    def fmt(key: str) -> str:
        value = totals.get(key, -1)
        return str(value) if value >= 0 else "不可用"

    print(
        f"{label} input_total={fmt('input_total')} output={fmt('output')} "
        f"cache_read={fmt('cache_read')} cache_write={fmt('cache_write')} total={fmt('total')}"
    )


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def snapshot_baseline(
    path: Path,
    session_id: str,
    totals: dict[str, int],
    model: str | None,
    effort: str | None = None,
) -> None:
    data = {
        "agent": "opencode",
        "db_path": str(db_path()),
        "session_id": session_id,
        "totals": totals,
        "model": model or "不可用",
        "effort": effort or "",
        "model_label": compose_model_label(model, effort),
        "saved_at": int(time.time()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def finalize(
    *,
    baseline_file: Path,
    markdown: Path,
    start_epoch: int,
    end_epoch: int,
    download_seconds: int | None,
    whisper_seconds: int | None,
    summary_seconds: int | None,
    model_override: str | None,
    effort_override: str | None,
    session_override: str | None,
) -> int:
    base = load_json(baseline_file)
    if not base:
        print("error=baseline_unreadable", file=sys.stderr)
        return 1
    conn = connect(db_path())
    session_id = session_override or str(base.get("session_id") or "") or None
    current, model, effort, last_event = unavailable_totals(), None, None, None
    if conn is None:
        print("error=db_unavailable", file=sys.stderr)
    else:
        resolved = find_session_id(conn, session_id)
        if resolved is None:
            print("error=session_unavailable", file=sys.stderr)
        else:
            current, model, effort, last_event = read_latest_usage(conn, resolved)
            print(f"SESSION_ID={resolved}")
        conn.close()

    baseline_totals = base.get("totals") if isinstance(base.get("totals"), dict) else unavailable_totals()
    delta = subtract_totals(current, baseline_totals)  # type: ignore[arg-type]
    model_name = (model_override or model or base.get("model") or "不可用")
    if not str(model_name).strip():
        model_name = "不可用"
    effort_level = (
        normalize_effort(effort_override)
        or normalize_effort(effort)
        or normalize_effort(base.get("effort"))
    )
    model_label = compose_model_label(str(model_name), effort_level)

    phases = phase_timings_from_args(download_seconds, whisper_seconds, summary_seconds)
    phases = normalize_phase_timings(phases)
    # Speed window: summary baseline → last attributed usage event (the completed
    # timestamp of the newest assistant request); fall back to END when the event
    # timestamp is unavailable.
    speed_seconds = None
    saved_at = number(base.get("saved_at"))
    if saved_at:
        if last_event and last_event > saved_at:
            speed_seconds = last_event - saved_at
        elif end_epoch > saved_at:
            speed_seconds = end_epoch - saved_at
    skill_version = read_skill_version(Path(__file__).resolve().parent)
    section = format_stats_section(
        model=model_label,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        delta=delta,
        phase_timings=phases,
        # OpenCode's input counter is nonCachedInputTokens already.
        input_mode="as_is",
        skill_version=skill_version,
        speed_seconds=speed_seconds,
    )
    if not patch_markdown_stats(markdown, section):
        print("error=markdown_missing", file=sys.stderr)
        return 1
    print_totals("DELTA", delta)
    print(f"MODEL={model_label}")
    print(f"SKILL_VERSION={format_skill_version(skill_version)}")
    print(f"EFFORT={effort_level or '不可用'}")
    print(
        "LLM_SPEED="
        + format_llm_speed(
            delta.get("output", -1),
            speed_window_seconds(
                speed_seconds=speed_seconds,
                fallback_seconds=summary_seconds,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
            ),
        )
    )
    print(f"PATCHED={markdown}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="CURRENT")
    parser.add_argument("--session", default="", help="OpenCode session id (ses_…)")
    parser.add_argument("--doctor", action="store_true", help="Preflight: can model/token be resolved?")
    parser.add_argument("--snapshot-baseline", type=Path, help="Write baseline JSON for --finalize")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--baseline-file", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--start-epoch", type=int)
    parser.add_argument("--end-epoch", type=int)
    parser.add_argument("--model-name", default="", help="Override model string when finalizing")
    parser.add_argument(
        "--effort",
        default="",
        help="Override reasoning effort (low/medium/high/xhigh/max) when finalizing",
    )
    parser.add_argument("--download-seconds", type=int, default=None)
    parser.add_argument("--whisper-seconds", type=int, default=None)
    parser.add_argument("--summary-seconds", type=int, default=None)
    args = parser.parse_args()

    if args.finalize:
        if not args.baseline_file or not args.markdown or args.start_epoch is None or args.end_epoch is None:
            print(
                "error=finalize_requires_baseline_markdown_start_end",
                file=sys.stderr,
            )
            raise SystemExit(1)
        raise SystemExit(
            finalize(
                baseline_file=args.baseline_file,
                markdown=args.markdown,
                start_epoch=args.start_epoch,
                end_epoch=args.end_epoch,
                download_seconds=args.download_seconds,
                whisper_seconds=args.whisper_seconds,
                summary_seconds=args.summary_seconds,
                model_override=(args.model_name or None),
                effort_override=(args.effort or None),
                session_override=(args.session or None),
            )
        )

    conn = connect(db_path())
    session_id = find_session_id(conn, args.session or None) if conn else None

    if args.doctor:
        model = None
        effort = None
        if conn is not None and session_id:
            _totals, model, effort, _last = read_latest_usage(conn, session_id)
        if conn is not None:
            conn.close()
        effort = normalize_effort(args.effort) or effort
        raise SystemExit(
            0
            if print_doctor(
                [
                    ("AGENT", "opencode"),
                    ("SKILL_VERSION", format_skill_version(read_skill_version(Path(__file__).resolve().parent))),
                    ("OPENCODE_DB", str(db_path()) if db_path().is_file() else ""),
                    ("SESSION_ID", session_id or ""),
                    ("MODEL", compose_model_label(model, effort) if model else ""),
                    # Informational only — a session without a variant marker is
                    # not a degraded environment.
                    ("EFFORT", effort or ""),
                ],
                essential=("OPENCODE_DB", "SESSION_ID", "MODEL"),
            )
            else 2
        )

    if conn is None or session_id is None:
        if conn is not None:
            conn.close()
        print("SESSION_ID=不可用")
        if args.snapshot_baseline:
            print(
                "error=session_unavailable_baseline_not_written "
                f"opencode_db={db_path()}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print("MODEL=不可用")
        print(f"EFFORT={normalize_effort(args.effort) or '不可用'}")
        print_totals(args.label, unavailable_totals())
        return

    totals, model, effort, _last = read_latest_usage(conn, session_id)
    conn.close()
    effort = normalize_effort(args.effort) or effort
    print(f"SESSION_ID={session_id}")
    print(f"MODEL={compose_model_label(model, effort)}")
    print(f"EFFORT={effort or '不可用'}")
    print_totals(args.label, totals)
    if args.snapshot_baseline:
        snapshot_baseline(args.snapshot_baseline, session_id, totals, model, effort)
        print(f"BASELINE_FILE={args.snapshot_baseline}")


if __name__ == "__main__":
    main()
