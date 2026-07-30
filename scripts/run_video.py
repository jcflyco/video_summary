#!/usr/bin/env python3
"""单视频开场编排：运行统计基线 + 视频 ID 查重 + 字幕/中文轨获取，一次调用完成。

输出 JSON：
- status=duplicate：已有总结，existing 列出命中文件，直接复用即可；
- status=ok：字幕就绪（字段同 fetch_video.py），另含 run_file 供 finalize_video.py 收尾；
- status=no_srt / error：与 fetch_video.py 一致；no_srt 也会写 run_file（Whisper 路径可用）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_video  # noqa: E402


def extract_video_id(url: str) -> str | None:
    m = re.search(
        r"(?:youtube\.com/(?:watch\?[^#\s]*v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})",
        url,
    )
    if m:
        return m.group(1)
    m = re.search(r"bilibili\.com/video/(BV[A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    return None


def find_session_file(scratchpad: Path) -> Path | None:
    """从 scratchpad 路径提取会话 UUID，在 ~/.claude/projects 下定位会话 JSONL。"""
    m = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        str(scratchpad),
    )
    if not m:
        return None
    hits = sorted((Path.home() / ".claude" / "projects").glob(f"*/{m.group(1)}.jsonl"))
    return hits[0] if hits else None


def session_usage(session_file: Path | None) -> dict | None:
    if not session_file:
        return None
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    try:
        with session_file.open(encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                u = (d.get("message") or {}).get("usage")
                if not u:
                    continue
                totals["input"] += u.get("input_tokens", 0)
                totals["output"] += u.get("output_tokens", 0)
                totals["cache_read"] += u.get("cache_read_input_tokens", 0)
                totals["cache_write"] += u.get("cache_creation_input_tokens", 0)
    except OSError:
        return None
    return totals


def dedup_hits(workdir: Path, video_id: str) -> list[str]:
    hits = []
    for pattern in ("output/*.md", "*.md"):
        for p in sorted(workdir.glob(pattern)):
            try:
                if video_id in p.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(str(p))
            except OSError:
                pass
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--dir", default=".", help="工作目录（含 output/）")
    ap.add_argument("--scratchpad", required=True, help="临时文件目录")
    ap.add_argument("--model", default="", help="当前模型名，写入运行统计")
    args = ap.parse_args()

    workdir = Path(args.dir).resolve()
    scratch = Path(args.scratchpad)
    scratch.mkdir(parents=True, exist_ok=True)

    start_epoch = int(time.time())
    start_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    session_file = find_session_file(scratch)
    baseline = session_usage(session_file)

    vid = extract_video_id(args.url)
    if vid:
        hits = dedup_hits(workdir, vid)
        if hits:
            print(json.dumps({"status": "duplicate", "video_id": vid, "existing": hits},
                             ensure_ascii=False, indent=2))
            return

    payload, code = fetch_video.fetch(args.url, scratch)
    real_vid = payload.get("video_id") or vid
    if payload.get("status") == "ok" and real_vid and not vid:
        # 短链（如 b23.tv）此刻才知道真实 ID，补一次查重
        hits = dedup_hits(workdir, real_vid)
        if hits:
            print(json.dumps({"status": "duplicate", "video_id": real_vid, "existing": hits},
                             ensure_ascii=False, indent=2))
            return

    if payload.get("status") in ("ok", "no_srt"):
        run_file = scratch / f"{real_vid or 'unknown'}.run.json"
        run_file.write_text(json.dumps({
            "start_epoch": start_epoch,
            "start_iso": start_iso,
            "session_file": str(session_file) if session_file else None,
            "baseline": baseline,
            "model": args.model,
            "workdir": str(workdir),
            "fetch": payload,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["run_file"] = str(run_file)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
