#!/usr/bin/env python3
"""Read cumulative token usage from a Codex rollout; finalize Markdown stats."""
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


def number(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def sessions_root() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


def find_session_file(session: Path | None) -> Path | None:
    """Locate the active Codex rollout.

    CODEX_THREAD_ID is only exported in some Codex CLI versions.  Without a
    fallback the whole stats block degraded to 不可用, so also accept the newest
    recent rollout under $CODEX_HOME/sessions.
    """
    if session is not None:
        return session if session.is_file() else None
    sessions = sessions_root()
    if not sessions.is_dir():
        return None
    thread_id = (os.environ.get("CODEX_THREAD_ID") or "").strip()
    if thread_id:
        found = newest_by_mtime(sessions.rglob(f"*{thread_id}*.jsonl"))
        if found:
            return found
    newest = newest_by_mtime(sessions.rglob("*.jsonl"))
    if newest and (time.time() - newest.stat().st_mtime) <= 6 * 3600:
        return newest
    return None


def read_latest_usage(session: Path) -> tuple[dict[str, int], str | None, str | None]:
    latest: dict[str, Any] | None = None
    model: str | None = None
    effort: str | None = None
    for line in session.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        usage = info.get("total_token_usage") if isinstance(info, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int) and isinstance(
            usage.get("output_tokens"), int
        ):
            latest = usage
        for key in ("model", "model_name"):
            mid = info.get(key) if isinstance(info, dict) else None
            if isinstance(mid, str) and mid.strip():
                model = mid.strip()
        mid2 = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(mid2, str) and mid2.strip():
            model = mid2.strip()

        # Reasoning level, verified against real rollout JSONL.  Checked in
        # least→most specific order so the most specific value wins per record,
        # and the newest record wins overall (a mid-session change is honoured).
        thread_settings = (
            payload.get("thread_settings") if isinstance(payload.get("thread_settings"), dict) else {}
        )
        for container, key in (
            ((payload.get("collaboration_mode") or {}).get("settings"), "reasoning_effort"),
            (thread_settings, "reasoning_effort"),
            (payload, "effort"),
        ):
            if not isinstance(container, dict):
                continue
            level = normalize_effort(container.get(key))
            if level:
                effort = level

    if latest is None:
        return unavailable_totals(), model, effort

    # Codex rollouts spell this "cache_write_input_tokens" (verified against real
    # rollout JSONL); the Anthropic-style "cache_creation_input_tokens" is never
    # present, so 缓存写 used to be 不可用 on every single Codex run.
    cache_write = -1
    for key in ("cache_write_input_tokens", "cache_creation_input_tokens"):
        raw = latest.get(key)
        if isinstance(raw, int) and raw >= 0:
            cache_write = raw
            break

    totals = {
        "input_total": number(latest.get("input_tokens")),
        "output": number(latest.get("output_tokens")),
        "cache_read": number(latest.get("cached_input_tokens")),
        "cache_write": cache_write,
    }

    # total_tokens is authoritative when present.  number() maps a MISSING key to 0,
    # so the old `if totals["total"] < 0` guard never fired and 合计 silently became 0.
    raw_total = latest.get("total_tokens")
    if isinstance(raw_total, int) and raw_total >= 0:
        totals["total"] = raw_total
    else:
        comps = [totals[k] for k in ("input_total", "output", "cache_read", "cache_write")]
        totals["total"] = sum(c for c in comps if c >= 0) if comps[0] >= 0 and comps[1] >= 0 else -1
    return totals, model, effort


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
        "agent": "codex",
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
) -> int:
    base = load_json(baseline_file)
    if not base:
        print("error=baseline_unreadable", file=sys.stderr)
        return 1
    session_path = session_override or Path(str(base.get("session_file") or ""))
    if not session_path.is_file():
        session_path = find_session_file(None)
    if session_path is None or not session_path.is_file():
        print("error=session_unavailable", file=sys.stderr)
        current, model, effort = unavailable_totals(), None, None
    else:
        current, model, effort = read_latest_usage(session_path)
        print(f"SESSION_FILE={session_path}")

    baseline_totals = base.get("totals") if isinstance(base.get("totals"), dict) else unavailable_totals()
    delta = subtract_totals(current, baseline_totals)  # type: ignore[arg-type]
    model_name = (model_override or model or base.get("model") or "不可用")
    if not str(model_name).strip():
        model_name = "不可用"
    # Effort: explicit override > live rollout > baseline snapshot.
    effort_level = (
        normalize_effort(effort_override)
        or normalize_effort(effort)
        or normalize_effort(base.get("effort"))
    )
    model_label = compose_model_label(str(model_name), effort_level)

    phases = phase_timings_from_args(download_seconds, whisper_seconds, summary_seconds)
    phases = normalize_phase_timings(phases)
    # See claude_usage.finalize: the divisor is the token-attribution window, not the
    # 总结 sub-phase, so numerator and denominator cover the same span.
    speed_seconds = None
    saved_at = number(base.get("saved_at"))
    if saved_at and end_epoch > saved_at:
        speed_seconds = end_epoch - saved_at
    skill_version = read_skill_version(Path(__file__).resolve().parent)
    section = format_stats_section(
        model=model_label,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        delta=delta,
        phase_timings=phases,
        input_mode="subtract_cache",
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
    parser.add_argument("--session", type=Path)
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
                session_override=args.session,
            )
        )

    session = find_session_file(args.session)

    if args.doctor:
        model = None
        effort = None
        if session is not None and session.is_file():
            _totals, model, effort = read_latest_usage(session)
        effort = normalize_effort(args.effort) or effort
        raise SystemExit(
            0
            if print_doctor(
                [
                    ("AGENT", "codex"),
                    ("SKILL_VERSION", format_skill_version(read_skill_version(Path(__file__).resolve().parent))),
                    ("CODEX_HOME", str(sessions_root().parent)),
                    ("THREAD_ID", (os.environ.get("CODEX_THREAD_ID") or "")),
                    ("SESSION_FILE", str(session) if session else ""),
                    ("MODEL", compose_model_label(model, effort) if model else ""),
                    # Informational only — a rollout without an effort marker is
                    # not a degraded environment.
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
                f"codex_home={sessions_root().parent}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print("MODEL=不可用")
        print(f"EFFORT={normalize_effort(args.effort) or '不可用'}")
        print_totals(args.label, unavailable_totals())
        return

    totals, model, effort = read_latest_usage(session)
    effort = normalize_effort(args.effort) or effort
    print(f"SESSION_FILE={session}")
    # Always emit MODEL= so callers can parse one stable contract (was conditional,
    # which made "no MODEL line" ambiguous with "script crashed").
    print(f"MODEL={compose_model_label(model, effort)}")
    print(f"EFFORT={effort or '不可用'}")
    print_totals(args.label, totals)
    if args.snapshot_baseline:
        snapshot_baseline(args.snapshot_baseline, session, totals, model, effort)
        print(f"BASELINE_FILE={args.snapshot_baseline}")


if __name__ == "__main__":
    main()
