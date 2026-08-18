#!/usr/bin/env python3
"""Shared runtime-stats formatting for Cursor / Claude / Codex usage scripts."""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

InputMode = Literal["subtract_cache", "as_is"]

UNAVAILABLE = "不可用"

# Reasoning/effort levels worth showing next to the model name.  Kept as a
# whitelist so numeric look-alikes (e.g. Codex's reasoning_output_tokens) and
# junk strings can never leak into the 模型 line.
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def normalize_effort(effort: Any) -> str | None:
    """Return a canonical effort level, or None when it isn't a known level."""
    if not isinstance(effort, str):
        return None
    value = effort.strip().lower().replace("_", "").replace("-", "")
    if not value:
        return None
    aliases = {"xhi": "xhigh", "extrahigh": "xhigh", "veryhigh": "xhigh", "maximum": "max"}
    value = aliases.get(value, value)
    return value if value in EFFORT_LEVELS else None


def compose_model_label(model: str | None, effort: Any = None) -> str:
    """Build the 模型 line value: "<model> <effort>" (e.g. "claude-opus-5 high").

    Effort is appended only when it resolves to a known level and isn't already
    part of the model id (some ids bake it in, e.g. "gpt-5.6-sol-high").
    """
    name = (model or "").strip()
    if not name or name == "不可用":
        return "不可用"
    level = normalize_effort(effort)
    if not level:
        return name
    haystack = name.lower().replace("_", "").replace("-", " ")
    if re.search(rf"(?:^|\s){re.escape(level)}(?:\s|$)", haystack):
        return name
    return f"{name} {level}"


# --- skill version ------------------------------------------------------------------
#
# The version is declared once as SKILL.md frontmatter `metadata.version` and copied
# into every 运行统计
# block so a summary can be traced back to the skill revision that produced it.
#
# Resolution has to survive the copies: cursor_usage.py and this library are installed
# into ~/.cursor/hooks/, where walking up from __file__ never reaches a SKILL.md.  So the
# order is: explicit value → env → walk up from a hint → the sidecar written at
# --ensure-hook time → the known install roots.

SKILL_MD = "SKILL.md"
SKILL_META_SIDECAR = Path.home() / ".cursor/hooks/usage/skill_meta.json"

SKILL_INSTALL_ROOTS = (
    Path.home() / ".claude/skills/video_summary",
    Path.home() / ".cursor/skills-cursor/video_summary",
    Path.home() / ".codex/skills/video_summary",
)


def _version_from_skill_md(path: Path) -> str | None:
    """Read `metadata.version` from SKILL.md, with legacy top-level compatibility."""
    try:
        text = path.read_text("utf-8", errors="ignore")
    except OSError:
        return None
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if not match:
        return None
    frontmatter = match.group(1)
    metadata = re.search(
        r"^metadata:\s*\n((?:^[ \t]+.*(?:\n|$))*)",
        frontmatter,
        flags=re.MULTILINE,
    )
    if metadata:
        found = re.search(
            r"^[ \t]+version:\s*(\S+)\s*$",
            metadata.group(1),
            flags=re.MULTILINE,
        )
        if found:
            return found.group(1).strip().strip("'\"") or None
    # Read skills installed before v2026.07.30 without forcing an immediate migration.
    legacy = re.search(r"^version:\s*(\S+)\s*$", frontmatter, flags=re.MULTILINE)
    if legacy:
        return legacy.group(1).strip().strip("'\"") or None
    return None


def find_skill_md(hint: Path | None = None) -> Path | None:
    """Locate the skill's SKILL.md by walking up from `hint`, then known roots."""
    starts: list[Path] = []
    if hint:
        starts.append(Path(hint))
    env_dir = os.environ.get("VIDEO_SUMMARY_SKILL_DIR")
    if env_dir:
        starts.append(Path(env_dir))
    for start in starts:
        try:
            start = start.expanduser().resolve()
        except OSError:
            continue
        for candidate in (start, *start.parents):
            md = candidate / SKILL_MD
            if md.is_file():
                return md
    for root in SKILL_INSTALL_ROOTS:
        md = root / SKILL_MD
        if md.is_file():
            return md
    return None


def read_skill_version(hint: Path | None = None, explicit: str | None = None) -> str:
    """Return the skill version string (no `v` prefix), or 不可用."""
    if explicit and explicit != UNAVAILABLE:
        return explicit.strip().lstrip("vV")
    env_version = os.environ.get("VIDEO_SUMMARY_SKILL_VERSION")
    if env_version:
        return env_version.strip().lstrip("vV")
    md = find_skill_md(hint)
    if md:
        version = _version_from_skill_md(md)
        if version:
            return version.lstrip("vV")
    # Sidecar: written when the Cursor hook was installed, so the hook copy of this
    # library can still name the version that produced the run.
    try:
        import json

        data = json.loads(SKILL_META_SIDECAR.read_text("utf-8"))
        version = str(data.get("version") or "").strip().lstrip("vV")
        if version:
            return version
    except Exception:
        pass
    return UNAVAILABLE


def format_skill_version(version: str | None) -> str:
    if not version or version == UNAVAILABLE:
        return UNAVAILABLE
    text = str(version).strip().lstrip("vV")
    return f"v{text}" if text else UNAVAILABLE


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, secs = divmod(seconds, 60)
    if secs:
        return f"{minutes} 分 {secs} 秒"
    return f"{minutes} 分"


def format_token_line(
    delta: dict[str, int],
    *,
    input_mode: InputMode = "subtract_cache",
) -> str:
    """Format Token 用量 line.

    - subtract_cache (Cursor / Codex): 输入（非缓存）= input_total - cache_read
    - as_is (Claude): input_total already excludes cache
    """
    if delta.get("total", -1) < 0:
        return "不可用"
    cache_read = delta.get("cache_read", -1)
    if input_mode == "as_is":
        input_non_cache = delta["input_total"] if delta.get("input_total", -1) >= 0 else -1
    elif cache_read >= 0:
        input_non_cache = delta["input_total"] - cache_read
        if input_non_cache < 0:
            input_non_cache = -1
    else:
        input_non_cache = delta["input_total"] if delta.get("input_total", -1) >= 0 else -1
    parts: list[str] = []
    parts.append(
        "输入（非缓存）"
        + (f" {input_non_cache:,}" if input_non_cache >= 0 else " 不可用")
    )
    parts.append("输出" + (f" {delta['output']:,}" if delta.get("output", -1) >= 0 else " 不可用"))
    parts.append(
        "缓存读" + (f" {delta['cache_read']:,}" if delta.get("cache_read", -1) >= 0 else " 不可用")
    )
    cw = delta.get("cache_write", -1)
    parts.append("缓存写" + (f" {cw:,}" if cw >= 0 else " 不可用"))
    parts.append("合计" + (f" {delta['total']:,}" if delta.get("total", -1) >= 0 else " 不可用"))
    return " · ".join(parts)


def format_phase_seconds(seconds: int | None) -> str:
    """Format phase wall-clock. None or negative => 不可用."""
    if seconds is None or seconds < 0:
        return "不可用"
    return format_duration(seconds)


def normalize_phase_timings(
    phase_timings: dict[str, Any] | None,
) -> dict[str, int | None] | None:
    """Keep only known phase keys. Missing keys stay absent (omit that line)."""
    if not phase_timings:
        return None
    out: dict[str, int | None] = {}
    for key in ("download_seconds", "whisper_seconds", "summary_seconds"):
        if key not in phase_timings:
            continue
        raw = phase_timings[key]
        if raw is None:
            out[key] = None
            continue
        try:
            out[key] = int(raw)
        except (TypeError, ValueError):
            out[key] = None
    return out or None


def phase_timings_from_args(
    download_seconds: int | None,
    whisper_seconds: int | None,
    summary_seconds: int | None,
) -> dict[str, int | None] | None:
    """Build phase_timings from CLI ints. Whisper omitted when flag not passed."""
    data: dict[str, int | None] = {}
    if download_seconds is not None:
        data["download_seconds"] = download_seconds
    if whisper_seconds is not None:
        data["whisper_seconds"] = whisper_seconds
    if summary_seconds is not None:
        data["summary_seconds"] = summary_seconds
    return data or None


def work_seconds_from_phases(
    phase_timings: dict[str, Any] | None,
    fallback_wall_seconds: int,
) -> int:
    """本视频实际用时 = 各阶段秒数之和；不计等待用户/其他视频。"""
    phases = normalize_phase_timings(phase_timings)
    if not phases:
        return max(0, fallback_wall_seconds)
    total = 0
    any_valid = False
    for key in ("download_seconds", "whisper_seconds", "summary_seconds"):
        if key not in phases:
            continue
        value = phases[key]
        if value is None or value < 0:
            continue
        total += value
        any_valid = True
    if any_valid:
        return total
    return max(0, fallback_wall_seconds)


def format_duration_with_phases(
    total_seconds: int,
    phase_timings: dict[str, Any] | None,
) -> str:
    """用时：X（下载用时：…，语音转写用时：…，总结用时：…）— Whisper 项可省略。"""
    phases = normalize_phase_timings(phase_timings)
    work_seconds = work_seconds_from_phases(phases, total_seconds)
    base = format_duration(work_seconds)
    if not phases:
        return base
    parts: list[str] = []
    if "download_seconds" in phases:
        parts.append(f"下载用时：{format_phase_seconds(phases['download_seconds'])}")
    if "whisper_seconds" in phases:
        parts.append(f"语音转写用时：{format_phase_seconds(phases['whisper_seconds'])}")
    if "summary_seconds" in phases:
        parts.append(f"总结用时：{format_phase_seconds(phases['summary_seconds'])}")
    if not parts:
        return base
    return f"{base}（{'，'.join(parts)}）"


def speed_window_seconds(
    *,
    speed_seconds: int | None,
    fallback_seconds: int | None = None,
    start_epoch: int,
    end_epoch: int,
) -> int:
    """Wall-clock of the window the counted output tokens were actually produced in.

    `speed_seconds` is the measured span (summary baseline snapshot → last attributed
    usage event).  The baseline is taken only after the transcript is ready, so both the
    token numerator and this denominator exclude download, Whisper, and user-consent
    waits.  If event timestamps are unavailable, use the measured summary phase; retain
    START→END only as a legacy fallback for old records without summary timing.
    """
    if speed_seconds is not None and speed_seconds > 0:
        return int(speed_seconds)
    if fallback_seconds is not None and fallback_seconds > 0:
        return int(fallback_seconds)
    return max(0, int(end_epoch) - int(start_epoch))


def format_llm_speed(
    output_tokens: int,
    window_seconds: int | None,
    note: str | None = None,
) -> str:
    """End-to-end throughput: output tokens ÷ token-attribution window → tok/s.

    Includes tool-execution time inside the summary window, so the label carries
    「（总结阶段，含工具执行）」 — it is summary-stage agent throughput, not raw
    decode speed.  Media download, Whisper, and consent waits are outside this window. A scope
    note (e.g. 「本批 3 条共用，端到端」) merges into the same parenthesis so the
    figure and its qualifier read as one statement.
    """
    # <= 0, not < 0: a run that produced a summary always emitted output tokens, so
    # zero means the usage source was unreadable.  "0.0 tok/s" would dress that up as
    # a measurement; 不可用 is the honest answer.
    if output_tokens is None or output_tokens <= 0:
        return UNAVAILABLE
    if window_seconds is None or window_seconds <= 0:
        return UNAVAILABLE
    suffix = f"（{note}，总结阶段，含工具执行）" if note else "（总结阶段，含工具执行）"
    return f"{output_tokens / window_seconds:.1f} tok/s{suffix}"


def format_stats_section(
    *,
    model: str,
    start_epoch: int,
    end_epoch: int,
    delta: dict[str, int],
    extra_line: str | None = None,
    phase_timings: dict[str, Any] | None = None,
    input_mode: InputMode = "subtract_cache",
    skill_version: str | None = None,
    speed_seconds: int | None = None,
    token_note: str | None = None,
    speed_note: str | None = None,
) -> str:
    start_s = datetime.fromtimestamp(start_epoch).strftime("%Y-%m-%d %H:%M:%S")
    end_s = datetime.fromtimestamp(end_epoch).strftime("%Y-%m-%d %H:%M:%S")
    token_line = format_token_line(delta, input_mode=input_mode)
    if token_note:
        token_line = f"{token_line}（{token_note}）"
    summary_fallback = None
    phases = normalize_phase_timings(phase_timings)
    if phases and phases.get("summary_seconds") is not None:
        summary_fallback = phases["summary_seconds"]
    window = speed_window_seconds(
        speed_seconds=speed_seconds,
        fallback_seconds=summary_fallback,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
    )
    speed_line = format_llm_speed(delta.get("output", -1), window, note=speed_note)
    if speed_note and speed_line == UNAVAILABLE:
        speed_line = f"{speed_line}（{speed_note}）"
    lines = [
        "## 运行统计",
        "",
        f"- 模型：{model}",
        f"- Skill 版本：{format_skill_version(skill_version if skill_version else read_skill_version())}",
        f"- 开始时间：{start_s}",
        f"- 完成时间：{end_s}",
        f"- 用时：{format_duration_with_phases(end_epoch - start_epoch, phase_timings)}",
        f"- Token 用量：{token_line}",
        f"- LLM 速度：{speed_line}",
    ]
    if extra_line:
        lines.append(f"- {extra_line}")
    return "\n".join(lines)


def patch_markdown_stats(md_path: Path, section: str) -> bool:
    """Replace or append the 运行统计 block without deleting later sections."""
    if not md_path.is_file():
        return False
    text = md_path.read_text("utf-8")
    pattern = r"\n---\n\n## 运行统计\n.*?(?=\n## |\Z)"
    replacement = "\n---\n\n" + section.rstrip() + "\n"
    if re.search(pattern, text, flags=re.DOTALL):
        text = re.sub(pattern, replacement, text, count=1, flags=re.DOTALL)
    else:
        text = text.rstrip() + replacement
    md_path.write_text(text, encoding="utf-8")
    return True


def unavailable_totals() -> dict[str, int]:
    return {
        "input_total": -1,
        "output": -1,
        "cache_read": -1,
        "cache_write": -1,
        "total": -1,
    }


def subtract_totals(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """Per-field delta; negative or missing → that field -1."""
    out: dict[str, int] = {}
    for key in ("input_total", "output", "cache_read", "cache_write"):
        cur = current.get(key, -1)
        base = baseline.get(key, -1)
        if cur < 0 or base < 0:
            out[key] = -1
            continue
        diff = cur - base
        out[key] = diff if diff >= 0 else -1
    comps = [out[k] for k in ("input_total", "output", "cache_read", "cache_write")]
    if all(c >= 0 for c in comps):
        # Prefer recomputed sum so Claude (no separate total in delta) stays consistent.
        out["total"] = sum(comps)
    else:
        cur_t = current.get("total", -1)
        base_t = baseline.get("total", -1)
        if cur_t >= 0 and base_t >= 0 and cur_t - base_t >= 0:
            out["total"] = cur_t - base_t
        else:
            out["total"] = -1
    return out


def add_phase_timing_args(parser: Any) -> None:
    parser.add_argument("--download-seconds", type=int, default=None)
    parser.add_argument("--whisper-seconds", type=int, default=None)
    parser.add_argument("--summary-seconds", type=int, default=None)


# --- workspace path → directory key -------------------------------------------------
#
# Claude Code and Cursor both slugify the workspace path into a directory name, but the
# exact rule is not documented and differs between them.  Verified on macOS:
#
#   Claude Code  /Users/a/.claude/skills          -> -Users-a--claude-skills
#                /Users/a/…/AI_task/个人/video_summary
#                                                 -> -Users-a-…-AI-task----video-summary
#     => leading "-", then EVERY character outside [A-Za-z0-9-] becomes one "-"
#        (so a 2-char CJK segment yields 2 dashes, a leading dot yields 1).
#
# Cursor's handling of non-ASCII could not be verified locally, so callers must try
# several candidates and fall back to scanning.  Never assume a single rule is right.


def slug_per_char(path: str) -> str:
    """One dash per non-[A-Za-z0-9-] character (Claude Code's verified rule)."""
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def slug_collapse_runs(path: str) -> str:
    """One dash per RUN of non-[A-Za-z0-9-] characters."""
    return re.sub(r"[^A-Za-z0-9-]+", "-", path)


def path_key_variants(cwd: Path, *, leading_dash: bool) -> list[str]:
    """Candidate directory keys for a workspace path, most likely first.

    Returns de-duplicated variants covering both slugification rules, because a
    single hard-coded rule silently resolves to a non-existent directory (which is
    how token/model stats used to degrade to 不可用 without any error).
    """
    text = str(Path(cwd).expanduser().resolve())
    if os.name == "nt":
        text = text.replace("\\", "/")
        if len(text) >= 2 and text[1] == ":":
            text = text[0] + text[2:]
    text = text.strip("/")
    prefix = "-" if leading_dash else ""
    out: list[str] = []
    for slug in (slug_per_char(text.replace("/", "-")), slug_collapse_runs(text.replace("/", "-"))):
        for candidate in (prefix + slug, prefix + slug.strip("-"), slug):
            if candidate and candidate not in out:
                out.append(candidate)
    return out


def newest_by_mtime(paths: Any) -> Path | None:
    """Newest existing file from an iterable of paths; None when nothing matches."""
    files = [p for p in paths if isinstance(p, Path) and p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def print_doctor(rows: list[tuple[str, str]], essential: tuple[str, ...] = ()) -> bool:
    """Print `key=value` diagnostics; verdict depends only on `essential` keys.

    Informational rows (CODEX_HOME, THREAD_ID, PROJECT_DIR_KEY …) are shown for
    debugging but must not flip the verdict — an empty THREAD_ID is normal when the
    session was resolved by the fallback, and reporting that as degraded would send
    callers chasing a non-problem.
    """
    ok = True
    for key, value in rows:
        text = "不可用" if value in (None, "") else str(value)
        if text == "不可用" and (not essential or key in essential):
            ok = False
        print(f"{key}={text}")
    print(f"DOCTOR={'ok' if ok else 'degraded'}")
    return ok
