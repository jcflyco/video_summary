#!/usr/bin/env python3
"""Dedup index for video_summary output/*.md (output/.index.json)."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

INDEX_NAME = ".index.json"
INDEX_VERSION = 1

YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/watch\?[^)\s]*?\bv=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})"
)
BILIBILI_RE = re.compile(r"(?:bilibili\.com/video/|b23\.tv/)(BV[A-Za-z0-9]+)")
XHS_RE = re.compile(r"xiaohongshu\.com/explore/([0-9a-fA-F]{24})")
APPLE_RE = re.compile(r"podcasts\.apple\.com/[^)\s]*?[?&]i=(\d+)")
XYZ_RE = re.compile(r"xiaoyuzhoufm\.com/episode/([0-9a-fA-F]+)")
LONGBRIDGE_RE = re.compile(r"longbridge\.(?:com|cn)/(?:[^)\s]*?/)?lives/(\d+)")
X_RE = re.compile(r"(?:x|twitter)\.com/[^)\s]*?status/(\d+)")


def detect_platform(url: str) -> str:
    lower = url.lower()
    host = urllib.parse.urlparse(url).hostname or ""
    host = host.lower().removeprefix("www.").removeprefix("m.")
    if "bilibili.com" in lower or "b23.tv" in lower:
        return "bilibili"
    if "youtube.com" in lower or "youtu.be" in lower:
        return "youtube"
    if "xiaohongshu.com" in lower or "xhslink.com" in lower:
        return "xiaohongshu"
    if "xiaoyuzhoufm.com" in lower:
        return "xiaoyuzhou"
    if "podcasts.apple.com" in lower or host == "itunes.apple.com":
        return "apple_podcasts"
    if "longbridge.com" in lower or "longbridge.cn" in lower:
        return "longbridge"
    if host in ("x.com", "twitter.com", "mobile.twitter.com"):
        return "x"
    return "unknown"


def extract_id_from_url(url: str, platform: str | None = None) -> str | None:
    platform = platform or detect_platform(url)
    if platform == "youtube":
        m = YOUTUBE_RE.search(url) or re.search(
            r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", url
        )
        return m.group(1) if m else None
    if platform == "bilibili":
        m = re.search(r"(BV[A-Za-z0-9]+)", url, re.I)
        return m.group(1) if m else None
    if platform == "xiaohongshu":
        m = re.search(r"/explore/([0-9a-fA-F]{24})", url)
        return m.group(1) if m else None
    if platform == "apple_podcasts":
        m = re.search(r"[?&]i=(\d+)", url)
        return m.group(1) if m else None
    if platform == "xiaoyuzhou":
        m = re.search(r"/episode/([0-9a-fA-F]+)", url)
        return m.group(1) if m else None
    if platform == "longbridge":
        m = re.search(r"/lives/(\d+)", url)
        return m.group(1) if m else None
    if platform == "x":
        m = re.search(r"/status/(\d+)", url)
        return m.group(1) if m else None
    return None


def parse_media_ref(url: str) -> dict[str, str] | None:
    platform = detect_platform(url)
    if platform == "unknown":
        return None
    video_id = extract_id_from_url(url, platform)
    if not video_id:
        return None
    return {"platform": platform, "video_id": video_id}


def index_key(platform: str, video_id: str) -> str:
    return f"{platform}:{video_id}"


def index_path(work_dir: Path) -> Path:
    return work_dir / "output" / INDEX_NAME


def empty_index() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "updated_at": 0, "entries": {}}


def load_index(work_dir: Path) -> dict[str, Any]:
    path = index_path(work_dir)
    if not path.is_file():
        return empty_index()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_index()
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return empty_index()
    data.setdefault("version", INDEX_VERSION)
    data.setdefault("updated_at", 0)
    return data


def save_index(work_dir: Path, data: dict[str, Any]) -> Path:
    out = work_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    data["version"] = INDEX_VERSION
    data["updated_at"] = int(time.time())
    path = index_path(work_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def lookup(
    work_dir: Path,
    *,
    url: str | None = None,
    platform: str | None = None,
    video_id: str | None = None,
) -> dict[str, Any] | None:
    if url and (not platform or not video_id):
        ref = parse_media_ref(url)
        if not ref:
            return None
        platform, video_id = ref["platform"], ref["video_id"]
    if not platform or not video_id:
        return None
    data = load_index(work_dir)
    entry = data["entries"].get(index_key(platform, video_id))
    if not entry:
        return None
    rel = entry.get("path") or ""
    full = work_dir / rel if rel else None
    if full is None or not full.is_file():
        # Stale index entry
        data["entries"].pop(index_key(platform, video_id), None)
        save_index(work_dir, data)
        return None
    result = dict(entry)
    result["abs_path"] = str(full)
    result["key"] = index_key(platform, video_id)
    return result


def register(
    work_dir: Path,
    *,
    platform: str,
    video_id: str,
    path: str | Path,
    url: str = "",
    title: str = "",
    webpage_url: str = "",
) -> dict[str, Any]:
    md = Path(path)
    if not md.is_file():
        raise FileNotFoundError(f"markdown not found: {md}")
    try:
        rel = str(md.resolve().relative_to(work_dir.resolve()))
    except ValueError:
        rel = str(md)
    entry = {
        "platform": platform,
        "video_id": video_id,
        "url": url or webpage_url,
        "webpage_url": webpage_url or url,
        "title": title,
        "path": rel.replace("\\", "/"),
        "mtime": int(md.stat().st_mtime),
    }
    data = load_index(work_dir)
    data["entries"][index_key(platform, video_id)] = entry
    save_index(work_dir, data)
    return entry


def unregister(
    work_dir: Path,
    *,
    platform: str | None = None,
    video_id: str | None = None,
    path: str | None = None,
) -> int:
    data = load_index(work_dir)
    removed = 0
    if platform and video_id:
        if data["entries"].pop(index_key(platform, video_id), None) is not None:
            removed += 1
    if path:
        name = Path(path).name
        for key, entry in list(data["entries"].items()):
            if Path(entry.get("path", "")).name == name:
                data["entries"].pop(key, None)
                removed += 1
    if removed:
        save_index(work_dir, data)
    return removed


def extract_refs_from_markdown(text: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(platform: str, video_id: str) -> None:
        key = index_key(platform, video_id)
        if key in seen:
            return
        seen.add(key)
        found.append({"platform": platform, "video_id": video_id})

    for m in YOUTUBE_RE.finditer(text):
        add("youtube", m.group(1))
    for m in BILIBILI_RE.finditer(text):
        add("bilibili", m.group(1))
    for m in XHS_RE.finditer(text):
        add("xiaohongshu", m.group(1))
    for m in APPLE_RE.finditer(text):
        add("apple_podcasts", m.group(1))
    for m in XYZ_RE.finditer(text):
        add("xiaoyuzhou", m.group(1))
    for m in LONGBRIDGE_RE.finditer(text):
        add("longbridge", m.group(1))
    for m in X_RE.finditer(text):
        add("x", m.group(1))
    return found


def rebuild_index(work_dir: Path) -> dict[str, Any]:
    outdir = work_dir / "output"
    data = empty_index()
    if not outdir.is_dir():
        save_index(work_dir, data)
        return {"status": "ok", "entries": 0, "files": 0, "skipped": 0}

    files = 0
    skipped = 0
    for md in sorted(outdir.glob("*.md")):
        files += 1
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            skipped += 1
            continue
        refs = extract_refs_from_markdown(text)
        if not refs:
            skipped += 1
            continue
        # Prefer first platform link in doc (usually 原链接 / first timestamp).
        ref = refs[0]
        title = md.name.removesuffix("_总结.md").removesuffix(".md")
        url_match = None
        for pattern in (
            YOUTUBE_RE,
            BILIBILI_RE,
            XHS_RE,
            APPLE_RE,
            XYZ_RE,
            LONGBRIDGE_RE,
        ):
            m = pattern.search(text)
            if m:
                # Reconstruct a usable URL from the match span when possible.
                start = text.rfind("(", 0, m.start())
                end = text.find(")", m.end())
                if start != -1 and end != -1 and end > m.start():
                    candidate = text[start + 1 : end]
                    if candidate.startswith("http"):
                        url_match = candidate
                        break
        entry = {
            "platform": ref["platform"],
            "video_id": ref["video_id"],
            "url": url_match or "",
            "webpage_url": url_match or "",
            "title": title,
            "path": f"output/{md.name}",
            "mtime": int(md.stat().st_mtime),
        }
        data["entries"][index_key(ref["platform"], ref["video_id"])] = entry
    save_index(work_dir, data)
    return {
        "status": "ok",
        "entries": len(data["entries"]),
        "files": files,
        "skipped": skipped,
        "index": str(index_path(work_dir)),
    }


def emit(payload: dict[str, Any], code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".", help="Work dir containing output/")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_lookup = sub.add_parser("lookup", help="Lookup by --url or --platform/--video-id")
    p_lookup.add_argument("--url")
    p_lookup.add_argument("--platform")
    p_lookup.add_argument("--video-id")

    p_reg = sub.add_parser("register", help="Register a finished markdown")
    p_reg.add_argument("--markdown", required=True)
    p_reg.add_argument("--url", default="")
    p_reg.add_argument("--platform")
    p_reg.add_argument("--video-id")
    p_reg.add_argument("--title", default="")
    p_reg.add_argument("--webpage-url", default="")

    p_un = sub.add_parser("unregister", help="Remove index entry")
    p_un.add_argument("--platform")
    p_un.add_argument("--video-id")
    p_un.add_argument("--path")

    sub.add_parser("rebuild", help="Rebuild index by scanning output/*.md")
    sub.add_parser("show", help="Print index summary")

    args = parser.parse_args()
    work_dir = Path(args.dir).resolve()

    if args.cmd == "lookup":
        hit = lookup(
            work_dir, url=args.url, platform=args.platform, video_id=args.video_id
        )
        if hit:
            emit({"status": "hit", "entry": hit})
        emit({"status": "miss"}, 0)

    if args.cmd == "register":
        platform = args.platform
        video_id = args.video_id
        if args.url and (not platform or not video_id):
            ref = parse_media_ref(args.url)
            if ref:
                platform = platform or ref["platform"]
                video_id = video_id or ref["video_id"]
        if not platform or not video_id:
            # Fall back to scanning the markdown body.
            text = Path(args.markdown).read_text(encoding="utf-8", errors="ignore")
            refs = extract_refs_from_markdown(text)
            if not refs:
                emit({"status": "error", "error": "cannot resolve platform/video_id"}, 1)
            platform = platform or refs[0]["platform"]
            video_id = video_id or refs[0]["video_id"]
        entry = register(
            work_dir,
            platform=platform,
            video_id=video_id,
            path=args.markdown,
            url=args.url,
            title=args.title,
            webpage_url=args.webpage_url,
        )
        emit({"status": "ok", "entry": entry})

    if args.cmd == "unregister":
        n = unregister(
            work_dir, platform=args.platform, video_id=args.video_id, path=args.path
        )
        emit({"status": "ok", "removed": n})

    if args.cmd == "rebuild":
        emit(rebuild_index(work_dir))

    if args.cmd == "show":
        data = load_index(work_dir)
        emit(
            {
                "status": "ok",
                "path": str(index_path(work_dir)),
                "entries": len(data.get("entries", {})),
                "updated_at": data.get("updated_at"),
            }
        )


if __name__ == "__main__":
    main()
