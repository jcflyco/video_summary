#!/usr/bin/env python3
"""Orchestrator for video_summary: check / probe / audio / whisper / batch / register.

Agent responsibility after pipeline prepare: read transcript_file, write Chinese
summary markdown, then call register + finalize.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from index_store import (  # noqa: E402
    lookup,
    parse_media_ref,
    rebuild_index,
    register as index_register,
)

BATCH_NAME = "batch_state.json"


def emit(payload: dict[str, Any], code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def run_json_script(script: str, args: list[str]) -> tuple[dict[str, Any], int]:
    cmd = [sys.executable, str(SCRIPT_DIR / script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    text = (proc.stdout or "").strip()
    # Some scripts print progress JSON lines; take the last JSON object.
    payload: dict[str, Any] = {}
    if text:
        # Prefer last {...} block
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            for line in reversed(text.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        payload = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            if not payload:
                # Multi-line pretty JSON: find last complete object by brace scan
                start = text.rfind("\n{")
                if start == -1 and text.startswith("{"):
                    start = 0
                elif start != -1:
                    start += 1
                if start is not None and start >= 0:
                    try:
                        payload = json.loads(text[start:])
                    except json.JSONDecodeError:
                        payload = {
                            "status": "error",
                            "error": "failed to parse script JSON",
                            "stdout": text[-2000:],
                            "stderr": (proc.stderr or "")[-2000:],
                        }
    if not payload:
        payload = {
            "status": "error",
            "error": (proc.stderr or "script failed").strip() or f"exit {proc.returncode}",
            "stderr": (proc.stderr or "")[-2000:],
        }
    return payload, proc.returncode


def default_scratchpad(work_dir: Path) -> Path:
    return work_dir / ".scratchpad" / "video_summary"


def batch_path(scratchpad: Path) -> Path:
    return scratchpad / BATCH_NAME


def load_batch(scratchpad: Path) -> dict[str, Any]:
    path = batch_path(scratchpad)
    if not path.is_file():
        raise FileNotFoundError(f"batch state missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_batch(scratchpad: Path, data: dict[str, Any]) -> Path:
    scratchpad.mkdir(parents=True, exist_ok=True)
    path = batch_path(scratchpad)
    data["updated_at"] = int(time.time())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def item_by_id(batch: dict[str, Any], video_id: str) -> dict[str, Any] | None:
    for item in batch.get("items", []):
        if str(item.get("video_id")) == str(video_id):
            return item
    return None


def ensure_local_server(work_dir: Path) -> dict[str, Any]:
    """缓存命中等不经 finalize 的出口也确保本地播放服务在运行（file:// 无法内嵌 YouTube）。"""
    try:
        from build_html import dir_port, ensure_server  # noqa: E402

        base = str(work_dir)
        port = dir_port(base)
        running = ensure_server(base, port)
        return {
            "server_running": running,
            "url": f"http://127.0.0.1:{port}/视频总结.html",
        }
    except Exception as exc:  # 服务拉起失败不阻断主流程，如实报告
        return {"server_running": False, "server_error": str(exc)}


def cmd_check(args: argparse.Namespace) -> None:
    work_dir = Path(args.dir).resolve()
    ref = parse_media_ref(args.url)
    if not ref:
        emit({"status": "error", "error": "unsupported or unparseable url"}, 1)
    hit = lookup(work_dir, platform=ref["platform"], video_id=ref["video_id"])
    if hit and not args.force:
        emit(
            {
                "status": "cached",
                "platform": ref["platform"],
                "video_id": ref["video_id"],
                "path": hit["path"],
                "abs_path": hit["abs_path"],
                "title": hit.get("title") or "",
                "agent_action": "reuse",
                "message": "已有总结，直接复用；用户要求重跑时加 --force",
                **ensure_local_server(work_dir),
            }
        )
    emit(
        {
            "status": "miss",
            "platform": ref["platform"],
            "video_id": ref["video_id"],
            "agent_action": "probe",
        }
    )


def cmd_probe(args: argparse.Namespace) -> None:
    work_dir = Path(args.dir).resolve()
    scratchpad = Path(args.scratchpad).resolve() if args.scratchpad else default_scratchpad(work_dir)
    scratchpad.mkdir(parents=True, exist_ok=True)

    ref = parse_media_ref(args.url)
    if ref and not args.force:
        hit = lookup(work_dir, platform=ref["platform"], video_id=ref["video_id"])
        if hit:
            emit(
                {
                    "status": "cached",
                    "platform": ref["platform"],
                    "video_id": ref["video_id"],
                    "path": hit["path"],
                    "abs_path": hit["abs_path"],
                    "title": hit.get("title") or "",
                    "agent_action": "reuse",
                    **ensure_local_server(work_dir),
                }
            )

    t0 = time.time()
    payload, code = run_json_script(
        "fetch_video.py",
        ["--url", args.url, "--scratchpad", str(scratchpad)],
    )
    elapsed = round(time.time() - t0, 1)
    payload["download_seconds"] = elapsed
    payload["scratchpad"] = str(scratchpad)

    status = payload.get("status")
    if status == "ok":
        payload["agent_action"] = "summarize"
        payload["ready_for_summary"] = True
    elif status == "no_srt":
        payload["agent_action"] = "ask_whisper"
        payload["ready_for_summary"] = False
    elif status == "cached":
        payload["agent_action"] = "reuse"
    else:
        payload["agent_action"] = "report_error"
    emit(payload, 0 if status in {"ok", "no_srt", "cached"} else (code or 1))


def cmd_download_audio(args: argparse.Namespace) -> None:
    work_dir = Path(args.dir).resolve() if args.dir else Path.cwd()
    scratchpad = Path(args.scratchpad).resolve() if args.scratchpad else default_scratchpad(work_dir)
    scratchpad.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    payload, code = run_json_script(
        "fetch_audio.py",
        ["--url", args.url, "--scratchpad", str(scratchpad)],
    )
    payload["download_seconds"] = round(time.time() - t0, 1)

    if args.batch and payload.get("status") == "ok":
        try:
            batch = load_batch(scratchpad)
            item = item_by_id(batch, str(payload.get("video_id") or ""))
            if item:
                item["audio_status"] = "ready"
                item["audio_file"] = payload.get("audio_file")
                item["download_seconds"] = payload["download_seconds"]
                if payload.get("title"):
                    item["title"] = payload["title"]
                save_batch(scratchpad, batch)
                payload["batch_updated"] = True
        except FileNotFoundError:
            pass

    emit(payload, 0 if payload.get("status") == "ok" else (code or 1))


def cmd_transcribe(args: argparse.Namespace) -> None:
    work_dir = Path(args.dir).resolve() if args.dir else Path.cwd()
    scratchpad = Path(args.scratchpad).resolve() if args.scratchpad else default_scratchpad(work_dir)
    scratchpad.mkdir(parents=True, exist_ok=True)

    audio = Path(args.audio)
    video_id = args.video_id
    if not video_id:
        video_id = audio.stem
    srt = scratchpad / f"{video_id}.whisper.srt"
    txt = scratchpad / f"{video_id}.txt"

    if args.batch:
        try:
            batch = load_batch(scratchpad)
            if batch.get("whisper_active"):
                emit(
                    {
                        "status": "error",
                        "error": f"whisper already active: {batch['whisper_active']}",
                        "agent_action": "wait",
                    },
                    1,
                )
            item = item_by_id(batch, video_id)
            if item:
                item["whisper_status"] = "running"
                batch["whisper_active"] = video_id
                save_batch(scratchpad, batch)
        except FileNotFoundError:
            pass

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "transcribe_whisper.py"),
        "--audio",
        str(audio),
        "--srt",
        str(srt),
        "--model",
        args.model,
        "--backend",
        args.backend,
        "--heartbeat-seconds",
        str(args.heartbeat_seconds),
    ]
    if args.language:
        cmd.extend(["--language", args.language])

    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    whisper_seconds = round(time.time() - t0, 1)

    # Parse last complete JSON line / object from stdout
    result: dict[str, Any] = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = obj

    if proc.returncode != 0 or result.get("status") == "error" or not srt.is_file():
        err = result.get("error") or (proc.stderr or "").strip() or "transcribe failed"
        if args.batch:
            _batch_whisper_done(scratchpad, video_id, ok=False, error=err)
        emit(
            {
                "status": "error",
                "video_id": video_id,
                "error": err,
                "whisper_seconds": whisper_seconds,
                "stderr": (proc.stderr or "")[-2000:],
            },
            1,
        )

    compact = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "compact_subtitles.py"),
            str(srt),
            str(txt),
        ],
        capture_output=True,
        text=True,
    )
    if compact.returncode != 0 or not txt.is_file():
        if args.batch:
            _batch_whisper_done(scratchpad, video_id, ok=False, error="compact failed")
        emit(
            {
                "status": "error",
                "video_id": video_id,
                "error": compact.stderr.strip() or "compact_subtitles failed",
                "whisper_seconds": whisper_seconds,
            },
            1,
        )

    lines = sum(1 for _ in txt.open(encoding="utf-8"))
    payload = {
        "status": "ok",
        "video_id": video_id,
        "subtitle_file": str(srt),
        "transcript_file": str(txt),
        "transcript_lines": lines,
        "compact_stats": (compact.stdout or "").strip(),
        "whisper_seconds": whisper_seconds,
        "backend": result.get("backend"),
        "language": result.get("language") or args.language or "",
        "subtitle_type": "whisper",
        "ready_for_summary": True,
        "agent_action": "summarize",
    }
    if args.batch:
        _batch_whisper_done(
            scratchpad,
            video_id,
            ok=True,
            transcript_file=str(txt),
            whisper_seconds=whisper_seconds,
            language=payload["language"],
        )
        payload["batch_updated"] = True
    emit(payload)


def _batch_whisper_done(
    scratchpad: Path,
    video_id: str,
    *,
    ok: bool,
    transcript_file: str | None = None,
    whisper_seconds: float | None = None,
    language: str = "",
    error: str = "",
) -> None:
    try:
        batch = load_batch(scratchpad)
    except FileNotFoundError:
        return
    item = item_by_id(batch, video_id)
    if item:
        if ok:
            item["whisper_status"] = "done"
            item["transcript_file"] = transcript_file
            item["summary_status"] = "pending"
            item["ready_for_summary"] = True
            if whisper_seconds is not None:
                item["whisper_seconds"] = whisper_seconds
            if language:
                item["language"] = language
            item["subtitle_type"] = "whisper"
        else:
            item["whisper_status"] = "error"
            item["error"] = error
            item["summary_status"] = "skipped"
        if batch.get("whisper_active") == video_id:
            batch["whisper_active"] = None
        save_batch(scratchpad, batch)


def cmd_register(args: argparse.Namespace) -> None:
    work_dir = Path(args.dir).resolve()
    md = Path(args.markdown)
    if not md.is_file():
        emit({"status": "error", "error": f"markdown not found: {md}"}, 1)

    platform = args.platform
    video_id = args.video_id
    if args.url and (not platform or not video_id):
        ref = parse_media_ref(args.url)
        if ref:
            platform = platform or ref["platform"]
            video_id = video_id or ref["video_id"]
    if not platform or not video_id:
        emit({"status": "error", "error": "need --url or --platform/--video-id"}, 1)

    entry = index_register(
        work_dir,
        platform=platform,
        video_id=video_id,
        path=md,
        url=args.url or "",
        title=args.title or "",
        webpage_url=args.webpage_url or args.url or "",
    )

    append: dict[str, Any] = {}
    md_text = md.read_text(encoding="utf-8", errors="ignore")
    has_original = "## 原文字幕" in md_text

    if args.subtitle_file:
        srt = Path(args.subtitle_file)
        if srt.is_file():
            append["srt"], _ = run_json_script(
                "append_srt.py",
                ["--md", str(md), "--srt", str(srt), "--heading", "原文字幕"],
            )
        else:
            append["srt"] = {
                "status": "error",
                "error": f"subtitle file not found: {srt}",
            }
    elif not has_original:
        append["srt"] = {
            "status": "error",
            "error": "missing --subtitle-file; 成功成稿必须含「## 原文字幕」",
        }

    if args.zh_subtitle_file:
        zh = Path(args.zh_subtitle_file)
        if zh.is_file():
            append["zh"], _ = run_json_script(
                "append_srt.py",
                ["--md", str(md), "--srt", str(zh), "--heading", "中文字幕"],
            )
        else:
            append["zh"] = {
                "status": "error",
                "error": f"zh subtitle file not found: {zh}",
            }

    md_text_after = md.read_text(encoding="utf-8", errors="ignore")
    has_original_after = "## 原文字幕" in md_text_after
    if not has_original_after:
        emit(
            {
                "status": "error",
                "error": "register 后仍无「## 原文字幕」；禁止交付无原文的总结",
                "entry": entry,
                "append": append,
                "agent_action": "fix_original_srt",
            },
            1,
        )

    if args.scratchpad:
        scratchpad = Path(args.scratchpad).resolve()
        try:
            batch = load_batch(scratchpad)
            item = item_by_id(batch, video_id)
            if item:
                item["summary_status"] = "done"
                item["md_path"] = entry["path"]
                item["ready_for_summary"] = False
                save_batch(scratchpad, batch)
        except FileNotFoundError:
            pass

    emit(
        {
            "status": "ok",
            "entry": entry,
            "append": append,
            "has_original_srt": True,
            "agent_action": "done",
        }
    )


def cmd_finalize(args: argparse.Namespace) -> None:
    work_dir = Path(args.dir).resolve()
    if args.rebuild_index:
        idx = rebuild_index(work_dir)
    else:
        idx = {"status": "skipped"}

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "build_html.py"), "--dir", str(work_dir)],
        capture_output=True,
        text=True,
    )
    html_payload: dict[str, Any]
    try:
        html_payload = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        html_payload = {
            "status": "error",
            "error": proc.stderr.strip() or "build_html failed",
            "stdout": proc.stdout[-1000:],
        }
    emit(
        {
            "status": "ok" if html_payload.get("status") == "ok" else "error",
            "index": idx,
            "html": html_payload,
        },
        0 if html_payload.get("status") == "ok" else 1,
    )


def cmd_rebuild_index(args: argparse.Namespace) -> None:
    emit(rebuild_index(Path(args.dir).resolve()))


def cmd_batch_init(args: argparse.Namespace) -> None:
    work_dir = Path(args.dir).resolve()
    scratchpad = Path(args.scratchpad).resolve() if args.scratchpad else default_scratchpad(work_dir)
    scratchpad.mkdir(parents=True, exist_ok=True)

    urls: list[str] = list(args.url or [])
    if args.urls_file:
        text = Path(args.urls_file).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    # Deduplicate preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    items = []
    for u in ordered:
        ref = parse_media_ref(u)
        items.append(
            {
                "url": u,
                "platform": ref["platform"] if ref else "unknown",
                "video_id": ref["video_id"] if ref else "",
                "title": "",
                "upload_date": "",
                "probe_status": "pending",
                "whisper_consent": None,
                "audio_status": "skipped",
                "audio_file": None,
                "whisper_status": "skipped",
                "transcript_file": None,
                "summary_status": "pending",
                "ready_for_summary": False,
                "md_path": None,
                "download_seconds": None,
                "whisper_seconds": None,
                "error": None,
            }
        )

    batch = {
        "batch_id": str(uuid.uuid4())[:8],
        "work_dir": str(work_dir),
        "scratchpad": str(scratchpad),
        "created_at": int(time.time()),
        "whisper_active": None,
        "items": items,
    }
    path = save_batch(scratchpad, batch)
    emit(
        {
            "status": "ok",
            "batch_id": batch["batch_id"],
            "batch_file": str(path),
            "count": len(items),
            "agent_action": "batch_probe",
        }
    )


def cmd_batch_probe(args: argparse.Namespace) -> None:
    work_dir = Path(args.dir).resolve()
    scratchpad = Path(args.scratchpad).resolve() if args.scratchpad else default_scratchpad(work_dir)
    batch = load_batch(scratchpad)

    for item in batch["items"]:
        if item.get("probe_status") not in {None, "pending"}:
            continue
        url = item["url"]
        ref = parse_media_ref(url)
        if ref:
            item["platform"] = ref["platform"]
            item["video_id"] = ref["video_id"]
            hit = lookup(work_dir, platform=ref["platform"], video_id=ref["video_id"])
            if hit and not args.force:
                item["probe_status"] = "cached"
                item["summary_status"] = "cached"
                item["md_path"] = hit["path"]
                item["title"] = hit.get("title") or ""
                item["ready_for_summary"] = False
                continue

        if item.get("platform") == "unknown":
            item["probe_status"] = "error"
            item["summary_status"] = "skipped"
            item["error"] = "unsupported platform"
            continue

        t0 = time.time()
        payload, _code = run_json_script(
            "fetch_video.py",
            ["--url", url, "--scratchpad", str(scratchpad)],
        )
        item["download_seconds"] = round(time.time() - t0, 1)
        item["title"] = payload.get("title") or item.get("title") or ""
        item["video_id"] = str(payload.get("video_id") or item.get("video_id") or "")
        item["platform"] = payload.get("platform") or item.get("platform")
        item["webpage_url"] = payload.get("webpage_url") or url
        item["language"] = payload.get("language") or ""
        item["duration_string"] = payload.get("duration_string") or ""
        item["uploader"] = payload.get("uploader") or ""
        item["upload_date"] = payload.get("upload_date") or ""

        status = payload.get("status")
        item["probe_status"] = status or "error"
        if status == "ok":
            item["transcript_file"] = payload.get("transcript_file")
            item["subtitle_type"] = payload.get("subtitle_type")
            item["summary_status"] = "pending"
            item["ready_for_summary"] = True
            item["audio_status"] = "skipped"
            item["whisper_status"] = "skipped"
        elif status == "no_srt":
            item["summary_status"] = "blocked"
            item["ready_for_summary"] = False
            item["audio_status"] = "pending"
            item["whisper_status"] = "pending"
            item["whisper_consent"] = None
        else:
            item["summary_status"] = "skipped"
            item["ready_for_summary"] = False
            item["error"] = payload.get("error") or payload.get("hint") or "probe failed"
            item["hint"] = payload.get("hint")

    save_batch(scratchpad, batch)
    emit(_batch_status_payload(batch))


def cmd_batch_set_consent(args: argparse.Namespace) -> None:
    scratchpad = Path(args.scratchpad).resolve()
    batch = load_batch(scratchpad)
    consent = args.consent in {"yes", "true", "1", "y"}

    targets: list[str] = []
    if args.all:
        targets = [
            str(i["video_id"])
            for i in batch["items"]
            if i.get("probe_status") == "no_srt" and i.get("whisper_consent") is None
        ]
    if args.video_id:
        targets.extend(args.video_id)

    updated = []
    for vid in targets:
        item = item_by_id(batch, vid)
        if not item:
            continue
        item["whisper_consent"] = consent
        if consent:
            item["audio_status"] = "pending"
            item["whisper_status"] = "pending"
            item["summary_status"] = "pending"
        else:
            item["audio_status"] = "skipped"
            item["whisper_status"] = "skipped"
            item["summary_status"] = "skipped"
            item["ready_for_summary"] = False
        updated.append(vid)

    save_batch(scratchpad, batch)
    payload = _batch_status_payload(batch)
    payload["updated"] = updated
    payload["consent"] = consent
    emit(payload)


def _whisper_queue(batch: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        i
        for i in batch["items"]
        if i.get("probe_status") == "no_srt"
        and i.get("whisper_consent") is True
        and i.get("whisper_status") not in {"done", "error", "skipped"}
    ]


def _batch_status_payload(batch: dict[str, Any]) -> dict[str, Any]:
    awaiting = [
        {
            "video_id": i.get("video_id"),
            "title": i.get("title"),
            "url": i.get("url"),
        }
        for i in batch["items"]
        if i.get("probe_status") == "no_srt" and i.get("whisper_consent") is None
    ]

    # Hard gate: while any no_srt lacks consent, do not recommend summarize/transcribe.
    gated = bool(awaiting)

    summarize_ready = []
    if not gated:
        for i in batch["items"]:
            if i.get("ready_for_summary") and i.get("summary_status") == "pending":
                summarize_ready.append(
                    {
                        "video_id": i.get("video_id"),
                        "title": i.get("title"),
                        "url": i.get("url"),
                        "platform": i.get("platform"),
                        "transcript_file": i.get("transcript_file"),
                        "subtitle_type": i.get("subtitle_type"),
                        "language": i.get("language"),
                        "webpage_url": i.get("webpage_url") or i.get("url"),
                        "uploader": i.get("uploader"),
                        "duration_string": i.get("duration_string"),
                        "upload_date": i.get("upload_date"),
                        "download_seconds": i.get("download_seconds"),
                        "whisper_seconds": i.get("whisper_seconds"),
                    }
                )

    queue = _whisper_queue(batch)
    whisper_active = batch.get("whisper_active")

    transcribe_ready = None
    prefetch_download = None
    if not gated:
        for i in queue:
            if i.get("whisper_status") == "running" or (
                whisper_active and str(i.get("video_id")) == str(whisper_active)
            ):
                continue
            if i.get("audio_status") == "ready" and i.get("audio_file"):
                if not whisper_active and transcribe_ready is None:
                    transcribe_ready = {
                        "video_id": i.get("video_id"),
                        "title": i.get("title"),
                        "url": i.get("url"),
                        "audio_file": i.get("audio_file"),
                        "language": i.get("language"),
                    }
            elif i.get("audio_status") in {"pending", "error", None}:
                if prefetch_download is None:
                    prefetch_download = {
                        "video_id": i.get("video_id"),
                        "title": i.get("title"),
                        "url": i.get("url"),
                    }

    # B5: allow prefetch even while whisper is active or summaries are running.
    # Prefer next item after the one currently being transcribed if possible.
    if not gated and prefetch_download is None:
        pass
    elif not gated and whisper_active and prefetch_download:
        # already set to first needing audio — good for overlap with whisper/summary
        pass

    actions: list[dict[str, Any]] = []
    if gated:
        actions.append(
            {
                "type": "ask_whisper_consent",
                "items": awaiting,
                "message": "存在无字幕条目，先一次性询问用户；确认前不要总结任何条目",
            }
        )
    else:
        for s in summarize_ready:
            actions.append({"type": "summarize", **s})
        if prefetch_download:
            actions.append(
                {
                    "type": "download_audio",
                    "overlap_ok": True,
                    "note": "可与 summarize / 当前 whisper 并行（B5）；全批仍最多 1 路 whisper",
                    **prefetch_download,
                }
            )
        if transcribe_ready:
            actions.append(
                {
                    "type": "transcribe",
                    "overlap_ok": False,
                    "note": "全批同一时刻最多 1 个 Whisper",
                    **transcribe_ready,
                }
            )

    done = sum(1 for i in batch["items"] if i.get("summary_status") in {"done", "cached"})
    skipped = sum(1 for i in batch["items"] if i.get("summary_status") == "skipped")
    total = len(batch["items"])

    return {
        "status": "ok",
        "batch_id": batch.get("batch_id"),
        "gated": gated,
        "whisper_active": whisper_active,
        "awaiting_consent": awaiting,
        "summarize_ready": summarize_ready,
        "transcribe_ready": transcribe_ready,
        "prefetch_download": prefetch_download,
        "actions": actions,
        "progress": {
            "total": total,
            "done": done,
            "skipped": skipped,
            "pending": total - done - skipped,
        },
        "items": [
            {
                "video_id": i.get("video_id"),
                "title": i.get("title"),
                "upload_date": i.get("upload_date"),
                "probe_status": i.get("probe_status"),
                "whisper_consent": i.get("whisper_consent"),
                "audio_status": i.get("audio_status"),
                "whisper_status": i.get("whisper_status"),
                "summary_status": i.get("summary_status"),
                "ready_for_summary": i.get("ready_for_summary"),
                "md_path": i.get("md_path"),
                "error": i.get("error"),
            }
            for i in batch["items"]
        ],
        "agent_action": (
            "ask_whisper"
            if gated
            else (
                "work"
                if actions
                else "finalize"
            )
        ),
    }


def cmd_batch_status(args: argparse.Namespace) -> None:
    scratchpad = Path(args.scratchpad).resolve()
    batch = load_batch(scratchpad)
    emit(_batch_status_payload(batch))


def cmd_batch_mark_summary(args: argparse.Namespace) -> None:
    scratchpad = Path(args.scratchpad).resolve()
    batch = load_batch(scratchpad)
    item = item_by_id(batch, args.video_id)
    if not item:
        emit({"status": "error", "error": f"unknown video_id {args.video_id}"}, 1)
    if args.status == "running":
        item["summary_status"] = "running"
        item["ready_for_summary"] = False
    elif args.status == "done":
        item["summary_status"] = "done"
        item["ready_for_summary"] = False
        if args.markdown:
            item["md_path"] = args.markdown
    elif args.status == "skipped":
        item["summary_status"] = "skipped"
        item["ready_for_summary"] = False
    save_batch(scratchpad, batch)
    emit(_batch_status_payload(batch))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_dir(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dir", default=".", help="Work dir with output/")

    def add_scratch(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--scratchpad",
            help="Scratch dir (default: <dir>/.scratchpad/video_summary)",
        )

    c = sub.add_parser("check", help="A3 index lookup")
    add_dir(c)
    c.add_argument("--url", required=True)
    c.add_argument("--force", action="store_true")

    c = sub.add_parser("probe", help="Index check + fetch_video")
    add_dir(c)
    add_scratch(c)
    c.add_argument("--url", required=True)
    c.add_argument("--force", action="store_true")

    c = sub.add_parser("download-audio", help="fetch_audio wrapper")
    add_dir(c)
    add_scratch(c)
    c.add_argument("--url", required=True)
    c.add_argument("--batch", action="store_true", help="Update batch_state.json")

    c = sub.add_parser("transcribe", help="Whisper + compact → transcript txt")
    add_dir(c)
    add_scratch(c)
    c.add_argument("--audio", required=True)
    c.add_argument("--video-id")
    c.add_argument("--language", default="")
    c.add_argument("--model", default="large-v3-turbo")
    c.add_argument("--backend", default="auto")
    c.add_argument("--heartbeat-seconds", type=float, default=2.0)
    c.add_argument("--batch", action="store_true")

    c = sub.add_parser("register", help="Register markdown into index")
    add_dir(c)
    add_scratch(c)
    c.add_argument("--markdown", required=True)
    c.add_argument("--url", default="")
    c.add_argument("--platform")
    c.add_argument("--video-id")
    c.add_argument("--title", default="")
    c.add_argument("--webpage-url", default="")
    c.add_argument(
        "--subtitle-file",
        default="",
        help="Required unless MD already has ## 原文字幕; original SRT to append",
    )
    c.add_argument("--zh-subtitle-file", default="", help="Chinese SRT to append after register")

    c = sub.add_parser("finalize", help="Optional rebuild-index + build_html")
    add_dir(c)
    c.add_argument("--rebuild-index", action="store_true")

    c = sub.add_parser("rebuild-index", help="Scan output/*.md into .index.json")
    add_dir(c)

    c = sub.add_parser("batch-init", help="Create batch state for multiple URLs")
    add_dir(c)
    add_scratch(c)
    c.add_argument("--url", action="append", default=[])
    c.add_argument("--urls-file")

    c = sub.add_parser("batch-probe", help="Probe all pending items in batch")
    add_dir(c)
    add_scratch(c)
    c.add_argument("--force", action="store_true")

    c = sub.add_parser("batch-set-consent", help="Set Whisper consent for no_srt items")
    add_scratch(c)
    c.add_argument("--consent", required=True, choices=["yes", "no"])
    c.add_argument("--video-id", action="append", default=[])
    c.add_argument("--all", action="store_true")

    c = sub.add_parser("batch-status", help="B5-aware next actions")
    add_scratch(c)

    c = sub.add_parser("batch-mark-summary", help="Update summary status in batch")
    add_scratch(c)
    c.add_argument("--video-id", required=True)
    c.add_argument("--status", required=True, choices=["running", "done", "skipped"])
    c.add_argument("--markdown", default="")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "check": cmd_check,
        "probe": cmd_probe,
        "download-audio": cmd_download_audio,
        "transcribe": cmd_transcribe,
        "register": cmd_register,
        "finalize": cmd_finalize,
        "rebuild-index": cmd_rebuild_index,
        "batch-init": cmd_batch_init,
        "batch-probe": cmd_batch_probe,
        "batch-set-consent": cmd_batch_set_consent,
        "batch-status": cmd_batch_status,
        "batch-mark-summary": cmd_batch_mark_summary,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
