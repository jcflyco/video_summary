#!/usr/bin/env python3
"""Download audio-only media for Whisper transcription.

Supports: YouTube, Bilibili, Xiaohongshu (yt-dlp + cookies),
Apple Podcasts / Xiaoyuzhou FM (direct enclosure / og:audio).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Reuse resolvers from fetch_video
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_video import (  # noqa: E402
    BROWSERS,
    USER_AGENT,
    BOT_PATTERNS,
    detect_platform,
    fetch_apple_podcasts_info,
    fetch_xiaoyuzhou_info,
    format_duration,
    xiaohongshu_hint,
)

AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".opus", ".ogg", ".wav", ".flac", ".webm"}


def emit(payload: dict, code: int) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def download_url(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Referer": "https://www.xiaoyuzhoufm.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
    except Exception:
        # Fallback curl (some CDN / SSL stacks behave better)
        proc = subprocess.run(
            ["curl", "-sL", "--http1.1", "-A", USER_AGENT, "-o", str(dest), url],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            raise RuntimeError(proc.stderr.strip() or "audio download failed")


def guess_ext(url: str, default: str = ".m4a") -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for ext in AUDIO_EXTS:
        if path.endswith(ext):
            return ext
    if path.endswith(".mp4"):
        return ".m4a"
    return default


def download_direct(info: dict, scratchpad: Path, platform: str) -> Path:
    video_id = str(info.get("id") or "audio")
    audio_url = info.get("audio_url") or ""
    if not audio_url:
        raise RuntimeError("no audio_url in metadata")
    ext = guess_ext(audio_url)
    dest = scratchpad / f"{video_id}{ext}"
    if dest.exists():
        dest.unlink()
    download_url(audio_url, dest)
    if dest.stat().st_size == 0:
        raise RuntimeError("downloaded audio is empty")
    return dest


def run_ytdlp_audio(url: str, scratchpad: Path, video_id: str) -> tuple[Path, str | None]:
    scratchpad.mkdir(parents=True, exist_ok=True)
    # Remove previous audio for this id
    for stale in scratchpad.glob(f"{video_id}.*"):
        if stale.suffix.lower() in AUDIO_EXTS | {".mp4", ".mkv", ".part"}:
            try:
                stale.unlink()
            except OSError:
                pass

    template = str(scratchpad / f"{video_id}.%(ext)s")
    last_error = "yt-dlp audio download failed"
    for browser in BROWSERS:
        args = [
            "yt-dlp",
            "--no-warnings",
            "--newline",
            "-f",
            "bestaudio/best",
            "-x",
            "--audio-format",
            "m4a",
            "--audio-quality",
            "0",
            "--no-playlist",
            "--cookies-from-browser",
            browser,
            "-o",
            template,
            url,
        ]
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode == 0:
            # Prefer m4a, else any audio-like file for this id
            candidates = sorted(
                [
                    p
                    for p in scratchpad.glob(f"{video_id}.*")
                    if p.suffix.lower() in AUDIO_EXTS | {".mp4"}
                ],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                return candidates[0], browser
            last_error = "yt-dlp succeeded but audio file missing"
            continue
        message = (proc.stderr or proc.stdout or "").strip()
        last_error = message or last_error
        if BOT_PATTERNS.search(message) or "cookies" in message.lower():
            continue
        # keep trying other browsers for cookie issues; otherwise raise
        if "ERROR" in message and "cookies" not in message.lower():
            # still try next browser for XHS/YouTube auth variance
            continue
    raise RuntimeError(last_error)


def extract_id(url: str, platform: str, info: dict | None = None) -> str:
    if info and info.get("id"):
        return str(info["id"])
    if platform == "xiaohongshu":
        m = re.search(r"/explore/([0-9a-fA-F]+)", url)
        if m:
            return m.group(1)
    if platform == "xiaoyuzhou":
        m = re.search(r"/episode/([0-9a-fA-F]+)", url)
        if m:
            return m.group(1)
    if platform == "apple_podcasts":
        m = re.search(r"[?&]i=(\d+)", url)
        if m:
            return m.group(1)
    if platform == "youtube":
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", url)
        if m:
            return m.group(1)
    if platform == "bilibili":
        m = re.search(r"(BV[A-Za-z0-9]+)", url)
        if m:
            return m.group(1)
    return "audio"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--scratchpad", required=True)
    args = parser.parse_args()

    platform = detect_platform(args.url)
    if platform == "unknown":
        emit({"status": "error", "error": "unsupported platform"}, 1)

    scratchpad = Path(args.scratchpad)
    scratchpad.mkdir(parents=True, exist_ok=True)

    browser = None
    info: dict = {}
    try:
        if platform == "xiaoyuzhou":
            info = fetch_xiaoyuzhou_info(args.url)
            audio_path = download_direct(info, scratchpad, platform)
        elif platform == "apple_podcasts":
            info = fetch_apple_podcasts_info(args.url)
            audio_path = download_direct(info, scratchpad, platform)
        else:
            hint = xiaohongshu_hint(args.url) if platform == "xiaohongshu" else None
            video_id = extract_id(args.url, platform)
            try:
                audio_path, browser = run_ytdlp_audio(args.url, scratchpad, video_id)
            except RuntimeError as exc:
                err = str(exc)
                payload = {
                    "status": "error",
                    "platform": platform,
                    "video_id": video_id,
                    "error": err,
                }
                if platform == "xiaohongshu":
                    payload["hint"] = hint or (
                        "小红书需要完整链接（含 xsec_token）+ Chrome 已登录 Cookie。"
                    )
                emit(payload, 1)
            info = {
                "id": video_id,
                "title": "",
                "webpage_url": args.url,
            }
            # Best-effort title via dump-json
            try:
                from fetch_video import fetch_info as ytdlp_info

                meta, browser2 = ytdlp_info(args.url)
                browser = browser or browser2
                info.update(
                    {
                        "id": meta.get("id") or video_id,
                        "title": meta.get("title") or "",
                        "uploader": meta.get("uploader") or meta.get("channel"),
                        "duration": meta.get("duration"),
                        "upload_date": meta.get("upload_date") or "",
                        "webpage_url": meta.get("webpage_url") or args.url,
                    }
                )
            except Exception:
                pass
    except (RuntimeError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        emit({"status": "error", "platform": platform, "error": str(exc)}, 1)

    emit(
        {
            "status": "ok",
            "platform": platform,
            "video_id": info.get("id"),
            "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel") or info.get("artist"),
            "duration_string": format_duration(info.get("duration")),
            "upload_date": info.get("upload_date") or "",
            "audio_file": str(audio_path),
            "audio_bytes": audio_path.stat().st_size,
            "webpage_url": info.get("webpage_url") or args.url,
            "cookies_browser": browser or "none",
        },
        0,
    )


if __name__ == "__main__":
    main()
