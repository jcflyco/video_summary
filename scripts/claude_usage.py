#!/usr/bin/env python3
"""Read cumulative token usage from a Claude Code session JSONL; finalize Markdown stats."""
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
    path_key_variants,
    phase_timings_from_args,
    print_doctor,
    read_skill_version,
    speed_window_seconds,
    subtract_totals,
    unavailable_totals,
)


def number(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def projects_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def encode_project_dir(cwd: Path) -> str:
    """Claude Code project folder name for a workspace path.

    Verified rule: leading "-", then every character outside [A-Za-z0-9-] becomes
    one "-".  The previous implementation only mapped "/" and "_", so any path with
    a dot, a space or non-ASCII (e.g. a Chinese directory name) resolved to a folder
    that does not exist — and model/token/LLM 速度 all silently degraded to 不可用.
    """
    return path_key_variants(cwd, leading_dash=True)[0]


def project_dir_for_cwd(cwd: Path) -> Path:
    root = projects_root()
    variants = path_key_variants(cwd, leading_dash=True)
    for key in variants:
        candidate = root / key
        if candidate.is_dir():
            return candidate
    return root / variants[0]


def session_by_id(sid: str) -> Path | None:
    """Locate <session-id>.jsonl anywhere under the projects root."""
    root = projects_root()
    if not sid or not root.is_dir():
        return None
    return newest_by_mtime(root.glob(f"*/{sid}.jsonl"))


def find_session_file(cwd: Path | None, session: Path | None) -> Path | None:
    if session is not None:
        return session if session.is_file() else None

    # 1) Explicit session id wins — search the whole projects root, not just the
    #    directory we guessed for cwd (the guess can be wrong for exotic paths).
    for key in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "SESSION_ID"):
        sid = (os.environ.get(key) or "").strip()
        if not sid:
            continue
        if cwd:
            candidate = project_dir_for_cwd(cwd) / f"{sid}.jsonl"
            if candidate.is_file():
                return candidate
        found = session_by_id(sid)
        if found:
            return found

    if not cwd:
        return None

    # 2) Newest transcript inside the resolved project directory.
    proj = project_dir_for_cwd(cwd)
    if proj.is_dir():
        found = newest_by_mtime(proj.glob("*.jsonl"))
        if found:
            return found

    # 3) Last resort: the project dir could not be resolved from the path at all.
    #    Fall back to the newest transcript across every project, but only when it
    #    is recent enough to plausibly be the running session.
    root = projects_root()
    if not root.is_dir():
        return None
    newest = newest_by_mtime(root.glob("*/*.jsonl"))
    if newest and (time.time() - newest.stat().st_mtime) <= 6 * 3600:
        return newest
    return None


def extract_usage(record: dict[str, Any]) -> dict[str, int] | None:
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if not isinstance(inp, int) and not isinstance(out, int):
        return None
    return {
        "input_total": number(inp),
        "output": number(out),
        "cache_read": number(usage.get("cache_read_input_tokens")),
        "cache_write": number(usage.get("cache_creation_input_tokens")),
    }


def usage_score(usage: dict[str, int]) -> int:
    return usage["input_total"] + usage["output"] + usage["cache_read"] + usage["cache_write"]


def accumulate_session(path: Path) -> tuple[dict[str, int], str | None, str | None]:
    """Dedupe by requestId (keep highest score), then sum. Return totals + model + effort."""
    best: dict[str, dict[str, int]] = {}
    model: str | None = None
    effort: str | None = None
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        usage = extract_usage(record)
        if not usage:
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        mid = message.get("model") if isinstance(message, dict) else None
        if isinstance(mid, str) and mid.strip() and mid.strip().lower() not in {"<synthetic>", "synthetic"}:
            model = mid.strip()
        # Claude Code writes the reasoning level on the assistant record itself
        # ("effort": "high"); keep the newest one so a mid-session /effort switch wins.
        level = normalize_effort(record.get("effort"))
        if level:
            effort = level
        rid = record.get("requestId") or record.get("request_id") or record.get("uuid")
        key = str(rid) if rid else f"anon:{len(best)}"
        prev = best.get(key)
        if prev is None or usage_score(usage) >= usage_score(prev):
            best[key] = usage

    if not best:
        return unavailable_totals(), model, effort
    totals = {"input_total": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}
    for usage in best.values():
        for key in ("input_total", "output", "cache_read", "cache_write"):
            totals[key] += usage[key]
    totals["total"] = (
        totals["input_total"] + totals["output"] + totals["cache_read"] + totals["cache_write"]
    )
    return totals, model, effort


def print_totals(label: str, totals: dict[str, int]) -> None:
    if totals.get("total", -1) < 0:
        print(
            f"{label} input_total=不可用 output=不可用 cache_read=不可用 "
            f"cache_write=不可用 total=不可用"
        )
        return
    print(
        "{} input_total={} output={} cache_read={} cache_write={} total={}".format(
            label,
            totals["input_total"],
            totals["output"],
            totals["cache_read"],
            totals["cache_write"],
            totals["total"],
        )
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
        "agent": "claude",
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
        current, model, effort = unavailable_totals(), None, None
    else:
        current, model, effort = accumulate_session(session_path)
        print(f"SESSION_FILE={session_path}")

    baseline_totals = base.get("totals") if isinstance(base.get("totals"), dict) else unavailable_totals()
    delta = subtract_totals(current, baseline_totals)  # type: ignore[arg-type]
    model_name = (model_override or model or base.get("model") or "不可用")
    if not str(model_name).strip():
        model_name = "不可用"
    # Effort: explicit override > live session > baseline snapshot.
    effort_level = (
        normalize_effort(effort_override)
        or normalize_effort(effort)
        or normalize_effort(base.get("effort"))
    )
    model_label = compose_model_label(str(model_name), effort_level)

    phases = phase_timings_from_args(download_seconds, whisper_seconds, summary_seconds)
    phases = normalize_phase_timings(phases)
    # LLM 速度 divides by the window the counted tokens were produced in, i.e. from the
    # baseline snapshot to now (finalize runs right after END).  Falls back to the
    # video's own START→END window when the baseline predates this script's version.
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
    parser.add_argument("--cwd", type=Path, default=None, help="Project cwd used to locate ~/.claude/projects/…")
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
            _totals, model, effort = accumulate_session(session)
        effort = normalize_effort(args.effort) or effort
        raise SystemExit(
            0
            if print_doctor(
                [
                    ("AGENT", "claude"),
                    ("SKILL_VERSION", format_skill_version(read_skill_version(Path(__file__).resolve().parent))),
                    ("CWD", str(cwd)),
                    ("PROJECT_DIR_KEY", encode_project_dir(cwd)),
                    ("SESSION_FILE", str(session) if session else ""),
                    ("MODEL", compose_model_label(model, effort) if model else ""),
                    # Effort is informational: a session with no effort marker is
                    # not degraded, so it stays out of `essential`.
                    ("EFFORT", effort or ""),
                ],
                essential=("SESSION_FILE", "MODEL"),
            )
            else 2
        )

    if session is None or not session.is_file():
        print("SESSION_FILE=不可用")
        # A baseline that was never written surfaces much later as
        # error=baseline_unreadable during --finalize.  Fail here instead.
        if args.snapshot_baseline:
            print(
                "error=session_unavailable_baseline_not_written "
                f"cwd={cwd} tried={','.join(path_key_variants(cwd, leading_dash=True))}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if args.model:
            print("MODEL=不可用")
            print(f"EFFORT={normalize_effort(args.effort) or '不可用'}")
            return
        print_totals(args.label, unavailable_totals())
        return

    totals, model, effort = accumulate_session(session)
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
