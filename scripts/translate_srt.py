#!/usr/bin/env python3
"""SRT 中文翻译辅助：export 导出「序号<TAB>原文」待翻译行；build 用译文行重建 .zh.srt。

翻译由模型完成，脚本只负责拆装与校验，保证中文 SRT 与原文 SRT 时间轴逐条对齐。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_cues(text: str) -> list[tuple[str, str]]:
    """解析 SRT 为 [(时间轴行, 单行文本)]，跳过空条目；顺序即对齐序号。"""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [l for l in block.split("\n") if l.strip()]
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        txt = re.sub(r"\s+", " ", " ".join(lines[ti + 1:])).strip()
        if txt:
            cues.append((lines[ti].strip(), txt))
    return cues


def fail(msg: str, **extra) -> None:
    print(json.dumps({"status": "error", "error": msg, **extra}, ensure_ascii=False))
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="导出待翻译行")
    ex.add_argument("--srt", required=True, type=Path, help="原文 SRT")
    ex.add_argument("--out", required=True, type=Path, help="导出的「序号<TAB>原文」文件")
    bd = sub.add_parser("build", help="用译文行重建中文 SRT")
    bd.add_argument("--srt", required=True, type=Path, help="原文 SRT（提供时间轴）")
    bd.add_argument("--lines", required=True, type=Path, help="「序号<TAB>中文」译文文件")
    bd.add_argument("--out", required=True, type=Path, help="输出的中文 SRT")
    args = ap.parse_args()

    if not args.srt.exists():
        fail(f"srt 不存在: {args.srt}")
    cues = parse_cues(args.srt.read_text(encoding="utf-8", errors="ignore"))
    if not cues:
        fail("SRT 无有效字幕条目")

    if args.cmd == "export":
        out = "\n".join(f"{i + 1}\t{t}" for i, (_, t) in enumerate(cues)) + "\n"
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(out, encoding="utf-8")
        print(json.dumps({"status": "ok", "out": str(args.out), "cues": len(cues),
                          "chars": len(out)}, ensure_ascii=False))
        return

    if not args.lines.exists():
        fail(f"译文文件不存在: {args.lines}")
    trans: dict[int, str] = {}
    for ln in args.lines.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        m = re.match(r"\s*(\d+)\t(.+)", ln)
        if not m:
            fail("译文行格式错误（应为 序号<TAB>中文）", line=ln[:80])
        trans[int(m.group(1))] = m.group(2).strip()
    missing = [i + 1 for i in range(len(cues)) if not trans.get(i + 1)]
    extra = sorted(k for k in trans if k > len(cues))
    if missing:
        fail("译文缺行，补齐后重跑 build", missing=missing[:20], missing_count=len(missing))
    if extra:
        fail("译文序号超出原文条数", extra=extra[:20], cues=len(cues))
    blocks = [f"{i + 1}\n{time}\n{trans[i + 1]}\n" for i, (time, _) in enumerate(cues)]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(blocks), encoding="utf-8")
    print(json.dumps({"status": "ok", "out": str(args.out), "cues": len(cues)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
