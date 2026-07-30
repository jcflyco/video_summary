#!/usr/bin/env python3
"""Convert SRT/VTT into a compact, timestamped transcript without losing content."""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path


TIMESTAMP = re.compile(r"(?:(\d+):)?(\d+):(\d+)[.,]\d+")
TAG = re.compile(r"<[^>]+>")
BRACKETED = re.compile(r"\[[^\]]*\]")


def seconds(value: str) -> int | None:
    match = TIMESTAMP.match(value.strip())
    if not match:
        return None
    return int(match.group(1) or 0) * 3600 + int(match.group(2)) * 60 + int(match.group(3))


def stamp(value: int) -> str:
    hours, rest = divmod(value, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def remove_rolling_overlap(previous: str, current: str) -> str:
    """Remove only an exact rolling-caption overlap (safe for manual captions too)."""
    maximum = min(len(previous), len(current))
    for size in range(maximum, 5, -1):
        if previous[-size:] == current[:size]:
            return current[size:].lstrip()
    return current


def parse(source: Path) -> list[tuple[int, str]]:
    text = source.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n")
    cues: list[tuple[int, str]] = []
    previous = ""
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start = seconds(lines[timing_index])
        if start is None:
            continue
        words = [html.unescape(TAG.sub("", line)).strip() for line in lines[timing_index + 1 :]]
        words = [word for word in words if word and not BRACKETED.fullmatch(word)]
        raw_current = re.sub(r"\s+", " ", " ".join(words)).strip()
        if not raw_current:
            continue
        current = remove_rolling_overlap(previous, raw_current)
        if current:
            cues.append((start, current))
            previous = raw_current
    return cues


def compact(cues: list[tuple[int, str]], window: int) -> list[str]:
    output: list[str] = []
    bucket: list[str] = []
    bucket_start: int | None = None

    def flush() -> None:
        if bucket and bucket_start is not None:
            output.append(f"[{stamp(bucket_start)}] " + " ".join(bucket))

    for start, text in cues:
        if bucket_start is not None and start - bucket_start >= window:
            flush()
            bucket, bucket_start = [], None
        if bucket_start is None:
            bucket_start = start
        bucket.append(text)
    flush()
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--window-seconds", type=int, default=30)
    args = parser.parse_args()
    if args.window_seconds < 10:
        parser.error("--window-seconds must be at least 10")

    cues = parse(args.source)
    if not cues:
        raise SystemExit("no readable subtitle cues")
    paragraphs = compact(cues, args.window_seconds)
    args.destination.write_text("\n".join(paragraphs) + "\n", encoding="utf-8")
    original, reduced = args.source.stat().st_size, args.destination.stat().st_size
    reduction = (1 - reduced / original) * 100 if original else 0
    print(
        f"{len(cues)} cues -> {len(paragraphs)} paragraphs; "
        f"{original} -> {reduced} bytes; reduction {reduction:.1f}%"
    )


if __name__ == "__main__":
    main()
