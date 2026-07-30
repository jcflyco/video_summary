#!/usr/bin/env python3
"""把 SRT 追加到总结 Markdown 的「## 原文字幕」或「## 中文字幕」小节。

由脚本直接读写文件，SRT 内容不经过模型上下文。幂等：已存在同名小节时跳过。
若文件已有「## 运行统计」，则插在该节之前，避免统计回填覆盖字幕。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--md", required=True, type=Path, help="总结 Markdown 文件")
    parser.add_argument("--srt", required=True, type=Path, help="SRT 文件")
    parser.add_argument("--heading", default="原文字幕", choices=["原文字幕", "中文字幕"],
                        help="追加的小节名（默认 原文字幕）")
    args = parser.parse_args()
    heading = f"## {args.heading}"

    if not args.md.exists():
        print(json.dumps({"status": "error", "error": f"markdown 不存在: {args.md}"},
                         ensure_ascii=False))
        raise SystemExit(1)
    if not args.srt.exists():
        print(json.dumps({"status": "error", "error": f"srt 不存在: {args.srt}"},
                         ensure_ascii=False))
        raise SystemExit(1)

    srt_text = args.srt.read_text(encoding="utf-8", errors="ignore").strip()
    if not srt_text:
        print(json.dumps({"status": "error", "error": "srt 文件为空"}, ensure_ascii=False))
        raise SystemExit(1)

    md_text = args.md.read_text(encoding="utf-8")
    if heading in md_text:
        print(json.dumps({"status": "skip", "reason": f"已存在「{args.heading}」小节"},
                         ensure_ascii=False))
        return

    fence = "````" if "```" in srt_text else "```"
    block = f"\n\n{heading}\n\n{fence}srt\n{srt_text}\n{fence}\n"

    # Insert before 运行统计 so later stats patches never sit above SRT by accident.
    stats_m = re.search(r"\n---\n\n## 运行统计\n", md_text)
    if stats_m:
        md_text = md_text[:stats_m.start()] + block + md_text[stats_m.start():]
    else:
        md_text = md_text.rstrip() + block

    args.md.write_text(md_text, encoding="utf-8")
    print(json.dumps({"status": "ok", "md": str(args.md),
                      "appended_bytes": len(block.encode("utf-8")),
                      "inserted_before_stats": bool(stats_m)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
