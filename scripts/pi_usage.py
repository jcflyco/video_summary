#!/usr/bin/env python3
"""Read cumulative token usage from a pi coding agent session JSONL; finalize Markdown stats."""
from __future__ import annotations

import argparse
import json
import os
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
    newest_by_mtime,
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
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0:
        return int(value)
    return 0


def sessions_root() -> Path:
    """pi stores sessions under <agent-dir>/sessions; both dirs have env overrides."""
    session_dir = (os.environ.get("PI_CODING_AGENT_SESSION_DIR") or "").strip()
    if session_dir:
        return Path(session_dir).expanduser()
    agent_dir = (os.environ.get("PI_CODING_AGENT_DIR") or "").strip()
    root = Path(agent_dir).expanduser() if agent_dir else Path.home() / ".pi" / "agent"
    return root / "sessions"


def encode_session_dir(cwd: Path) -> str:
    """pi's verified rule: "--" + cwd (leading slash stripped, then / \\ : each -> "-") + "--".

    Unlike Claude Code, pi keeps dots, underscores and non-ASCII characters as-is.
    """
    text = str(Path(cwd).expanduser().resolve())
    if text[:1] in ("/", "\\"):
        text = text[1:]
    for ch in ("/", "\\", ":"):
        text = text.replace(ch, "-")
    return f"--{text}--"


def find_session_file(cwd: Path | None, session: Path | None) -> Path | None:
    if session is not None:
        return session if session.is_file() else None

    root = sessions_root()
    if not root.is_dir():
        return None

    # 1) Newest transcript inside the cwd-derived session directory.
    if cwd:
        candidate_dir = root / encode_session_dir(cwd)
        if candidate_dir.is_dir():
            found = newest_by_mtime(candidate_dir.glob("*.jsonl"))
            if found:
                return found

    # 2) Fall back to the newest transcript across every session dir, but only
    #    when it is recent enough to plausibly be the running session.
    newest = newest_by_mtime(root.glob("*/*.jsonl"))
    if newest and (time.time() - newest.stat().st_mtime) <= RECENT_SECONDS:
        return newest
    return None


def accumulate_session(path: Path) -> tuple[dict[str, int], str | None, str | None, int | None]:
    """Sum per-message usage. Return (totals, model, effort, last_usage_event_epoch_s).

    pi appends one record per event; assistant messages carry a per-request `usage`
    {input, output, cacheRead, cacheWrite, reasoning, totalTokens}.  `reasoning` is a
    detail: most providers already count thinking inside `output` (then totalTokens ==
    input+output+cacheRead+cacheWrite); Gemini keeps thoughts out of `output` (then
    totalTokens is larger).  Add `reasoning` to 输出 only in the latter case so thinking
    counts as output without double-counting.
    """
    totals = {"input_total": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}
    model: str | None = None
    effort: str | None = None
    last_event: int | None = None
    saw_usage = False
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        rtype = record.get("type")
        if rtype == "model_change":
            mid = record.get("modelId")
            if isinstance(mid, str) and mid.strip():
                model = mid.strip()
            continue
        if rtype == "thinking_level_change":
            # "off" is not a known effort level -> normalize_effort returns None,
            # which correctly clears the label instead of printing "off".
            effort = normalize_effort(record.get("thinkingLevel"))
            continue
        if rtype != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        saw_usage = True
        inp = number(usage.get("input"))
        out = number(usage.get("output"))
        cache_read = number(usage.get("cacheRead"))
        cache_write = number(usage.get("cacheWrite"))
        reasoning = number(usage.get("reasoning"))
        total = number(usage.get("totalTokens"))
        if reasoning and total > inp + out + cache_read + cache_write:
            out += reasoning
        totals["input_total"] += inp
        totals["output"] += out
        totals["cache_read"] += cache_read
        totals["cache_write"] += cache_write
        mid = message.get("model")
        if isinstance(mid, str) and mid.strip():
            model = mid.strip()
        ts = message.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            # epoch milliseconds on assistant messages
            last_event = int(ts / 1000) if ts > 10**11 else int(ts)

    if not saw_usage:
        return unavailable_totals(), model, effort, last_event
    totals["total"] = (
        totals["input_total"] + totals["output"] + totals["cache_read"] + totals["cache_write"]
    )
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
    session: Path,
    totals: dict[str, int],
    model: str | None,
    effort: str | None = None,
) -> None:
    data = {
        "agent": "pi",
        "session_file": str(session),
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
    session_override: Path | None,
    cwd: Path,
) -> int:
    base = load_json(baseline_file)
    if not base:
        print("error=baseline_unreadable", file=sys.stderr)
        return 1
    session_path = session_override or Path(str(base.get("session_file") or ""))
    if not session_path.is_file():
        session_path = find_session_file(cwd, None)
    if session_path is None or not session_path.is_file():
        print("error=session_unavailable", file=sys.stderr)
        current, model, effort, last_event = unavailable_totals(), None, None, None
    else:
        current, model, effort, last_event = accumulate_session(session_path)
        print(f"SESSION_FILE={session_path}")

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
    # Speed window: summary baseline → last attributed usage event (the newest
    # assistant message's timestamp); fall back to END when unavailable.
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
        # pi's `input` excludes cacheRead/cacheWrite (totalTokens is the plain sum).
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
    parser.add_argument("--session", type=Path, help="pi session .jsonl path")
    parser.add_argument("--cwd", type=Path, default=None, help="Project cwd used to locate ~/.pi/agent/sessions/…")
    parser.add_argument("--model", action="store_true", help="Only print MODEL=")
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

    cwd = args.cwd or Path.cwd()

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
                session_override=args.session,
                cwd=cwd,
            )
        )

    session = find_session_file(cwd, args.session)

    if args.doctor:
        model = None
        effort = None
        if session is not None and session.is_file():
            _totals, model, effort, _last = accumulate_session(session)
        effort = normalize_effort(args.effort) or effort
        raise SystemExit(
            0
            if print_doctor(
                [
                    ("AGENT", "pi"),
                    ("SKILL_VERSION", format_skill_version(read_skill_version(Path(__file__).resolve().parent))),
                    ("CWD", str(cwd)),
                    ("SESSIONS_DIR_KEY", encode_session_dir(cwd)),
                    ("SESSION_FILE", str(session) if session else ""),
                    ("MODEL", compose_model_label(model, effort) if model else ""),
                    # Effort is informational: thinking off / unset is not degraded.
                    ("EFFORT", effort or ""),
                ],
                essential=("SESSION_FILE", "MODEL"),
            )
            else 2
        )

    if session is None or not session.is_file():
        print("SESSION_FILE=不可用")
        if args.snapshot_baseline:
            print(
                "error=session_unavailable_baseline_not_written "
                f"cwd={cwd} tried={sessions_root() / encode_session_dir(cwd)}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if args.model:
            print("MODEL=不可用")
            print(f"EFFORT={normalize_effort(args.effort) or '不可用'}")
            return
        print_totals(args.label, unavailable_totals())
        return

    totals, model, effort, _last = accumulate_session(session)
    effort = normalize_effort(args.effort) or effort
    print(f"SESSION_FILE={session}")
    print(f"MODEL={compose_model_label(model, effort)}")
    print(f"EFFORT={effort or '不可用'}")
    if args.model:
        return
    print_totals(args.label, totals)
    if args.snapshot_baseline:
        snapshot_baseline(args.snapshot_baseline, session, totals, model, effort)
        print(f"BASELINE_FILE={args.snapshot_baseline}")


if __name__ == "__main__":
    main()
