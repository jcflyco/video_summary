#!/usr/bin/env python3
"""单视频收尾编排：写运行统计 + 附原文/中文字幕 + 重建 HTML，一次调用完成。

用法：finalize_video.py --md <总结.md> --run <run.json> [--srt 覆盖原文SRT] [--zh-srt 覆盖中文SRT]
统计数字由脚本直接计算并写入 Markdown，不经过模型上下文；读取失败时写「不可用」。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_video import session_usage  # noqa: E402

STATS_HEADING = "## 运行统计"


def fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分 {seconds % 60} 秒"


def stats_block(run: dict) -> tuple[str, dict]:
    end_epoch = int(time.time())
    end_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    model = run.get("model") or "不可用"
    elapsed = fmt_duration(max(0, end_epoch - int(run.get("start_epoch") or end_epoch)))

    token_line = "不可用"
    baseline = run.get("baseline")
    session_file = run.get("session_file")
    current = session_usage(Path(session_file)) if session_file else None
    if baseline and current:
        delta = {k: current[k] - baseline[k] for k in baseline}
        if min(delta.values()) >= 0:
            total = sum(delta.values())
            token_line = (
                f"输入（非缓存）{delta['input']:,} · 输出 {delta['output']:,} · "
                f"缓存读 {delta['cache_read']:,} · 缓存写 {delta['cache_write']:,} · "
                f"合计 {total:,}"
            )
    block = (
        f"\n\n---\n\n{STATS_HEADING}\n\n"
        f"- 模型：{model}\n"
        f"- 开始时间：{run.get('start_iso') or '不可用'}\n"
        f"- 完成时间：{end_iso}\n"
        f"- 用时：{elapsed}\n"
        f"- Token 用量：{token_line}\n"
    )
    return block, {"end": end_iso, "elapsed": elapsed, "tokens": token_line}


def run_script(script: str, args: list[str]) -> dict:
    script_dir = Path(__file__).resolve().parent
    proc = subprocess.run(
        [sys.executable, str(script_dir / script), *args],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"status": "error", "error": (proc.stderr or proc.stdout).strip()[:300]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--md", required=True, type=Path, help="总结 Markdown")
    ap.add_argument("--run", required=True, type=Path, help="run_video.py 产出的 run.json")
    ap.add_argument("--srt", type=Path, help="覆盖原文 SRT 路径（如 Whisper 产物）")
    ap.add_argument("--zh-srt", dest="zh_srt", type=Path, help="覆盖中文 SRT 路径")
    args = ap.parse_args()

    if not args.md.exists():
        print(json.dumps({"status": "error", "error": f"markdown 不存在: {args.md}"},
                         ensure_ascii=False))
        raise SystemExit(1)
    run = json.loads(args.run.read_text(encoding="utf-8"))
    fetch = run.get("fetch") or {}

    result: dict = {"status": "ok", "md": str(args.md)}

    # 1. 运行统计（幂等：已有小节则跳过）
    md_text = args.md.read_text(encoding="utf-8")
    if STATS_HEADING in md_text:
        result["stats"] = "skip"
    else:
        block, meta = stats_block(run)
        with args.md.open("a", encoding="utf-8") as f:
            f.write(block)
        result["stats"] = meta

    # 2. 附原文字幕
    srt = args.srt or (Path(fetch["subtitle_file"]) if fetch.get("subtitle_file") else None)
    if srt and srt.exists():
        result["srt"] = run_script("append_srt.py",
                                   ["--md", str(args.md), "--srt", str(srt),
                                    "--heading", "原文字幕"])
    else:
        result["srt"] = {"status": "error", "error": "原文 SRT 缺失"}

    # 3. 附平台中文字幕；无平台中文轨时默认跳过，仅在用户明确要求时另走 LLM 翻译流程
    zh = args.zh_srt or (Path(fetch["zh_subtitle_file"]) if fetch.get("zh_subtitle_file") else None)
    if zh and zh.exists():
        result["zh"] = run_script("append_srt.py",
                                  ["--md", str(args.md), "--srt", str(zh),
                                   "--heading", "中文字幕"])
    else:
        result["zh"] = {"status": "none"}

    # 4. 重建 HTML 汇总页
    workdir = run.get("workdir") or str(args.md.resolve().parent.parent)
    result["html"] = run_script("build_html.py", ["--dir", workdir])

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
