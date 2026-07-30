#!/usr/bin/env python3
"""Read Cursor agent token usage and model for video_summary stats."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

def _bootstrap_runtime_stats_lib() -> None:
    """Make runtime_stats_lib importable when this file is copied to ~/.cursor/hooks/."""
    try:
        import runtime_stats_lib  # noqa: F401
        return
    except ImportError:
        pass
    here = Path(__file__).resolve().parent
    candidates = [
        here,
        Path.home() / ".cursor/hooks",
        Path.home() / ".agents/skills/video_summary/scripts",
        Path.home() / ".claude/skills/video_summary/scripts",
    ]
    for cand in candidates:
        if not (cand / "runtime_stats_lib.py").is_file():
            continue
        path = str(cand)
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            import runtime_stats_lib  # noqa: F401
            return
        except ImportError:
            continue
    raise ImportError(
        "runtime_stats_lib not found; re-run: "
        "python3 <skill>/scripts/cursor_usage.py --ensure-hook --skill-dir <skill>"
    )


_bootstrap_runtime_stats_lib()

from runtime_stats_lib import (  # noqa: E402
    compose_model_label,
    format_duration,
    format_duration_with_phases,
    format_llm_speed,
    format_phase_seconds,
    format_skill_version,
    format_stats_section,
    format_token_line,
    normalize_effort,
    normalize_phase_timings,
    patch_markdown_stats,
    path_key_variants,
    phase_timings_from_args,
    print_doctor,
    read_skill_version,
    speed_window_seconds,
    work_seconds_from_phases,
)


def number(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def debug_log(message: str) -> None:
    path = Path.home() / ".cursor/hooks/usage/hook-debug.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")
    except Exception:
        pass


def load_json_lenient(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text("utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        try:
            data, _ = json.JSONDecoder().raw_decode(text.lstrip())
            return data if isinstance(data, dict) else None
        except Exception:
            return None


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def transcript_conversation_id() -> str | None:
    """Return the conversation ID encoded in the current transcript path, if any.

    Cursor CLI can leave ``CURSOR_CONVERSATION_ID`` set to the preceding task
    while ``CURSOR_TRANSCRIPT_PATH`` already points at the task currently being
    executed.  The transcript is therefore the authoritative source when it is
    available.
    """
    for env_name in ("CURSOR_TRANSCRIPT_PATH", "AGENT_TRANSCRIPTS"):
        raw = os.environ.get(env_name)
        if not raw:
            continue
        path = Path(raw)
        if path.is_file():
            conv = conv_from_transcript_path(str(path))
            if conv:
                return conv
        elif path.is_dir():
            candidates = sorted(
                [*path.glob("*/agent-transcripts/*/*.jsonl"), *path.glob("*/*.jsonl")],
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for candidate in candidates:
                conv = conv_from_transcript_path(str(candidate))
                if conv:
                    return conv
    return None


def workspace_transcript_roots() -> list[Path]:
    """agent-transcripts dirs for this workspace, most specific first.

    Cursor's slugification of non-ASCII paths is undocumented and could not be
    verified locally, so try every plausible key instead of hard-coding one rule
    that silently resolves to a missing directory.
    """
    projects = Path.home() / ".cursor/projects"
    roots: list[Path] = []
    for key in path_key_variants(Path.cwd(), leading_dash=False):
        root = projects / key / "agent-transcripts"
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def newest_workspace_transcript_id(*, any_workspace: bool = False) -> str | None:
    """Find the newest transcript for this workspace when CLI omits its path."""
    roots = workspace_transcript_roots()
    if not roots and any_workspace:
        projects = Path.home() / ".cursor/projects"
        if projects.is_dir():
            roots = [p for p in projects.glob("*/agent-transcripts") if p.is_dir()]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(root.glob("*/*.jsonl"))
    for candidate in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        if any_workspace and (time.time() - candidate.stat().st_mtime) > 6 * 3600:
            continue
        conv = conv_from_transcript_path(str(candidate))
        if conv:
            return conv
    return None


def conversation_id() -> str | None:
    """Resolve the active Cursor conversation without trusting a stale CLI env var."""
    direct = transcript_conversation_id()
    if direct:
        return direct
    env_conv = os.environ.get("CURSOR_CONVERSATION_ID") or None
    newest = newest_workspace_transcript_id()
    if newest and newest != env_conv:
        env_path = transcript_path(env_conv)
        newest_path = transcript_path(newest)
        if not env_path or (newest_path and newest_path.stat().st_mtime > env_path.stat().st_mtime):
            return newest
    # Last resort: the workspace slug could not be resolved from cwd at all.
    return env_conv or newest or newest_workspace_transcript_id(any_workspace=True)


def conversation_id_source() -> str:
    if transcript_conversation_id():
        return "transcript"
    env_conv = os.environ.get("CURSOR_CONVERSATION_ID") or None
    newest = newest_workspace_transcript_id()
    if newest and newest != env_conv:
        env_path = transcript_path(env_conv)
        newest_path = transcript_path(newest)
        if not env_path or (newest_path and newest_path.stat().st_mtime > env_path.stat().st_mtime):
            return "workspace_transcript"
    if os.environ.get("CURSOR_CONVERSATION_ID"):
        return "environment"
    if newest:
        return "workspace_transcript"
    # Mirror conversation_id()'s last-resort fallback so the two never disagree.
    if newest_workspace_transcript_id(any_workspace=True):
        return "any_workspace_transcript"
    return "unavailable"


def transcript_path(conv: str | None) -> Path | None:
    env = os.environ.get("CURSOR_TRANSCRIPT_PATH") or os.environ.get("AGENT_TRANSCRIPTS")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        if p.is_dir() and conv:
            cand = p / conv / f"{conv}.jsonl"
            if cand.is_file():
                return cand
    if not conv:
        return None
    root = Path.home() / ".cursor/projects"
    matches = sorted(root.glob(f"*/agent-transcripts/{conv}/{conv}.jsonl"), key=lambda x: x.stat().st_mtime)
    return matches[-1] if matches else None


def ledger_path(conv: str | None) -> Path | None:
    if not conv:
        return None
    return Path.home() / ".cursor/hooks/usage" / f"{conv}.json"


def context_path(conv: str | None) -> Path | None:
    if not conv:
        return None
    return Path.home() / ".cursor/hooks/usage/context" / f"{conv}.json"


def conv_from_transcript_path(path: str | None) -> str | None:
    if not path:
        return None
    parts = Path(path).parts
    if "agent-transcripts" not in parts:
        return None
    idx = parts.index("agent-transcripts")
    if idx + 1 >= len(parts):
        return None
    return parts[idx + 1]


def read_context_snapshot(conv: str | None) -> dict[str, Any] | None:
    path = context_path(conv)
    if not path:
        return None
    return load_json_lenient(path)


def context_to_totals(snapshot: dict[str, Any] | None) -> dict[str, int]:
    if not snapshot:
        return empty_totals()
    # Prefer accumulated session totals from current_usage ingestion.
    if snapshot.get("source") == "statusline_cumulative" or snapshot.get("session_input_total") is not None:
        inp = number(snapshot.get("session_input_total", snapshot.get("input_total")))
        out = number(snapshot.get("session_output_total", snapshot.get("output_total")))
        cr_raw = snapshot.get("session_cache_read")
        cw_raw = snapshot.get("session_cache_write")
        return {
            "input_total": inp,
            "output": out,
            "cache_read": number(cr_raw) if cr_raw is not None else -1,
            "cache_write": number(cw_raw) if cw_raw is not None else -1,
            "total": inp + out,
        }
    # No session_* fields => the statusline never saw a `current_usage` block, so the
    # only numbers here are context-window OCCUPANCY (`context_window_size` ×
    # `used_percentage`), not token usage.  Reporting occupancy as usage produced the
    # classic broken block "输入（非缓存）14,080 · 输出 0 · 缓存读 不可用 · LLM 速度 0.0 tok/s":
    # occupancy grows during the turn (so the delta looks "usable"), while output stays
    # 0 because it has no occupancy counterpart.  Refuse it and let the stop hook win.
    return unavailable_totals()


def unavailable_totals() -> dict[str, int]:
    return {
        "input_total": -1,
        "output": -1,
        "cache_read": -1,
        "cache_write": -1,
        "total": -1,
    }


def is_cli_runtime() -> bool:
    return Path.home().joinpath(".cursor/cli-config.json").is_file()


def read_ledger(conv: str | None) -> dict[str, Any] | None:
    path = ledger_path(conv)
    if not path:
        return None
    return load_json_lenient(path)


# --- sub-agent → parent conversation --------------------------------------------------
#
# A Cursor sub-agent runs under its own conversation id, but stop / afterAgentResponse
# only fire for the conversation the user is talking to.  A pending job keyed on the
# sub-agent id therefore waits for a ledger that is never written, which is how batches
# ended up with a permanent「Token 用量：不可用」.
#
# ~/.cursor/chats/<workspace>/<conv>/store.db carries the link: its `meta` row holds
# hex-encoded JSON with subagentInfo.parentAgentId / rootParentAgentId.

_CHAT_META_CACHE: dict[str, dict[str, Any] | None] = {}


def chat_store_meta(conv: str | None) -> dict[str, Any] | None:
    """Decode the meta blob of a conversation's chat store, if present."""
    if not conv:
        return None
    if conv in _CHAT_META_CACHE:
        return _CHAT_META_CACHE[conv]
    result = _read_chat_store_meta(conv)
    _CHAT_META_CACHE[conv] = result
    return result


def _read_chat_store_meta(conv: str) -> dict[str, Any] | None:
    root = Path.home() / ".cursor/chats"
    for db in root.glob(f"*/{conv}/store.db"):
        try:
            connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            row = connection.execute("select value from meta limit 1").fetchone()
        except sqlite3.Error:
            row = None
        finally:
            connection.close()
        if not row or not row[0]:
            continue
        raw = row[0]
        if isinstance(raw, str):
            # Stored as hex-encoded UTF-8 JSON; tolerate a plain-JSON variant too.
            try:
                raw = bytes.fromhex(raw)
            except ValueError:
                raw = raw.encode("utf-8", "ignore")
        try:
            data = json.loads(raw.decode("utf-8", "ignore"))
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def parent_conversation(conv: str | None) -> str | None:
    meta = chat_store_meta(conv)
    info = (meta or {}).get("subagentInfo")
    if not isinstance(info, dict):
        return None
    for key in ("rootParentAgentId", "parentAgentId"):
        value = info.get(key)
        if isinstance(value, str) and value and value != conv:
            return value
    return None


def conversation_lineage(conv: str | None, *, max_depth: int = 6) -> list[str]:
    """[conv, parent, grandparent, …] — the ledgers that may hold this run's usage."""
    chain: list[str] = []
    current = conv
    while current and current not in chain and len(chain) < max_depth:
        chain.append(current)
        current = parent_conversation(current)
    return chain


def usage_convs(conv: str | None) -> list[str]:
    """Lineage entries that actually have a ledger file, own conversation first."""
    return [c for c in conversation_lineage(conv) if (ledger_path(c) or Path("/")).is_file()]


def root_usage_conv(conv: str | None) -> str | None:
    """The conversation whose ledger the stop hook writes for this run."""
    for candidate in conversation_lineage(conv):
        ledger = read_ledger(candidate)
        if ledger and (ledger.get("events") or []):
            return candidate
    lineage = conversation_lineage(conv)
    return lineage[-1] if lineage else conv


def window_events(
    convs: list[str],
    start_epoch: int,
    end_epoch: int,
    *,
    hook_grace_s: int = 60,
) -> list[tuple[str, dict[str, Any]]]:
    """Ledger events in [start, end+grace] across a lineage, deduped by generation."""
    upper = number(end_epoch) + max(0, hook_grace_s)
    seen: set[str] = set()
    out: list[tuple[str, dict[str, Any]]] = []
    for conv in convs:
        ledger = read_ledger(conv)
        for event in (ledger or {}).get("events") or []:
            if not isinstance(event, dict):
                continue
            ts = number(event.get("ts"))
            if ts < start_epoch or ts > upper:
                continue
            key = str(event.get("generation_id") or f"{conv}:{ts}")
            if key in seen:
                continue
            seen.add(key)
            out.append((key, event))
    return out


def totals_from_events(events: list[tuple[str, dict[str, Any]]]) -> dict[str, int]:
    if not events:
        return unavailable_totals()
    totals = empty_totals()
    for _key, event in events:
        for field in ("input_total", "output", "cache_read", "cache_write"):
            totals[field] += number(event.get(field))
    if totals["input_total"] <= 0 and totals["output"] <= 0:
        return unavailable_totals()
    totals["total"] = totals["input_total"] + totals["output"]
    return totals


def empty_totals() -> dict[str, int]:
    return {"input_total": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}


def totals_from_ledger(ledger: dict[str, Any] | None) -> dict[str, int]:
    if not ledger or not isinstance(ledger.get("totals"), dict):
        return empty_totals()
    t = ledger["totals"]
    totals = {
        "input_total": number(t.get("input_total")),
        "output": number(t.get("output")),
        "cache_read": number(t.get("cache_read")),
        "cache_write": number(t.get("cache_write")),
        "total": number(t.get("total")) or number(t.get("input_total")) + number(t.get("output")),
    }
    return totals


def totals_from_ledger_since(
    ledger: dict[str, Any] | None,
    start_epoch: int,
    end_epoch: int | None = None,
    *,
    hook_grace_s: int = 60,
) -> dict[str, int]:
    """Rebuild a run's totals from recorded events in [start, end+grace].

    This is only a recovery path for old Markdown files whose baseline was
    bound to a stale Cursor conversation.  It deliberately uses the ledger
    event timestamps rather than another conversation's cumulative totals.

    ``end_epoch`` (when set) caps the window so later turns in the same chat
    are not attributed to this video.  ``hook_grace_s`` allows the stop hook
    to land shortly after the agent recorded END (default 60s; keep tight so
    follow-up Q&A in the same conversation is excluded).
    """
    if not ledger or not isinstance(ledger.get("events"), list):
        return unavailable_totals()
    totals = empty_totals()
    found = False
    upper = None if end_epoch is None else number(end_epoch) + max(0, hook_grace_s)
    for event in ledger["events"]:
        if not isinstance(event, dict):
            continue
        ts = number(event.get("ts"))
        if ts < start_epoch:
            continue
        if upper is not None and ts > upper:
            continue
        found = True
        for key in ("input_total", "output", "cache_read", "cache_write"):
            totals[key] += number(event.get(key))
    if not found or totals["input_total"] <= 0 and totals["output"] <= 0:
        return unavailable_totals()
    totals["total"] = totals["input_total"] + totals["output"]
    return totals


USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
    "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
    "cached_input_tokens", "cache_read_tokens", "cache_write_tokens",
)


def add_usage(totals: dict[str, int], usage: dict[str, Any]) -> None:
    mapping = {
        "input_total": ("input_tokens", "inputTokens", "prompt_tokens"),
        "output": ("output_tokens", "outputTokens", "completion_tokens"),
        "cache_read": ("cache_read_input_tokens", "cacheReadInputTokens", "cached_input_tokens", "cache_read_tokens", "cacheReadTokens"),
        "cache_write": ("cache_creation_input_tokens", "cacheCreationInputTokens", "cache_write_tokens", "cacheWriteTokens"),
    }
    for dest, keys in mapping.items():
        for key in keys:
            val = usage.get(key)
            if isinstance(val, int) and val >= 0:
                totals[dest] += val
                break
    totals["total"] = totals["input_total"] + totals["output"]


def scan_transcript(path: Path) -> dict[str, int]:
    totals = empty_totals()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            usage = obj.get("usage") or obj.get("tokenUsage")
            if isinstance(usage, dict):
                add_usage(totals, usage)
            if any(k in obj for k in USAGE_KEYS):
                add_usage(totals, obj)
            for val in obj.values():
                walk(val)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    try:
        for line in path.read_text("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                walk(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return totals


def worker_result_event(path: Path) -> dict[str, Any] | None:
    """Last `result` event from a nested `cursor-agent -p --output-format (stream-)json` capture.

    A nested worker run is the only Cursor path that reports real per-generation
    usage mid-turn: hooks stay silent for headless runs, but the final result
    event carries `usage` (inputTokens/outputTokens/cacheReadTokens/…) and
    `duration_api_ms` for exactly one generation.
    """
    try:
        text = path.read_text("utf-8", errors="ignore")
    except OSError:
        return None
    result: dict[str, Any] | None = None
    try:
        whole = json.loads(text)
        candidates = whole if isinstance(whole, list) else [whole]
    except json.JSONDecodeError:
        candidates = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for obj in candidates:
        if isinstance(obj, dict) and obj.get("type") == "result":
            result = obj
    return result


def totals_from_worker_result(result: dict[str, Any]) -> dict[str, int]:
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return unavailable_totals()
    totals = empty_totals()
    add_usage(totals, usage)
    if totals["output"] <= 0:
        return unavailable_totals()
    return totals


def totals_from_sources(conv: str | None) -> tuple[dict[str, int], str]:
    ledger = read_ledger(conv)
    if ledger:
        totals = totals_from_ledger(ledger)
        if totals.get("total", 0) > 0:
            return totals, "ledger"

    context = read_context_snapshot(conv)
    if context:
        totals = context_to_totals(context)
        if totals.get("total", 0) > 0:
            return totals, "context"

    if ledger:
        return totals_from_ledger(ledger), "ledger"

    transcript = transcript_path(conv)
    if transcript:
        totals = scan_transcript(transcript)
        if any(v > 0 for v in totals.values()):
            return totals, f"transcript:{transcript}"
    return unavailable_totals(), "unavailable"


def subtract(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in ("input_total", "output", "cache_read", "cache_write", "total"):
        cur = current.get(key, -1)
        # A baseline snapshotted before any usage existed stores the -1 不可用
        # sentinel; subtracting it would add a phantom +1 to every field.
        base = max(0, baseline.get(key, 0))
        if cur < 0:
            out[key] = -1
        else:
            out[key] = max(0, cur - base)
    return out


def has_usable_delta(delta: dict[str, int]) -> bool:
    """A delta is only usable once it carries real *output* tokens.

    `total > 0` alone is not enough: an input-only delta (output == 0) means the
    source has not caught up with this run yet.  Accepting it wrote a bogus stats
    block AND cleared the pending job, so the correct ledger numbers arriving with
    the stop hook could never land.  Output is also the LLM 速度 numerator, so a
    zero here can only ever yield a meaningless "0.0 tok/s".
    """
    return delta.get("total", -1) > 0 and delta.get("output", -1) > 0


def context_delta(conv: str | None, baseline_file: Path | None) -> tuple[dict[str, int], str]:
    base = load_baseline(baseline_file) if baseline_file else None
    base_ctx = (base or {}).get("context") or {}
    current = read_context_snapshot(conv)
    if not current:
        return unavailable_totals(), "unavailable"
    delta = subtract(context_to_totals(current), context_to_totals(base_ctx))
    if has_usable_delta(delta):
        return delta, "context"
    return unavailable_totals(), "unavailable"


def first_number(obj: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in obj and obj[key] is not None and obj[key] != "":
            if isinstance(obj[key], (int, float, str)):
                return number(obj[key])
    return None


def extract_usage_dict(payload: dict[str, Any]) -> dict[str, int] | None:
    """Pull token fields from a hook/statusline payload. Returns None if absent."""
    candidates: list[dict[str, Any]] = [payload]
    for key in ("usage", "tokenUsage", "token_usage", "tokenusage", "result"):
        val = payload.get(key)
        if isinstance(val, dict):
            candidates.append(val)
    nested = payload.get("message")
    if isinstance(nested, dict):
        candidates.append(nested)
        for key in ("usage", "tokenUsage"):
            val = nested.get(key)
            if isinstance(val, dict):
                candidates.append(val)

    for cand in candidates:
        inp = first_number(
            cand,
            ("input_tokens", "inputTokens", "input_total", "prompt_tokens", "promptTokens"),
        )
        out = first_number(
            cand,
            ("output_tokens", "outputTokens", "output_total", "completion_tokens", "completionTokens"),
        )
        if inp is None and out is None:
            continue
        cr = first_number(
            cand,
            (
                "cache_read_tokens",
                "cacheReadInputTokens",
                "cache_read_input_tokens",
                "cached_input_tokens",
                "cache_read",
            ),
        )
        cw = first_number(
            cand,
            (
                "cache_write_tokens",
                "cacheCreationInputTokens",
                "cache_creation_input_tokens",
                "cache_write",
            ),
        )
        return {
            "input_total": inp or 0,
            "output": out or 0,
            "cache_read": cr or 0,
            "cache_write": cw or 0,
        }
    return None


def usage_fingerprint(usage: dict[str, int]) -> tuple[int, int, int, int]:
    return (
        usage.get("input_total", 0),
        usage.get("output", 0),
        usage.get("cache_read", 0),
        usage.get("cache_write", 0),
    )


def ingest_hook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest stop/afterAgentResponse/sessionEnd hook JSON; always attempt pending flush."""
    conv = (
        payload.get("conversation_id")
        or payload.get("session_id")
        or conv_from_transcript_path(str(payload.get("transcript_path") or ""))
    )
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    result: dict[str, Any] = {"conversation_id": conv, "event": event, "recorded": False, "flushed": 0}

    if not conv or event not in {"stop", "sessionEnd", "afterAgentResponse"}:
        result["status"] = "ignored"
        return result

    usage = extract_usage_dict(payload)
    generation_id = str(payload.get("generation_id") or payload.get("generationId") or "") or None
    model = payload.get("model") or payload.get("model_id")
    path = ledger_path(conv)
    assert path is not None
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    recorded = False
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            data = load_json_lenient(path) or {
                "conversation_id": conv,
                "events": [],
                "totals": empty_totals(),
            }
            data.setdefault("events", [])
            data.setdefault("totals", empty_totals())
            events: list[dict[str, Any]] = list(data.get("events") or [])

            if usage and (usage["input_total"] > 0 or usage["output"] > 0):
                skip = False
                fp = usage_fingerprint(usage)
                if generation_id:
                    for ev in events:
                        if ev.get("generation_id") == generation_id:
                            skip = True
                            break
                if not skip:
                    # Also skip identical token tuple within 5s (stop + afterAgentResponse race).
                    now = int(time.time())
                    for ev in reversed(events[-5:]):
                        ev_fp = (
                            number(ev.get("input_total")),
                            number(ev.get("output")),
                            number(ev.get("cache_read")),
                            number(ev.get("cache_write")),
                        )
                        if ev_fp == fp and abs(now - number(ev.get("ts"))) <= 5:
                            skip = True
                            break
                if not skip:
                    t = data["totals"]
                    for key, val in usage.items():
                        t[key] = number(t.get(key)) + val
                    t["total"] = number(t.get("input_total")) + number(t.get("output"))
                    concrete_model = str(model) if model and is_concrete_model(str(model)) else None
                    if concrete_model:
                        data["model"] = concrete_model
                    data["updated_at"] = int(time.time())
                    event_record = {
                        "ts": data["updated_at"],
                        "hook": event,
                        "generation_id": generation_id,
                        **usage,
                    }
                    if concrete_model:
                        event_record["model"] = concrete_model
                    events.append(event_record)
                    data["events"] = events
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
                    tmp.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(tmp, path)
                    recorded = True
                    debug_log(
                        f"event={event} conv={conv} recorded=1 "
                        f"in={usage['input_total']} out={usage['output']} gen={generation_id or '-'}"
                    )
                else:
                    debug_log(f"event={event} conv={conv} recorded=0 deduped gen={generation_id or '-'}")
            else:
                keys = ",".join(sorted(payload.keys()))
                debug_log(f"event={event} conv={conv} recorded=0 no_token_fields keys={keys}")
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    flushed = flush_pending_stats(conv)
    upgraded = upgrade_settled_notes(conv)
    result.update(
        {"status": "ok", "recorded": recorded, "flushed": flushed, "upgraded": upgraded}
    )
    return result


def ingest_context_payload(payload: dict[str, Any]) -> Path | None:
    conv = payload.get("conversation_id") or conv_from_transcript_path(
        str(payload.get("transcript_path") or "")
    )
    if not conv:
        session_id = str(payload.get("session_id") or "")
        if session_id:
            conv = session_id
    if not conv:
        return None

    cw = payload.get("context_window") or {}
    if not isinstance(cw, dict):
        cw = {}

    prev = read_context_snapshot(conv) or {}
    session_in = number(prev.get("session_input_total"))
    session_out = number(prev.get("session_output_total"))
    session_cr = number(prev.get("session_cache_read"))
    session_cw = number(prev.get("session_cache_write"))
    last_fp = prev.get("last_current_usage_fp")

    current_usage = cw.get("current_usage")
    added = False
    if isinstance(current_usage, dict):
        usage = extract_usage_dict(current_usage) or extract_usage_dict(
            {
                "input_tokens": current_usage.get("input_tokens"),
                "output_tokens": current_usage.get("output_tokens"),
                "cache_read_input_tokens": current_usage.get("cache_read_input_tokens")
                or current_usage.get("cacheReadInputTokens"),
                "cache_creation_input_tokens": current_usage.get("cache_creation_input_tokens")
                or current_usage.get("cacheCreationInputTokens"),
            }
        )
        if usage and (usage["input_total"] > 0 or usage["output"] > 0):
            fp = list(usage_fingerprint(usage))
            if fp != last_fp:
                # current_usage is per-call; accumulate across distinct snapshots.
                turn_input = usage["input_total"] + usage["cache_read"] + usage["cache_write"]
                session_in += turn_input
                session_out += usage["output"]
                session_cr += usage["cache_read"]
                session_cw += usage["cache_write"]
                last_fp = fp
                added = True

    inp = cw.get("total_input_tokens")
    out = cw.get("total_output_tokens")
    if not isinstance(inp, int) or inp < 0:
        used_pct = cw.get("used_percentage")
        size = cw.get("context_window_size")
        if isinstance(used_pct, (int, float)) and isinstance(size, int) and size > 0:
            inp = int(size * float(used_pct) / 100.0)
        else:
            inp = number(prev.get("input_total"))
    if not isinstance(out, int) or out < 0:
        out = number(prev.get("output_total"))

    data = {
        "conversation_id": conv,
        "session_id": payload.get("session_id"),
        "input_total": inp,
        "output_total": out,
        "context_window_size": cw.get("context_window_size"),
        "used_percentage": cw.get("used_percentage"),
        "session_input_total": session_in if added or prev.get("session_input_total") is not None else None,
        "session_output_total": session_out if added or prev.get("session_output_total") is not None else None,
        "session_cache_read": session_cr if added or prev.get("session_cache_read") is not None else None,
        "session_cache_write": session_cw if added or prev.get("session_cache_write") is not None else None,
        "last_current_usage_fp": last_fp,
        "updated_at": int(time.time()),
        "source": "statusline_cumulative" if (added or prev.get("source") == "statusline_cumulative") else "statusline",
    }
    # Drop nulls for cleaner JSON
    data = {k: v for k, v in data.items() if v is not None}
    path = context_path(conv)
    assert path is not None
    atomic_write_json(path, data)
    if added:
        flush_pending_stats(conv)
    return path


def ledger_has_new_usage(
    conv: str | None,
    base_totals: dict[str, int],
    base_events: int,
) -> bool:
    ledger = read_ledger(conv)
    if not ledger:
        return False
    if len(ledger.get("events") or []) > base_events:
        return True
    current = totals_from_ledger(ledger)
    return any(current.get(k, 0) > base_totals.get(k, 0) for k in ("input_total", "output", "total"))


def pending_dir() -> Path:
    return Path.home() / ".cursor/hooks/usage/pending"


def pending_job_path(conv: str, markdown: Path | str | None = None) -> Path:
    """Job file for one (conversation, markdown) pair.

    Keyed by markdown too: a batch summarises several videos inside a single Cursor
    turn, and a conversation-only key made video N overwrite video N-1, so every
    Markdown but the last kept its 不可用 placeholder forever.
    """
    if markdown is None:
        return pending_dir() / f"{conv}.json"
    digest = hashlib.sha1(str(Path(markdown).resolve()).encode("utf-8")).hexdigest()[:12]
    return pending_dir() / f"{conv}__{digest}.json"


def pending_job_paths(conv: str) -> list[Path]:
    """Every pending job for a conversation, including the legacy single-file name."""
    directory = pending_dir()
    if not directory.is_dir():
        return []
    paths = sorted(directory.glob(f"{conv}__*.json"))
    legacy = directory / f"{conv}.json"
    if legacy.is_file():
        paths.append(legacy)
    return paths


def pending_jobs_for(conv: str) -> list[tuple[Path, dict[str, Any]]]:
    """Jobs this conversation's ledger can settle — its own plus its sub-agents'.

    A sub-agent job is filed under the sub-agent's id but its tokens only ever reach
    the parent's ledger, so matching by filename alone left those jobs stranded.  Match
    on the recorded `usage_conversation_id` as well, and fall back to resolving the
    lineage for jobs written by an older version of this script.
    """
    directory = pending_dir()
    if not directory.is_dir():
        return []
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        job = load_json_lenient(path)
        if not job:
            continue
        own = str(job.get("conversation_id") or "")
        usage = str(job.get("usage_conversation_id") or "")
        if conv in (own, usage):
            out.append((path, job))
            continue
        if not usage and own and root_usage_conv(own) == conv:
            out.append((path, job))
    return out


def write_pending_job(
    *,
    conv: str,
    baseline_file: Path,
    baseline_events: int,
    markdown: Path,
    start_epoch: int,
    end_epoch: int,
    model: str,
    extra_line: str | None = None,
    phase_timings: dict[str, Any] | None = None,
) -> Path:
    base = load_baseline(baseline_file) or {}
    job = {
        "conversation_id": conv,
        # Where the tokens will land.  Kept separate from conversation_id so a sub-agent
        # job can be flushed by the parent's stop hook instead of waiting forever.
        "usage_conversation_id": base.get("usage_conversation_id") or root_usage_conv(conv),
        "baseline_file": str(baseline_file.resolve()),
        "baseline_events": baseline_events,
        "baseline_totals": base.get("totals") or empty_totals(),
        "baseline_context": base.get("context") or {},
        "baseline_epoch": number(base.get("saved_at")) or start_epoch,
        "markdown": str(markdown.resolve()),
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "model": model,
        "extra_line": extra_line,
        "phase_timings": normalize_phase_timings(phase_timings),
        "skill_version": base.get("skill_version")
        or read_skill_version(Path(__file__).resolve().parent),
        "created_at": int(time.time()),
    }
    path = pending_job_path(conv, markdown)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, job)
    return path


def load_pending_job(conv: str) -> dict[str, Any] | None:
    for path in pending_job_paths(conv):
        job = load_json_lenient(path)
        if job:
            return job
    return None


def remove_pending_job(conv: str, markdown: Path | str | None = None) -> None:
    """Drop one job (when `markdown` is given) or every job for the conversation."""
    targets = [pending_job_path(conv, markdown)] if markdown else pending_job_paths(conv)
    for path in targets:
        if path.is_file():
            path.unlink()


def settled_dir() -> Path:
    return pending_dir().parent / "settled"


def settled_marker_path(conv: str, markdown: Path | str) -> Path:
    digest = hashlib.sha1(str(Path(markdown).resolve()).encode("utf-8")).hexdigest()[:12]
    return settled_dir() / f"{conv}__{digest}.json"


def write_settled_marker(
    job: dict[str, Any], note: str, reason: str, speed_note: str | None = None
) -> None:
    """Remember that this (conversation, markdown) was settled as unsplittable.

    The marker is what stops ensure_stats / finalize from re-registering a pending
    job for an already-settled markdown: once the sibling jobs are gone, a lone
    re-registered job looks unambiguous and swallows the whole generation's delta —
    that is exactly how one video of a batch ended up with the batch's entire usage
    and an inflated LLM 速度.
    """
    conv = str(job.get("conversation_id") or "")
    markdown = str(job.get("markdown") or "")
    if not conv or not markdown:
        return
    path = settled_marker_path(conv, markdown)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path,
        {
            "job": job,
            "note": note,
            "speed_note": speed_note,
            "reason": reason,
            "settled_at": int(time.time()),
        },
    )
    # Markers only matter while their batch's conversation can still be flushed.
    cutoff = time.time() - 7 * 24 * 3600
    for old in settled_dir().glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            continue


def settled_marker(conv: str, markdown: Path | str) -> dict[str, Any] | None:
    return load_json_lenient(settled_marker_path(conv, markdown))


def settled_markers_for(conv: str) -> list[tuple[Path, dict[str, Any]]]:
    directory = settled_dir()
    if not directory.is_dir():
        return []
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob(f"{conv}__*.json")):
        data = load_json_lenient(path)
        if data and isinstance(data.get("job"), dict):
            out.append((path, data))
    return out


def upgrade_settled_notes(conv: str | None) -> int:
    """Re-patch settled markdowns once the aggregate event carries batch totals.

    Batches are often settled mid-turn, before the stop hook records any usage, so
    their note lacks the 本批合计 figures.  Once the event lands, rewrite each
    settled markdown with the enriched note; the stored note keeps this idempotent.
    """
    if not conv:
        return 0
    markers = settled_markers_for(conv)
    if not markers:
        return 0
    jobs = [data["job"] for _path, data in markers]
    note = batch_token_note(jobs)
    if "本批合计" not in note:
        return 0
    speed_note = batch_speed_note(jobs)
    upgraded = 0
    for path, data in markers:
        if data.get("note") == note and data.get("speed_note") == speed_note:
            continue
        if apply_stats_job(
            data["job"], token_note=note, speed_note=speed_note, force_unavailable=True
        ):
            data["note"] = note
            data["speed_note"] = speed_note
            atomic_write_json(path, data)
            upgraded += 1
    return upgraded


def job_usage_convs(job: dict[str, Any]) -> list[str]:
    """Ledgers that could hold this job's tokens: its own chat plus its ancestors."""
    convs: list[str] = []
    for candidate in (job.get("conversation_id"), job.get("usage_conversation_id")):
        text = str(candidate or "")
        if not text:
            continue
        for entry in conversation_lineage(text):
            if entry not in convs:
                convs.append(entry)
    return convs


def job_speed_seconds(job: dict[str, Any], last_event_ts: int | None) -> int | None:
    """Wall-clock of the window the counted output tokens were produced in.

    Starts at the baseline snapshot (nothing before it is in the delta) and ends at the
    last usage event attributed to the job — the stop hook fires after the agent
    recorded END, so ending at END would clip real generation time and inflate tok/s.
    """
    start = number(job.get("baseline_epoch")) or number(job.get("start_epoch"))
    if not start:
        return None
    end = number(last_event_ts) if last_event_ts else number(job.get("end_epoch"))
    if not end or end <= start:
        return None
    return end - start


def live_speed_seconds(
    base: dict[str, Any] | None,
    conv: str | None,
    start_epoch: int,
    end_epoch: int,
) -> int | None:
    """`job_speed_seconds` for the direct (non-pending) write paths."""
    convs = conversation_lineage(conv) if conv else []
    events = window_events(convs, start_epoch, end_epoch) if convs else []
    last_ts = max((number(e.get("ts")) for _k, e in events), default=0) or None
    pseudo_job = {
        "baseline_epoch": number((base or {}).get("saved_at")),
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
    }
    return job_speed_seconds(pseudo_job, last_ts)


def apply_stats_job(
    job: dict[str, Any],
    *,
    isolate_window: bool = False,
    token_note: str | None = None,
    speed_note: str | None = None,
    force_unavailable: bool = False,
) -> bool:
    conv = job.get("conversation_id")
    baseline_file = Path(str(job.get("baseline_file") or ""))
    markdown = Path(str(job.get("markdown") or ""))
    start_epoch = number(job.get("start_epoch"))
    end_epoch = number(job.get("end_epoch"))
    if not conv or not markdown.is_file() or not start_epoch or not end_epoch:
        return False

    convs = job_usage_convs(job) or [str(conv)]
    events = window_events(convs, start_epoch, end_epoch)
    last_event_ts = max((number(e.get("ts")) for _k, e in events), default=0) or None

    if force_unavailable:
        # The window is genuinely unsplittable (several videos, one generation).
        # Write 不可用 plus the batch total rather than leaving the placeholder to rot.
        delta = unavailable_totals()
        source = "ambiguous_shared_generation"
    elif isolate_window:
        # Several videos share one baseline inside a single turn, so the
        # baseline-to-now delta covers all of them.  Attribute per video using the
        # ledger events that fall inside this video's own window instead of writing
        # the same joint total onto each Markdown.
        delta = totals_from_events(events)
        source = "ledger_window"
    # Prefer live baseline file; fall back to embedded totals when file was deleted.
    elif baseline_file.is_file():
        delta, source = resolve_delta(conv, baseline_file)
        if not has_usable_delta(delta):
            # Walk up to the parent's ledger — unless this is a sub-agent Cursor never
            # metered, where the parent's events belong to the orchestrator, not here.
            if subagent_without_ledger(job):
                return False
            delta = totals_from_events(events)
            source = "ledger_window_lineage"
    else:
        base_totals = job.get("baseline_totals") or empty_totals()
        base_events = number(job.get("baseline_events"))
        ledger = read_ledger(conv)
        if ledger and ledger_has_new_usage(conv, base_totals, base_events):
            delta = subtract(totals_from_ledger(ledger), base_totals)
            source = "ledger_embedded_baseline"
        else:
            delta = totals_from_events(events)
            source = "ledger_window_lineage"

    if not force_unavailable and not has_usable_delta(delta):
        return False

    resolved_model = resolve_model(
        conv,
        baseline_file=baseline_file if baseline_file.is_file() else None,
    )
    recorded_model = str(job.get("model") or "")
    # Pending jobs are written before the current turn's stop hook fires.  Replace
    # their provisional desktop/Auto value with the concrete CLI model reported by
    # that hook; retain a concrete recorded fallback for older ledgers.
    model = resolved_model if is_concrete_model(resolved_model) else recorded_model
    if not is_concrete_model(model):
        model = "不可用"
    section = format_stats_section(
        model=model,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        delta=delta,
        extra_line=job.get("extra_line"),
        phase_timings=job.get("phase_timings"),
        skill_version=job.get("skill_version"),
        speed_seconds=job_speed_seconds(job, last_event_ts),
        token_note=token_note,
        speed_note=speed_note,
    )
    patch_markdown_stats(markdown, section)
    remove_pending_job(conv, markdown)
    print(f"PATCH_SOURCE={source}")
    return True


def _job_event_keys(job: dict[str, Any]) -> set[str]:
    """Generation ids attributable to a job's wall-clock window, across its lineage."""
    start_epoch = number(job.get("start_epoch"))
    end_epoch = number(job.get("end_epoch"))
    if not start_epoch or not end_epoch:
        return set()
    convs = job_usage_convs(job) or [str(job.get("conversation_id") or "")]
    return {key for key, _event in window_events(convs, start_epoch, end_epoch)}


SUBAGENT_TOKEN_NOTE = "Cursor 未记录子 agent 的 token；该条无法获得实测值"


def subagent_without_ledger(job: dict[str, Any]) -> bool:
    """True when this run happened in a sub-agent whose tokens Cursor never recorded.

    Sub-agent generations emit no stop / afterAgentResponse hook and carry no usage in
    the transcript or chat store, so their tokens exist nowhere on disk.  Borrowing the
    parent's numbers would silently report the orchestrator's few hundred tokens as the
    summary's cost, so say 不可用 and name the reason instead.
    """
    conv = str(job.get("conversation_id") or "")
    if not conv or not parent_conversation(conv):
        return False
    own = read_ledger(conv)
    return not ((own or {}).get("events") or [])


def _batch_events(jobs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Ledger events attributable to the whole batch's wall-clock window."""
    convs: list[str] = []
    for job in jobs:
        for conv in job_usage_convs(job):
            if conv not in convs:
                convs.append(conv)
    start = min((number(j.get("start_epoch")) for j in jobs), default=0)
    end = max((number(j.get("end_epoch")) for j in jobs), default=0)
    if not convs or not start or not end:
        return []
    return window_events(convs, start, end)


def batch_token_note(jobs: list[dict[str, Any]]) -> str:
    """Explain an unsplittable batch instead of writing a bare 不可用."""
    totals = totals_from_events(_batch_events(jobs))
    if totals.get("total", -1) <= 0:
        return f"{len(jobs)} 条视频共用一次生成，无法拆分到单条"
    return (
        f"{len(jobs)} 条视频共用一次生成，无法拆分到单条；"
        f"本批合计 输出 {totals['output']:,} · 合计 {totals['total']:,}"
    )


def batch_speed_note(jobs: list[dict[str, Any]]) -> str | None:
    """Measured batch-level throughput for an unsplittable batch.

    Per-video speed cannot exist (one aggregate event covers the whole turn), but
    the batch's own window — earliest baseline snapshot to the last attributed
    event — and its output total are both real measurements, so report them as a
    labelled 本批 figure instead of leaving the speed line as a bare 不可用.
    """
    events = _batch_events(jobs)
    totals = totals_from_events(events)
    output = totals.get("output", -1)
    if output <= 0:
        return None
    last_ts = max((number(e.get("ts")) for _k, e in events), default=0)
    starts = [
        number(j.get("baseline_epoch")) or number(j.get("start_epoch")) for j in jobs
    ]
    start = min((s for s in starts if s > 0), default=0)
    if not last_ts or not start or last_ts <= start:
        return None
    window = last_ts - start
    return f"单条不可拆分；本批 {output:,} tok ÷ {window} 秒 ≈ {output / window:.1f} tok/s，含工具执行"


def flush_pending_stats(conv: str | None) -> int:
    """Apply every pending job this conversation's ledger can settle."""
    if not conv:
        return 0
    entries = pending_jobs_for(conv)
    if not entries:
        return 0
    all_jobs = [job for _path, job in entries]

    patched = 0

    # 1. Sub-agent runs Cursor never metered.  Settle them first so they neither wait
    #    forever nor drag the parent's numbers in.
    unrecorded = {idx for idx, job in enumerate(all_jobs) if subagent_without_ledger(job)}
    for idx in sorted(unrecorded):
        job = all_jobs[idx]
        markdown = Path(str(job.get("markdown") or ""))
        debug_log(f"stats_subagent_unrecorded conv={conv} markdown={markdown}")
        print(f"PATCH_UNAVAILABLE=subagent_not_metered markdown={markdown}")
        if apply_stats_job(job, token_note=SUBAGENT_TOKEN_NOTE, force_unavailable=True):
            patched += 1

    jobs = [job for idx, job in enumerate(all_jobs) if idx not in unrecorded]

    # A job re-registered for a markdown that was already settled as part of an
    # unsplittable batch must never be treated as a lone, unambiguous job — that
    # would hand it the entire generation's delta.  Drop it and, if the aggregate
    # event has arrived meanwhile, enrich the settled note with the batch totals.
    revived: set[int] = set()
    for idx, job in enumerate(jobs):
        own_conv = str(job.get("conversation_id") or "")
        markdown = job.get("markdown") or ""
        if own_conv and markdown and settled_marker(own_conv, markdown):
            revived.add(idx)
    for idx in sorted(revived):
        job = jobs[idx]
        debug_log(
            f"stats_settled_job_dropped conv={conv} markdown={job.get('markdown')}"
        )
        remove_pending_job(str(job.get("conversation_id") or ""), job.get("markdown"))
    for own_conv in {str(jobs[idx].get("conversation_id") or "") for idx in revived}:
        upgrade_settled_notes(own_conv)
    jobs = [job for idx, job in enumerate(jobs) if idx not in revived]
    if not jobs:
        return patched

    # Several pending Markdown jobs owned by the same direct Cursor conversation can
    # only have come from one still-running agent turn: stop-hook flushing happens at
    # every turn boundary.  Cursor emits one aggregate generation event for that turn,
    # so settle every item as unsplittable instead of leaving early video windows
    # pending forever while only the last window happens to include the stop event.
    direct_conversations = {str(job.get("conversation_id") or "") for job in jobs}
    if len(jobs) > 1 and len(direct_conversations) == 1:
        note = batch_token_note(jobs)
        speed_note = batch_speed_note(jobs)
        for job in jobs:
            markdown = Path(str(job.get("markdown") or ""))
            debug_log(
                f"stats_ambiguous conv={conv} markdown={markdown} "
                "reason=same_turn_multiple_jobs"
            )
            print(f"PATCH_UNSPLITTABLE=same_turn_batch markdown={markdown}")
            if apply_stats_job(
                job, token_note=note, speed_note=speed_note, force_unavailable=True
            ):
                write_settled_marker(job, note, "same_turn_multiple_jobs", speed_note)
                patched += 1
        return patched

    # 2. Cursor reports one aggregate token event per model generation. If several video
    #    windows match the same generation, the aggregate cannot be split honestly.
    event_sets = [_job_event_keys(job) for job in jobs]
    ambiguous: set[int] = set()
    for left in range(len(jobs)):
        for right in range(left + 1, len(jobs)):
            if event_sets[left] & event_sets[right]:
                ambiguous.update((left, right))

    if ambiguous:
        shared = [jobs[idx] for idx in sorted(ambiguous)]
        note = batch_token_note(shared)
        speed_note = batch_speed_note(shared)
        for job in shared:
            markdown = Path(str(job.get("markdown") or ""))
            debug_log(
                f"stats_ambiguous conv={conv} markdown={markdown} "
                "reason=shared_generation_event"
            )
            print(f"PATCH_UNSPLITTABLE=shared_generation markdown={markdown}")
            if apply_stats_job(
                job, token_note=note, speed_note=speed_note, force_unavailable=True
            ):
                write_settled_marker(job, note, "shared_generation_event", speed_note)
                patched += 1

    safe_jobs = [job for idx, job in enumerate(jobs) if idx not in ambiguous]
    isolate = len(jobs) > 1
    patched += sum(1 for job in safe_jobs if apply_stats_job(job, isolate_window=isolate))
    return patched


def is_concrete_model(model: str | None) -> bool:
    if not model:
        return False
    return model.strip().lower() not in {
        "",
        "default",
        "auto",
        "empty",
        "null",
        "unknown",
        "unavailable",
        "不可用",
    }


def load_baseline(path: Path) -> dict[str, Any] | None:
    return load_json_lenient(path)


def snapshot_baseline(path: Path, conv: str | None) -> dict[str, Any]:
    ledger = read_ledger(conv)
    totals = totals_from_ledger(ledger)
    context = read_context_snapshot(conv)
    model_raw, effort = read_model_from_storage()
    data = {
        "conversation_id": conv,
        "conversation_id_source": conversation_id_source(),
        # The conversation whose stop hook will actually record this run's tokens.
        # For a sub-agent that is the parent chat, not `conv`.
        "usage_conversation_id": root_usage_conv(conv),
        "totals": totals,
        "events_count": len((ledger or {}).get("events") or []),
        "context": context or {},
        "model_raw": model_raw,
        "model_effort": effort,
        # Cursor CLI may use an explicit model while the desktop Composer setting is
        # still "default"/Auto.  Never freeze Auto into the baseline; the stop hook
        # will report the concrete model that actually handled this generation.
        "model_friendly": (
            friendly_model(model_raw, effort) if is_concrete_model(model_raw) else "不可用"
        ),
        # Snapshot time doubles as the start of the LLM 速度 window: every output token
        # counted in the delta was produced after this instant.
        "saved_at": int(time.time()),
        "skill_version": read_skill_version(Path(__file__).resolve().parent),
    }
    atomic_write_json(path, data)
    return data


def wait_for_new_event(
    conv: str | None,
    baseline_events: int,
    base_totals: dict[str, int] | None = None,
    timeout_s: float = 90.0,
    baseline_file: Path | None = None,
) -> dict[str, int] | None:
    base_totals = base_totals or empty_totals()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if conv and flush_pending_stats(conv):
            # Pending job already patched markdown; still return ledger totals if available.
            return totals_from_ledger(read_ledger(conv) or {})
        if ledger_has_new_usage(conv, base_totals, baseline_events):
            return totals_from_ledger(read_ledger(conv) or {})
        if baseline_file is not None:
            delta, _source = resolve_delta(conv, baseline_file)
            if has_usable_delta(delta):
                current = empty_totals()
                for key in current:
                    base_val = base_totals.get(key, 0)
                    d_val = delta.get(key, -1)
                    current[key] = base_val + d_val if d_val >= 0 else base_val
                return current
        time.sleep(0.25)
    return None


def print_usage(label: str, conv: str | None, baseline_file: Path | None = None) -> None:
    totals, source = totals_from_sources(conv)
    print(f"SESSION_FILE={session_file(conv)}")
    if baseline_file and baseline_file.is_file():
        base = load_baseline(baseline_file)
        if base and isinstance(base.get("totals"), dict):
            delta = subtract(totals, base["totals"])
            if delta["total"] >= 0:
                cw = delta["cache_write"]
                print("{} input_total={} output={} cache_read={} cache_write={} total={}".format(
                    label,
                    delta["input_total"],
                    delta["output"],
                    delta["cache_read"],
                    cw if cw >= 0 else "不可用",
                    delta["total"],
                ))
                print("SOURCE=delta")
                return
    if source == "unavailable" or totals["total"] < 0:
        print(f"{label} input_total=不可用 output=不可用 cache_read=不可用 cache_write=不可用 total=不可用")
        return
    cw = totals["cache_write"]
    print("{} input_total={} output={} cache_read={} cache_write={} total={}".format(
        label,
        totals["input_total"],
        totals["output"],
        totals["cache_read"],
        cw if cw >= 0 else "不可用",
        totals["total"],
    ))
    print(f"SOURCE={source}")


def ensure_stats(
    *,
    baseline_file: Path,
    markdown: Path,
    start_epoch: int,
    end_epoch: int,
    phase_timings: dict[str, Any] | None = None,
) -> bool:
    base = load_baseline(baseline_file)
    conv = (base or {}).get("conversation_id") or conversation_id()
    model = resolve_model(conv, baseline_file=baseline_file)
    phases = normalize_phase_timings(phase_timings)

    if conv:
        flush_pending_stats(conv)

    delta, source = resolve_delta(conv, baseline_file)
    actual_model = _new_ledger_model(
        read_ledger(conv),
        number((base or {}).get("events_count")),
    )
    if has_usable_delta(delta) and actual_model:
        model = resolve_model(conv, baseline_file=baseline_file)
        section = format_stats_section(
            model=model,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            delta=delta,
            phase_timings=phases,
            skill_version=(base or {}).get("skill_version"),
            speed_seconds=live_speed_seconds(base, conv, start_epoch, end_epoch),
        )
        patch_markdown_stats(markdown, section)
        if conv:
            remove_pending_job(conv, markdown)
        print(f"PATCHED={markdown}")
        print_delta("CURRENT", delta)
        print(f"SOURCE={source}")
        print("ENSURE_STATS=ok")
        return True

    if conv and markdown.is_file():
        if settled_marker(conv, markdown):
            # Already settled as an unsplittable batch member: re-registering would
            # revive the job as a lone unambiguous one and hand it the whole
            # generation delta.  Only try to enrich the note with batch totals.
            upgrade_settled_notes(conv)
            print("ENSURE_STATS=unsplittable_batch")
            return True
        # Re-register the job: --wait-patch now settles everything through
        # flush_pending_stats, so a missing job would mean nothing ever lands.
        if not pending_job_path(conv, markdown).is_file():
            write_pending_job(
                conv=conv,
                baseline_file=baseline_file,
                baseline_events=number((base or {}).get("events_count")),
                markdown=markdown,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                model=model,
                extra_line=None,
                phase_timings=phases,
            )
        spawn_wait_patch(
            baseline_file=baseline_file,
            markdown=markdown,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            model=model,
            extra_line=None,
            phase_timings=phases,
        )

    print("ENSURE_STATS=pending")
    return False


def append_phase_cli_args(cmd: list[str], phase_timings: dict[str, Any] | None) -> None:
    phases = normalize_phase_timings(phase_timings)
    if not phases:
        return
    for key, flag in (
        ("download_seconds", "--download-seconds"),
        ("whisper_seconds", "--whisper-seconds"),
        ("summary_seconds", "--summary-seconds"),
    ):
        if key not in phases:
            continue
        val = phases[key]
        cmd.extend([flag, str(-1 if val is None else val)])


def spawn_wait_patch(
    *,
    baseline_file: Path,
    markdown: Path | None,
    start_epoch: int,
    end_epoch: int,
    model: str,
    extra_line: str | None = None,
    phase_timings: dict[str, Any] | None = None,
) -> None:
    script = Path(__file__).resolve()
    cmd = [
        sys.executable,
        str(script),
        "--wait-patch",
        "--baseline-file",
        str(baseline_file),
        "--start-epoch",
        str(start_epoch),
        "--end-epoch",
        str(end_epoch),
        "--model",
        model,
    ]
    if markdown:
        cmd.extend(["--markdown", str(markdown)])
    if extra_line:
        cmd.extend(["--extra-line", extra_line])
    append_phase_cli_args(cmd, phase_timings)
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def resolve_delta(
    conv: str | None,
    baseline_file: Path,
) -> tuple[dict[str, int], str]:
    base = load_baseline(baseline_file) or {}
    base_totals = base.get("totals") or empty_totals()
    base_events = number(base.get("events_count"))
    base_ctx_totals = context_to_totals(base.get("context") or {})

    ledger = read_ledger(conv)
    if ledger:
        current = totals_from_ledger(ledger)
        delta = subtract(current, base_totals)
        if ledger_has_new_usage(conv, base_totals, base_events) and has_usable_delta(delta):
            return delta, "ledger"

    ctx_delta, ctx_source = context_delta(conv, baseline_file)
    if has_usable_delta(ctx_delta):
        return ctx_delta, ctx_source

    # Unsafe fallthrough: ledger baseline is empty (totals=0) but statusline already
    # had session cumulative totals. subtract(absolute_session, zeros) would attribute
    # the entire conversation to this video (inflated Token / LLM 速度).
    if base_totals.get("total", 0) <= 0 and base_ctx_totals.get("total", 0) > 0:
        return unavailable_totals(), "await_hook"

    current, source = totals_from_sources(conv)
    delta = subtract(current, base_totals)
    if has_usable_delta(delta):
        # Extra guard: never accept a "delta" that equals absolute session totals
        # when we already knew a non-zero session baseline from context.
        if (
            base_ctx_totals.get("total", 0) > 0
            and delta.get("output", -1) == current.get("output", -2)
            and delta.get("input_total", -1) == current.get("input_total", -2)
        ):
            return unavailable_totals(), "await_hook"
        return delta, source
    return unavailable_totals(), "unavailable"


def finalize_usage(
    *,
    baseline_file: Path,
    markdown: Path | None,
    start_epoch: int,
    end_epoch: int,
    model: str,
    extra_line: str | None = None,
    phase_timings: dict[str, Any] | None = None,
    sync_wait_s: float = 0.0,
) -> None:
    base = load_baseline(baseline_file)
    conv = (base or {}).get("conversation_id") or conversation_id()
    base_totals = (base or {}).get("totals") or empty_totals()
    base_events = number((base or {}).get("events_count"))
    phases = normalize_phase_timings(phase_timings)

    if conv and markdown and settled_marker(conv, markdown):
        # Settled as an unsplittable batch member — never overwrite that verdict.
        upgrade_settled_notes(conv)
        print("STATS_UNSPLITTABLE=settled_batch")
        return

    delta, source = resolve_delta(conv, baseline_file)

    if sync_wait_s > 0 and not has_usable_delta(delta) and conv:
        deadline = time.time() + sync_wait_s
        while time.time() < deadline:
            if flush_pending_stats(conv):
                print("PATCHED_VIA_FLUSH=1")
                print_delta("CURRENT", resolve_delta(conv, baseline_file)[0])
                return
            if ledger_has_new_usage(conv, base_totals, base_events):
                current = totals_from_ledger(read_ledger(conv) or {})
                delta = subtract(current, base_totals)
                if has_usable_delta(delta):
                    source = "ledger"
                    break
            ctx_delta, ctx_source = context_delta(conv, baseline_file)
            if has_usable_delta(ctx_delta):
                delta = ctx_delta
                source = ctx_source
                break
            time.sleep(0.25)

    actual_model = _new_ledger_model(read_ledger(conv), base_events)
    if has_usable_delta(delta) and actual_model:
        # The sync wait may have observed the stop hook after the caller resolved
        # its provisional model.  Re-resolve here so a desktop Auto/default value
        # can never become the final Markdown model.
        model = resolve_model(conv, baseline_file=baseline_file)
        print_delta("CURRENT", delta)
        print(f"SOURCE={source}")
        if markdown:
            section = format_stats_section(
                model=model,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                delta=delta,
                extra_line=extra_line,
                phase_timings=phases,
                skill_version=(base or {}).get("skill_version"),
                speed_seconds=live_speed_seconds(base, conv, start_epoch, end_epoch),
            )
            patch_markdown_stats(markdown, section)
            print(f"PATCHED={markdown}")
        if conv:
            remove_pending_job(conv, markdown)
        return

    print("STATS_PENDING=waiting_for_stop_hook")
    if conv and markdown:
        write_pending_job(
            conv=conv,
            baseline_file=baseline_file,
            baseline_events=base_events,
            markdown=markdown,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            model=model,
            extra_line=extra_line,
            phase_timings=phases,
        )
        print(f"PENDING_JOB={pending_job_path(conv, markdown)}")
        section = format_stats_section(
            model=model,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            delta=unavailable_totals(),
            extra_line=extra_line,
            phase_timings=phases,
            skill_version=(base or {}).get("skill_version"),
        )
        patch_markdown_stats(markdown, section)
        print(f"PATCHED_PENDING={markdown}")
    spawn_wait_patch(
        baseline_file=baseline_file,
        markdown=markdown,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        model=model,
        extra_line=extra_line,
        phase_timings=phases,
    )
    print("CURRENT input_total=不可用 output=不可用 cache_read=不可用 cache_write=不可用 total=不可用")
    print("SOURCE=pending_stop_hook")


def print_delta(label: str, delta: dict[str, int]) -> None:
    if delta.get("total", -1) < 0:
        print(f"{label} input_total=不可用 output=不可用 cache_read=不可用 cache_write=不可用 total=不可用")
        return
    cw = delta["cache_write"]
    print("{} input_total={} output={} cache_read={} cache_write={} total={}".format(
        label,
        delta["input_total"],
        delta["output"],
        delta["cache_read"],
        cw if cw >= 0 else "不可用",
        delta["total"],
    ))
    print("SOURCE=delta")


def model_storage_candidates() -> list[Path]:
    """Cursor's globalStorage DB across platforms (was macOS-only)."""
    home = Path.home()
    cands = [
        home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb",  # macOS
        home / ".config/Cursor/User/globalStorage/state.vscdb",                       # Linux
        home / ".config/cursor/User/globalStorage/state.vscdb",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        cands.append(Path(appdata) / "Cursor/User/globalStorage/state.vscdb")         # Windows
    return cands


def read_model_from_storage() -> tuple[str | None, str | None]:
    db = next((p for p in model_storage_candidates() if p.is_file()), None)
    if db is None:
        return None, None
    try:
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key=?",
            ("src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser",),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None, None
        data = json.loads(row[0])
        cfg = ((data.get("aiSettings") or {}).get("modelConfig") or {}).get("composer") or {}
        model = cfg.get("modelName")
        params = cfg.get("selectedModels") or []
        effort = None
        if params and isinstance(params[0], dict):
            for p in params[0].get("parameters") or []:
                if isinstance(p, dict) and p.get("id") == "effort":
                    effort = p.get("value")
        return model, effort
    except Exception:
        return None, None


def friendly_model(model: str | None, effort: str | None = None) -> str:
    if not model:
        return "不可用"
    mapping = {
        "composer-2.5": "Composer 2.5",
        "composer-2.5-fast": "Composer 2.5 Fast",
        "default": "Auto",
        "grok-4.5": "Grok 4.5",
        "grok-4.5-fast": "Grok 4.5 Fast",
        # Keep the baked-in level accurate: this id is xhigh, not high, or the
        # effort suffix gets appended on top ("Grok 4.5 high xhigh").
        "grok-4.5-fast-xhigh": "Grok 4.5 xhigh",
        "cursor-grok-4.5-high-fast": "Grok 4.5 high",
        "claude-sonnet-5": "Sonnet 5",
        "claude-opus-5": "Opus 5",
        "claude-opus-4-8": "Opus 4.8",
        "claude-opus-4-7": "Opus 4.7",
        "claude-haiku-4-5": "Haiku 4.5",
        "claude-fable-5": "Fable 5",
        "gpt-5.6-sol": "GPT 5.6 Sol",
        "gpt-5.6-sol-high": "GPT 5.6 Sol high",
        "gpt-5.6-terra": "GPT 5.6 Terra",
        "gpt-5.6-terra-medium": "GPT 5.6 Terra medium",
    }
    known = mapping.get(model)
    if known:
        name = known
    else:
        # Unknown/new model: keep the raw id rather than lower-casing it into
        # something unrecognisable ("claude-opus-5" -> "claude opus 5").
        name = model.strip()
    # Shared with Claude / Codex so the 模型 line reads the same in all three
    # environments, and unknown effort strings can't leak into the stats block.
    return compose_model_label(name, effort)


def _matching_settings_effort(
    actual_model: str,
    settings_model: str | None,
    settings_effort: str | None,
) -> str | None:
    """Use desktop effort only when it belongs to the same concrete model."""
    if not settings_model or settings_model.strip().lower() != actual_model.strip().lower():
        return None
    return settings_effort


def _new_ledger_model(ledger: dict[str, Any] | None, baseline_events: int) -> str | None:
    """Return the concrete model from events created after this run's baseline."""
    events = list((ledger or {}).get("events") or [])
    if len(events) <= baseline_events:
        return None
    for event in reversed(events[baseline_events:]):
        if not isinstance(event, dict):
            continue
        raw = str(event.get("model") or "")
        if is_concrete_model(raw):
            return raw
    raw = str((ledger or {}).get("model") or "")
    return raw if is_concrete_model(raw) else None


def resolve_model(conv: str | None, baseline_file: Path | None = None) -> str:
    """Resolve model from the actual CLI generation, never finalise as Auto.

    Priority with a baseline: a new stop-hook event for this run, then a concrete
    baseline setting.  Without a baseline, the latest concrete ledger value is a
    best-effort diagnostic fallback.  Desktop ``default``/Auto is deliberately
    treated as unavailable because it does not identify the model Cursor routed to.
    """
    base = load_baseline(baseline_file) if baseline_file else None
    settings_model, settings_effort = read_model_from_storage()
    ledger = read_ledger(conv)

    actual = _new_ledger_model(ledger, number((base or {}).get("events_count")))
    if actual:
        return friendly_model(
            actual,
            _matching_settings_effort(actual, settings_model, settings_effort),
        )

    if base:
        raw = str(base.get("model_raw") or "")
        if is_concrete_model(raw):
            return friendly_model(raw, base.get("model_effort"))
        friendly = str(base.get("model_friendly") or "")
        if is_concrete_model(friendly):
            return friendly
        return "不可用"

    raw = str((ledger or {}).get("model") or "")
    if is_concrete_model(raw):
        return friendly_model(
            raw,
            _matching_settings_effort(raw, settings_model, settings_effort),
        )
    if is_concrete_model(settings_model):
        return friendly_model(settings_model, settings_effort)
    return "不可用"


def session_file(conv: str | None) -> str:
    led = ledger_path(conv)
    if led and led.is_file():
        return str(led)
    tr = transcript_path(conv)
    if tr and tr.is_file():
        return str(tr)
    return "不可用"


def ensure_cli_statusline(skill_dir: Path) -> None:
    src = skill_dir / "scripts/cursor-statusline-usage.sh"
    if not src.is_file():
        print("STATUSLINE=missing_script")
        return
    dest = Path.home() / ".cursor/hooks/video_summary-statusline-usage.sh"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text("utf-8"), encoding="utf-8")
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    cli_config = Path.home() / ".cursor/cli-config.json"
    if not cli_config.is_file():
        print("STATUSLINE=skipped_no_cli_config")
        return
    try:
        data = json.loads(cli_config.read_text("utf-8"))
    except Exception:
        print("STATUSLINE=skipped_bad_cli_config")
        return
    cmd = str(dest)
    existing = data.get("statusLine") or {}
    existing_cmd = str(existing.get("command") or "")
    if "video_summary-statusline-usage.sh" in existing_cmd:
        print(f"STATUSLINE=already_configured path={dest}")
        return
    if existing_cmd and "video_summary-statusline-usage.sh" not in existing_cmd:
        print(f"STATUSLINE=manual_required existing={existing_cmd}")
        return
    data["statusLine"] = {
        "type": "command",
        "command": cmd,
        "padding": 0,
        "updateIntervalMs": 300,
        "timeoutMs": 500,
    }
    cli_config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"STATUSLINE=installed path={dest}")


def ensure_hook(skill_dir: Path) -> None:
    hook_script = skill_dir / "scripts/cursor-usage-hook.sh"
    if not hook_script.is_file():
        print("HOOK=missing_script")
        return
    hooks_dir = Path.home() / ".cursor/hooks"
    dest = hooks_dir / "cursor-usage-hook.sh"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(hook_script.read_text("utf-8"), encoding="utf-8")
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    usage_script = Path(__file__).resolve()
    usage_dest = hooks_dir / "video_summary-cursor_usage.py"
    usage_dest.write_text(usage_script.read_text("utf-8"), encoding="utf-8")

    # Required import for the hooks copy; without it stop/afterAgentResponse crash
    # with ModuleNotFoundError and token stats stay「不可用」forever.
    lib_src = usage_script.parent / "runtime_stats_lib.py"
    if not lib_src.is_file():
        lib_src = skill_dir / "scripts" / "runtime_stats_lib.py"
    if lib_src.is_file():
        lib_dest = hooks_dir / "runtime_stats_lib.py"
        lib_dest.write_text(lib_src.read_text("utf-8"), encoding="utf-8")
        print(f"HOOK_LIB=installed path={lib_dest}")
    else:
        print("HOOK_LIB=missing runtime_stats_lib.py", file=sys.stderr)

    # The hooks copies live outside the skill tree, where walking up from __file__ never
    # finds SKILL.md.  Leave the version behind so stop-hook patches can still name it.
    skill_version = read_skill_version(skill_dir)
    sidecar = hooks_dir / "usage" / "skill_meta.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        sidecar,
        {"skill_dir": str(skill_dir.resolve()), "version": skill_version, "saved_at": int(time.time())},
    )
    print(f"HOOK_SKILL_VERSION={format_skill_version(skill_version)}")

    hooks_json = Path.home() / ".cursor/hooks.json"
    entry = {"command": "./hooks/cursor-usage-hook.sh"}
    data: dict[str, Any]
    if hooks_json.is_file():
        try:
            data = json.loads(hooks_json.read_text("utf-8"))
        except Exception:
            data = {"version": 1, "hooks": {}}
    else:
        data = {"version": 1, "hooks": {}}
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    for event in ("stop", "sessionEnd", "afterAgentResponse"):
        items = hooks.setdefault(event, [])
        if not any(isinstance(i, dict) and i.get("command") == entry["command"] for i in items):
            items.append(dict(entry))
    hooks_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"HOOK=installed path={dest}")

    # Smoke-test the installed hooks copy so a missing runtime_stats_lib fails loudly here,
    # not silently inside stop/afterAgentResponse (which used to leave Token「不可用」).
    try:
        probe = subprocess.run(
            [sys.executable, str(usage_dest), "--print-conversation-id"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if probe.returncode != 0:
            err = (probe.stderr or probe.stdout or "").strip().replace("\n", " ")[:300]
            print(f"HOOK_SMOKE=fail code={probe.returncode} err={err}", file=sys.stderr)
        else:
            print("HOOK_SMOKE=ok")
    except Exception as exc:
        print(f"HOOK_SMOKE=fail err={exc}", file=sys.stderr)

    ensure_cli_statusline(skill_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="CURRENT")
    parser.add_argument("--model", action="store_true")
    parser.add_argument("--doctor", action="store_true", help="Preflight: can model/token be resolved?")
    parser.add_argument("--ensure-hook", action="store_true")
    parser.add_argument("--skill-dir", type=Path)
    parser.add_argument("--snapshot-baseline", type=Path)
    parser.add_argument("--baseline-file", type=Path)
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--wait-patch", action="store_true")
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--start-epoch", type=int)
    parser.add_argument("--end-epoch", type=int)
    parser.add_argument("--extra-line")
    parser.add_argument(
        "--download-seconds",
        type=int,
        help="Wall-clock seconds for subtitle/audio download; -1 => 不可用",
    )
    parser.add_argument(
        "--whisper-seconds",
        type=int,
        help="Wall-clock seconds for Whisper transcription; omit if unused; -1 => 不可用",
    )
    parser.add_argument(
        "--summary-seconds",
        type=int,
        help="Wall-clock seconds for summarization; -1 => 不可用",
    )
    parser.add_argument("--flush-pending", action="store_true")
    parser.add_argument("--ensure-stats", action="store_true")
    parser.add_argument("--ingest-context", action="store_true")
    parser.add_argument("--ingest-hook", action="store_true")
    parser.add_argument("--sync-wait", type=float, default=0.0)
    parser.add_argument("--conversation-id")
    parser.add_argument("--print-conversation-id", action="store_true")
    parser.add_argument("--repair-from-ledger", action="store_true")
    parser.add_argument(
        "--skill-version",
        help="Override the Skill 版本 line; normally read from SKILL.md frontmatter",
    )
    parser.add_argument(
        "--extract-result",
        type=Path,
        help="Print the result text from a nested cursor-agent (stream-)json capture",
    )
    parser.add_argument(
        "--finalize-from-result",
        type=Path,
        help="Write real per-video stats from a nested cursor-agent (stream-)json capture",
    )
    parser.add_argument(
        "--model-name",
        help="Model label for --finalize-from-result (the nested worker's model)",
    )
    parser.add_argument(
        "--effort",
        help="Reasoning effort appended to --model-name (none/minimal/low/medium/high/xhigh/max)",
    )
    args = parser.parse_args()

    phase_timings = phase_timings_from_args(
        args.download_seconds,
        args.whisper_seconds,
        args.summary_seconds,
    )

    # An explicit value lets the workflow bind every Step 0/4 command to the
    # same session even if Cursor mutates environment variables mid-turn.
    conv = args.conversation_id or conversation_id()

    if args.print_conversation_id:
        if conv:
            print(conv)
        return

    if args.ingest_context:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            print("INGEST_CONTEXT=invalid_json", file=sys.stderr)
            sys.exit(1)
        path = ingest_context_payload(payload if isinstance(payload, dict) else {})
        if path:
            print(f"INGEST_CONTEXT={path}")
        else:
            print("INGEST_CONTEXT=skipped")
        return

    if args.ingest_hook:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            print("INGEST_HOOK=invalid_json", file=sys.stderr)
            sys.exit(1)
        result = ingest_hook_payload(payload if isinstance(payload, dict) else {})
        print(
            "INGEST_HOOK status={status} recorded={recorded} flushed={flushed} conv={conversation_id}".format(
                **{k: result.get(k) for k in ("status", "recorded", "flushed", "conversation_id")}
            )
        )
        return

    if args.ensure_hook:
        skill_dir = args.skill_dir or Path(__file__).resolve().parent.parent
        ensure_hook(skill_dir)
        return

    if args.doctor:
        ledger = read_ledger(conv)
        model_raw, effort = read_model_from_storage()
        resolved_model = resolve_model(conv)
        db = next((p for p in model_storage_candidates() if p.is_file()), None)
        hook_installed = (Path.home() / ".cursor/hooks/cursor-usage-hook.sh").is_file()
        ok = print_doctor(
            [
                ("AGENT", "cursor"),
                ("SKILL_VERSION", format_skill_version(read_skill_version(Path(__file__).resolve().parent))),
                ("CWD", str(Path.cwd())),
                ("CONVERSATION_ID", conv or ""),
                ("CONVERSATION_SOURCE", conversation_id_source()),
                # A sub-agent's own ledger is never written; this names the chat whose
                # stop hook will settle its stats.
                ("USAGE_CONVERSATION_ID", root_usage_conv(conv) or ""),
                ("SESSION_FILE", session_file(conv)),
                ("LEDGER_EVENTS", str(len((ledger or {}).get("events") or []))),
                ("MODEL_DB", str(db) if db else ""),
                # Desktop "default" means a router, not the concrete model used by
                # Cursor CLI.  Report unavailable until a hook supplies the real id.
                ("MODEL", resolved_model),
                # Informational: some models have no selectable effort level.
                ("EFFORT", normalize_effort(effort) or ""),
                ("HOOK_INSTALLED", "yes" if hook_installed else ""),
            ],
            # MODEL_DB is informational: without the desktop DB the model name still
            # resolves from the ledger, so it must not flip the verdict on its own.
            essential=("CONVERSATION_ID", "SESSION_FILE", "MODEL", "HOOK_INSTALLED"),
        )
        if not hook_installed:
            print(
                "hint=run: python3 <skill>/scripts/cursor_usage.py --ensure-hook "
                "--skill-dir <skill>   # Token 统计依赖 stop hook",
                file=sys.stderr,
            )
        sys.exit(0 if ok else 2)

    if args.snapshot_baseline:
        if not conv:
            print(
                "error=conversation_unavailable_baseline_degraded "
                f"cwd={Path.cwd()} tried={','.join(path_key_variants(Path.cwd(), leading_dash=False))}",
                file=sys.stderr,
            )
            sys.exit(1)
        snap = snapshot_baseline(args.snapshot_baseline, conv)
        print(f"BASELINE_FILE={args.snapshot_baseline}")
        print(f"CONVERSATION_ID={snap['conversation_id'] or '不可用'}")
        print(f"CONVERSATION_SOURCE={snap['conversation_id_source']}")
        print(
            "BASELINE input_total={} output={} cache_read={} cache_write={} total={}".format(
                snap["totals"]["input_total"],
                snap["totals"]["output"],
                snap["totals"]["cache_read"],
                snap["totals"]["cache_write"],
                snap["totals"]["total"],
            )
        )
        print(f"BASELINE_EVENTS={snap['events_count']}")
        ctx = snap.get("context") or {}
        if ctx.get("input_total") is not None:
            print(
                "BASELINE_CONTEXT input_total={} output_total={}".format(
                    ctx.get("input_total", 0),
                    ctx.get("output_total", 0),
                )
            )
        if snap.get("model_friendly"):
            print(f"MODEL={snap['model_friendly']}")
        return

    if args.repair_from_ledger:
        if not conv or not args.markdown or not args.start_epoch or not args.end_epoch:
            print("error=repair_requires_conversation_markdown_start_end", file=sys.stderr)
            sys.exit(1)
        # Search the whole lineage: a sub-agent's own ledger is always empty because
        # stop only fires for the chat the user drives.
        repair_convs = conversation_lineage(conv)
        repair_events = window_events(repair_convs, args.start_epoch, args.end_epoch)
        delta = totals_from_events(repair_events)
        if not has_usable_delta(delta):
            delta = totals_from_ledger_since(read_ledger(conv), args.start_epoch, args.end_epoch)
            repair_events = []
        if not has_usable_delta(delta):
            print("REPAIRED=0 source=ledger_window_unavailable", file=sys.stderr)
            sys.exit(2)
        last_ts = max((number(e.get("ts")) for _k, e in repair_events), default=0) or None
        base_for_repair = load_baseline(args.baseline_file) if args.baseline_file else None
        section = format_stats_section(
            model=resolve_model(conv),
            start_epoch=args.start_epoch,
            end_epoch=args.end_epoch,
            delta=delta,
            extra_line=args.extra_line,
            phase_timings=phase_timings,
            skill_version=(base_for_repair or {}).get("skill_version")
            or args.skill_version,
            speed_seconds=job_speed_seconds(
                {
                    "baseline_epoch": number((base_for_repair or {}).get("saved_at")),
                    "start_epoch": args.start_epoch,
                    "end_epoch": args.end_epoch,
                },
                last_ts,
            ),
        )
        if not patch_markdown_stats(args.markdown, section):
            print("error=repair_markdown_missing", file=sys.stderr)
            sys.exit(1)
        print(f"REPAIRED={args.markdown}")
        print_delta("CURRENT", delta)
        print("SOURCE=ledger_window")
        return

    if args.extract_result:
        result = worker_result_event(args.extract_result)
        if not result or result.get("is_error"):
            print("error=worker_result_missing_or_error", file=sys.stderr)
            sys.exit(2)
        sys.stdout.write(str(result.get("result") or ""))
        return

    if args.finalize_from_result:
        if not args.markdown or not args.start_epoch or not args.end_epoch:
            print("error=finalize_from_result_requires_markdown_start_end", file=sys.stderr)
            sys.exit(1)
        result = worker_result_event(args.finalize_from_result)
        if not result or result.get("is_error"):
            print("error=worker_result_missing_or_error", file=sys.stderr)
            sys.exit(2)
        delta = totals_from_worker_result(result)
        if not has_usable_delta(delta):
            print("error=worker_usage_missing", file=sys.stderr)
            sys.exit(2)
        duration_ms = number(result.get("duration_api_ms")) or number(result.get("duration_ms"))
        speed_seconds = max(1, round(duration_ms / 1000)) if duration_ms > 0 else None
        if args.model_name:
            worker_model = compose_model_label(args.model_name, args.effort)
        else:
            raw = result.get("model")
            worker_model = str(raw) if raw and is_concrete_model(str(raw)) else "不可用"
        section = format_stats_section(
            model=worker_model,
            start_epoch=args.start_epoch,
            end_epoch=args.end_epoch,
            delta=delta,
            extra_line=args.extra_line,
            phase_timings=phase_timings,
            skill_version=args.skill_version,
            speed_seconds=speed_seconds,
        )
        if not patch_markdown_stats(args.markdown, section):
            print("error=finalize_markdown_missing", file=sys.stderr)
            sys.exit(1)
        print(f"PATCHED={args.markdown}")
        print_delta("CURRENT", delta)
        print("SOURCE=worker_result")
        return

    if args.wait_patch:
        base = load_baseline(args.baseline_file) if args.baseline_file else None
        conv = (base or {}).get("conversation_id") or conv
        # Everything goes through flush_pending_stats: it is the only path that knows
        # whether sibling videos share this generation.  The old fallback here wrote the
        # conversation-wide delta straight into each Markdown, which is how two videos
        # in one batch ended up with an identical (and doubled) 输出 figure.
        usage_conv = (base or {}).get("usage_conversation_id") or root_usage_conv(conv)
        for target in dict.fromkeys([c for c in (conv, usage_conv) if c]):
            if flush_pending_stats(target):
                print(f"PATCHED={args.markdown}")
                return
        base_totals = (base or {}).get("totals") or empty_totals()
        base_events = number((base or {}).get("events_count"))
        wait_for_new_event(
            usage_conv or conv,
            base_events,
            base_totals,
            timeout_s=180.0,
            baseline_file=args.baseline_file,
        )
        for target in dict.fromkeys([c for c in (conv, usage_conv) if c]):
            if flush_pending_stats(target):
                print(f"PATCHED={args.markdown}")
                print("SOURCE=flush_pending")
                return
        for target in dict.fromkeys([c for c in (conv, usage_conv) if c]):
            if upgrade_settled_notes(target):
                print(f"UPGRADED={args.markdown}")
                print("SOURCE=settled_note_upgrade")
                return
        return

    if args.flush_pending:
        target = args.conversation_id or conv
        count = flush_pending_stats(target)
        print(f"FLUSHED={count}")
        return

    if args.ensure_stats:
        if not args.baseline_file or not args.markdown or not args.start_epoch or not args.end_epoch:
            print("error=ensure_stats_requires_baseline_markdown_start_end", file=sys.stderr)
            sys.exit(1)
        ok = ensure_stats(
            baseline_file=args.baseline_file,
            markdown=args.markdown,
            start_epoch=args.start_epoch,
            end_epoch=args.end_epoch,
            phase_timings=phase_timings,
        )
        sys.exit(0 if ok else 2)

    if args.finalize:
        if not args.baseline_file or not args.start_epoch or not args.end_epoch:
            print("error=finalize_requires_baseline_start_end", file=sys.stderr)
            sys.exit(1)
        model = resolve_model(conv, baseline_file=args.baseline_file)
        sync_wait = args.sync_wait
        # Mid-turn finalize often runs before stop hook / statusline catch up.
        # Wait briefly so we prefer ledger/context *increments*, not whole-session totals.
        if sync_wait <= 0:
            sync_wait = 8.0 if is_cli_runtime() else 5.0
        finalize_usage(
            baseline_file=args.baseline_file,
            markdown=args.markdown,
            start_epoch=args.start_epoch,
            end_epoch=args.end_epoch,
            model=model,
            extra_line=args.extra_line,
            phase_timings=phase_timings,
            sync_wait_s=sync_wait,
        )
        return

    if args.model:
        print(f"MODEL={resolve_model(conv, baseline_file=args.baseline_file)}")
        return

    print_usage(args.label, conv, baseline_file=args.baseline_file)


if __name__ == "__main__":
    main()
