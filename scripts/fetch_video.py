#!/usr/bin/env python3
"""Fetch media metadata, pick original-language subtitles, download and compact.

Supports: YouTube, Bilibili, Xiaohongshu, Apple Podcasts, Xiaoyuzhou FM, Longbridge lives.
Podcast / XHS notes usually have no timed captions → status no_srt with metadata.
Longbridge lives expose platform transcripts via REST → status ok with a built SRT.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BROWSERS = ("chrome", "safari", "firefox")
BOT_PATTERNS = re.compile(
    r"sign in to confirm|not a bot|cookies-from-browser|login required|confirm you|"
    r"300031|暂时无法浏览|No video formats found",
    re.I,
)
# Bilibili danmaku is not a transcript; never select it.
SKIP_SUB_LANGS = frozenset({"danmaku"})
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Platforms that use yt-dlp subtitle pipeline (cookies + dump-json / list-subs).
YTDLP_PLATFORMS = frozenset({"youtube", "bilibili", "xiaohongshu"})
# Platforms that resolve metadata outside yt-dlp and almost never have timed subs here.
PODCAST_PLATFORMS = frozenset({"apple_podcasts", "xiaoyuzhou"})


def base_lang(code: str) -> str:
    if code.startswith("ai-"):
        return code[3:].split("-")[0].lower()
    return code.split("-")[0].lower()


def matches_lang(code: str, target: str) -> bool:
    return base_lang(code) == target.lower()


def infer_language(title: str, platform: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", title):
        return "zh"
    if re.search(r"[\u3040-\u30ff\uac00-\ud7af]", title):
        return "ja" if re.search(r"[\u3040-\u30ff]", title) else "ko"
    return "en"


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
    return "unknown"


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "00:00"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_text(url: str, timeout: int = 30) -> str:
    return http_get(url, timeout=timeout).decode("utf-8", "ignore")


def usable_tracks(tracks: dict | None) -> dict[str, list]:
    out: dict[str, list] = {}
    for code, formats in (tracks or {}).items():
        if code in SKIP_SUB_LANGS:
            continue
        out[code] = formats if isinstance(formats, list) else []
    return out


def split_ai_tracks(
    manual: dict[str, list], auto: dict[str, list]
) -> tuple[dict[str, list], dict[str, list]]:
    """Bilibili puts AI captions under subtitles as ai-*; treat them as auto."""
    manual_out = dict(manual)
    auto_out = dict(auto)
    for code in list(manual_out):
        if code.startswith("ai-"):
            auto_out.setdefault(code, manual_out.pop(code))
    return manual_out, auto_out


def pick_manual(subtitles: dict[str, list], target: str) -> str | None:
    codes = [c for c in subtitles if not c.startswith("ai-") and c not in SKIP_SUB_LANGS]
    matching = [c for c in codes if matches_lang(c, target)]
    if not matching:
        return None
    for pref in (f"{target}-orig", target, f"{target}-HK", f"{target}-Hans", f"{target}-Hant"):
        for code in matching:
            if code == pref or code.startswith(f"{pref}-"):
                return code
    return matching[0]


def pick_auto(automatic: dict[str, list], target: str, platform: str) -> str | None:
    codes = [c for c in automatic if c not in SKIP_SUB_LANGS]
    if platform == "bilibili":
        for code in codes:
            if code.startswith("ai-") and matches_lang(code, target):
                return code
        for code in codes:
            if code == f"ai-{target}":
                return code
    matching = [c for c in codes if matches_lang(c, target)]
    if not matching:
        return None
    for pref in (f"{target}-orig", target):
        for code in matching:
            if code == pref:
                return code
    orig = [c for c in matching if c.endswith("-orig")]
    if orig:
        return orig[0]
    return matching[0]


ZH_PREFS = ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK")


def pick_zh(tracks: dict[str, list]) -> str | None:
    codes = [c for c in tracks if c not in SKIP_SUB_LANGS and matches_lang(c, "zh")]
    if not codes:
        return None
    for pref in ZH_PREFS:
        for code in codes:
            if code == pref or code.startswith(pref + "-") or code == f"ai-{pref}":
                return code
    return codes[0]


def select_zh_track(info: dict) -> tuple[str, str] | None:
    """挑选中文对照字幕轨：人工中文 > 平台机翻/AI 中文。返回 (code, type)。"""
    manual = usable_tracks(info.get("subtitles"))
    auto = usable_tracks(info.get("automatic_captions"))
    code = pick_zh(manual)
    if code:
        return code, "manual"
    code = pick_zh(auto)
    if code:
        return code, "auto"
    return None


def select_subtitle(info: dict, platform: str) -> tuple[str, str, str, bool] | None:
    """Return (lang_code, subtitle_type, reason, is_auto)."""
    raw_lang = (info.get("language") or "").strip()
    target = base_lang(raw_lang) if raw_lang else infer_language(info.get("title") or "", platform)

    manual, auto = split_ai_tracks(
        usable_tracks(info.get("subtitles")),
        usable_tracks(info.get("automatic_captions")),
    )

    picked = pick_manual(manual, target)
    if picked:
        return picked, "manual", f"人工字幕，原语言 {target}，代码 {picked}", False

    picked = pick_auto(auto, target, platform)
    if picked:
        return (
            picked,
            "auto",
            f"无人工字幕，选用自动字幕 {picked}（原语言 {target}）",
            True,
        )
    return None


def run_ytdlp(args: list[str], skip_download: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["yt-dlp", "--no-warnings", *args]
    if skip_download:
        cmd.insert(1, "--skip-download")
    return subprocess.run(cmd, capture_output=True, text=True)


def dump_json(url: str, browser: str | None) -> dict:
    args = ["--dump-single-json", "--no-playlist"]
    if browser:
        args.extend(["--cookies-from-browser", browser])
    args.append(url)
    proc = run_ytdlp(args)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "yt-dlp failed")
    return json.loads(proc.stdout)


def fetch_info(url: str) -> tuple[dict, str | None]:
    last_error = "yt-dlp failed"
    for browser in BROWSERS:
        try:
            return dump_json(url, browser), browser
        except (RuntimeError, json.JSONDecodeError) as exc:
            message = str(exc)
            last_error = message
            if BOT_PATTERNS.search(message):
                continue
            if "cookies" in message.lower() or "sign in" in message.lower():
                continue
            raise
    raise RuntimeError(last_error)


def parse_list_subs_output(text: str) -> tuple[dict[str, list], dict[str, list]]:
    """Parse yt-dlp --list-subs text into (subtitles, automatic_captions) stubs."""
    manual: dict[str, list] = {}
    auto: dict[str, list] = {}
    bucket: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if "Available automatic captions" in line:
            bucket = "auto"
            continue
        if "Available subtitles" in line:
            bucket = "manual"
            continue
        if bucket is None or not line:
            continue
        if line.lower().startswith("language"):
            continue

        code = line.split()[0]
        if not code or code in SKIP_SUB_LANGS:
            continue
        stub = [{"ext": "srt"}]
        if code.startswith("ai-") or bucket == "auto":
            auto[code] = stub
        else:
            manual[code] = stub
    return manual, auto


def list_subs(url: str, browser: str | None) -> tuple[dict[str, list], dict[str, list]]:
    args = ["--list-subs", "--no-playlist"]
    if browser:
        args.extend(["--cookies-from-browser", browser])
    args.append(url)
    proc = run_ytdlp(args)
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    return parse_list_subs_output(text)


def enrich_subtitles(info: dict, url: str, browser: str | None) -> str | None:
    manual, auto = split_ai_tracks(
        usable_tracks(info.get("subtitles")),
        usable_tracks(info.get("automatic_captions")),
    )
    if manual or auto:
        info["subtitles"] = manual
        info["automatic_captions"] = auto
        return "dump"

    listed_manual, listed_auto = list_subs(url, browser)
    listed_manual, listed_auto = split_ai_tracks(listed_manual, listed_auto)
    info["subtitles"] = listed_manual
    info["automatic_captions"] = listed_auto
    if listed_manual or listed_auto:
        return "list-subs"
    return None


def find_subtitle_file(
    out_dir: Path, video_id: str, lang: str, exclude: set[Path] | None = None
) -> Path:
    skip = exclude or set()
    patterns = [
        f"{video_id}.{lang}.srt",
        f"{video_id}.{lang}.vtt",
        f"{video_id}.srt",
        f"{video_id}.vtt",
    ]
    for name in patterns:
        path = out_dir / name
        if path not in skip and path.exists() and path.stat().st_size > 0:
            return path
    for path in sorted(out_dir.glob(f"{video_id}*{lang}*")):
        if path not in skip and path.suffix in {".srt", ".vtt"} and path.stat().st_size > 0:
            return path
    for path in sorted(out_dir.glob(f"{video_id}.*")):
        if path not in skip and path.suffix in {".srt", ".vtt"} and path.stat().st_size > 0:
            return path
    raise RuntimeError("subtitle file empty or missing")


def find_zh_file(out_dir: Path, video_id: str, lang: str) -> Path | None:
    """严格按语言码定位中文轨产物，找不到返回 None，绝不回退到其他轨。"""
    for name in (f"{video_id}.{lang}.srt", f"{video_id}.{lang}.vtt"):
        path = out_dir / name
        if path.exists() and path.stat().st_size > 0:
            return path
    for path in sorted(out_dir.glob(f"{video_id}*{lang}*")):
        if path.suffix in {".srt", ".vtt"} and path.stat().st_size > 0:
            return path
    return None


def download_subtitles(
    url: str,
    video_id: str,
    langs: list[str],
    out_dir: Path,
    browser: str | None,
    info_json: Path | None = None,
    clean_stale: bool = True,
) -> None:
    """
    一次调用下载多条字幕轨（原文 + 中文对照）。

    - 始终同时带 --write-subs 与 --write-auto-subs：Bilibili 的 ai-zh 挂在普通
      subtitles 桶下，只开 --write-auto-subs 会漏掉。
    - 提供 info_json 时用 --load-info-json 复用元信息，避免重复网络解析。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if clean_stale:
        for stale in out_dir.glob(f"{video_id}.*"):
            if stale.suffix in {".srt", ".vtt", ".ttml", ".srv3", ".srv2", ".srv1", ".json3"}:
                try:
                    stale.unlink()
                except OSError:
                    pass

    template = str(out_dir / "%(id)s.%(ext)s")
    args = [
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        ",".join(langs),
        "--convert-subs",
        "srt",
        "-o",
        template,
    ]
    if browser:
        args.extend(["--cookies-from-browser", browser])
    if info_json is not None:
        args.extend(["--load-info-json", str(info_json)])
    else:
        args.append(url)
    proc = run_ytdlp(args)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "subtitle download failed")


def dedupe_srt(path: Path) -> None:
    """清洗滚动式自动字幕（YouTube/B 站机翻常见累积重复行）。"""
    text = path.read_text(encoding="utf-8", errors="ignore")
    cues: list[tuple[str, str, str]] = []
    prev = ""
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [l for l in block.split("\n") if l.strip()]
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        m = re.match(r"(\S+)\s*-->\s*(\S+)", lines[ti].strip())
        if not m:
            continue
        start, end = m.group(1), m.group(2)
        txt = re.sub(r"\s+", " ", " ".join(lines[ti + 1 :])).strip()
        if not txt:
            continue
        if txt == prev:
            if cues:
                cues[-1] = (cues[-1][0], end, cues[-1][2])
            continue
        if prev and txt.startswith(prev + " "):
            txt = txt[len(prev) :].strip()
        cues.append((start, end, txt))
        prev = txt
    out = "\n".join(f"{i + 1}\n{s} --> {e}\n{t}\n" for i, (s, e, t) in enumerate(cues))
    path.write_text(out, encoding="utf-8")


def compact_subtitle(src: Path, dst: Path, script_dir: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(script_dir / "compact_subtitles.py"), str(src), str(dst)],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def emit(payload: dict, code: int) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def no_srt_payload(
    *,
    platform: str,
    info: dict,
    url: str,
    browser: str | None = None,
    sub_source: str | None = None,
    error: str | None = None,
    hint: str | None = None,
    subtitle_lang_attempted: str | None = None,
) -> dict:
    payload = {
        "status": "no_srt",
        "platform": platform,
        "video_id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel") or info.get("artist"),
        "duration_string": format_duration(info.get("duration")),
        "upload_date": info.get("upload_date") or "",
        "language": info.get("language") or infer_language(info.get("title") or "", platform),
        "webpage_url": info.get("webpage_url") or url,
        "audio_url": info.get("audio_url") or "",
        "subtitle_discovery": sub_source,
        "cookies_browser": browser or "none",
    }
    if error:
        payload["error"] = error
    if hint:
        payload["hint"] = hint
    if subtitle_lang_attempted:
        payload["subtitle_lang_attempted"] = subtitle_lang_attempted
    return payload


def emit_no_srt(**kwargs: object) -> None:
    emit(no_srt_payload(**kwargs), 2)


# ----- Xiaoyuzhou -----


def parse_xiaoyuzhou_id(url: str) -> str | None:
    m = re.search(r"/episode/([0-9a-fA-F]+)", url)
    return m.group(1) if m else None


def fetch_xiaoyuzhou_info(url: str) -> dict:
    eid = parse_xiaoyuzhou_id(url)
    if not eid:
        raise RuntimeError("无法从链接解析小宇宙 episode id")
    page = http_get_text(url)
    title = ""
    audio_url = ""
    duration = None
    uploader = ""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        page,
        re.S,
    )
    if m:
        data = json.loads(m.group(1))
        ep = (data.get("props") or {}).get("pageProps", {}).get("episode") or {}
        title = html.unescape(ep.get("title") or "")
        duration = ep.get("duration")
        uploader = ((ep.get("podcast") or {}).get("title")) or ""
        media = ep.get("media") or {}
        source = (media.get("source") or {}) if isinstance(media, dict) else {}
        audio_url = source.get("url") or (ep.get("enclosure") or {}).get("url") or ""
        if not audio_url:
            media_id = ep.get("mediaKey") or ep.get("transcriptMediaId") or ""
            if media_id and not media_id.startswith("http"):
                audio_url = f"https://media.xyzcdn.net/{media_id}"
    if not title:
        mt = re.search(r'property="og:title" content="([^"]+)"', page)
        title = html.unescape(mt.group(1)) if mt else f"xiaoyuzhou-{eid}"
    if not audio_url:
        ma = re.search(r'property="og:audio" content="([^"]+)"', page)
        audio_url = ma.group(1) if ma else ""
    if not audio_url:
        raise RuntimeError("小宇宙页面未找到公开音频地址（可能已下架或需登录）")
    return {
        "id": eid,
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "language": infer_language(title, "xiaoyuzhou"),
        "webpage_url": f"https://www.xiaoyuzhoufm.com/episode/{eid}",
        "audio_url": audio_url,
        "subtitles": {},
        "automatic_captions": {},
    }


# ----- Apple Podcasts -----


def parse_apple_podcasts_url(url: str) -> tuple[str | None, str | None, str, str | None]:
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    country = parts[0] if parts and len(parts[0]) == 2 else "us"
    collection_id = None
    m_col = re.search(r"/id(\d+)", parsed.path)
    if m_col:
        collection_id = m_col.group(1)
    qs = urllib.parse.parse_qs(parsed.query)
    episode_id = (qs.get("i") or [None])[0]
    slug = None
    if "podcast" in parts:
        idx = parts.index("podcast")
        if idx + 1 < len(parts) and not parts[idx + 1].startswith("id"):
            slug = urllib.parse.unquote(parts[idx + 1])
    return collection_id, episode_id, country, slug


def itunes_lookup(params: dict) -> list[dict]:
    query = urllib.parse.urlencode(params)
    data = json.loads(http_get_text(f"https://itunes.apple.com/lookup?{query}", timeout=30))
    return data.get("results") or []


def normalize_slug(text: str) -> str:
    text = urllib.parse.unquote(text or "").lower()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text


def fetch_rss_enclosure(feed_url: str, slug: str | None, title_hint: str | None) -> dict | None:
    # curl via urllib; some feeds need HTTP/1.1 — retry with curl if SSL fails
    try:
        raw = http_get(feed_url, timeout=45)
    except Exception:
        proc = subprocess.run(
            ["curl", "-sL", "--http1.1", "-A", USER_AGENT, feed_url],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        raw = proc.stdout
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    channel = root.find("channel")
    if channel is None:
        return None
    targets = [normalize_slug(x) for x in (slug, title_hint) if x]
    best = None
    for item in channel.findall("item"):
        title = item.findtext("title") or ""
        enc = item.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        blob = normalize_slug(title)
        score = 0
        for t in targets:
            if t and t in blob:
                score = max(score, len(t))
            if t and blob in t:
                score = max(score, len(blob))
        if score and (best is None or score > best[0]):
            duration = None
            dur_el = item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")
            if dur_el is not None and dur_el.text:
                parts = dur_el.text.strip().split(":")
                try:
                    if len(parts) == 3:
                        duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 2:
                        duration = int(parts[0]) * 60 + int(parts[1])
                    else:
                        duration = int(float(parts[0]))
                except ValueError:
                    duration = None
            best = (
                score,
                {
                    "title": title,
                    "audio_url": enc.get("url"),
                    "duration": duration,
                    "uploader": channel.findtext("title") or "",
                },
            )
    return best[1] if best else None


def fetch_apple_podcasts_info(url: str) -> dict:
    collection_id, episode_id, country, slug = parse_apple_podcasts_url(url)
    if not episode_id:
        raise RuntimeError(
            "Apple Podcasts 链接需指向单集（含 ?i= 单集 ID），不能只用播客主页"
        )
    if not collection_id:
        raise RuntimeError("无法从链接解析 Apple Podcasts collection id")

    countries = []
    for c in (country, "tw", "us", "cn", "hk"):
        if c and c not in countries:
            countries.append(c)

    episode = None
    feed_url = None
    collection_name = ""
    for c in countries:
        results = itunes_lookup(
            {
                "id": collection_id,
                "entity": "podcastEpisode",
                "limit": 200,
                "country": c,
            }
        )
        for r in results:
            if r.get("kind") == "podcast" and r.get("feedUrl"):
                feed_url = r.get("feedUrl")
                collection_name = r.get("collectionName") or collection_name
            if r.get("kind") == "podcast-episode" and str(r.get("trackId")) == str(episode_id):
                episode = r
                break
        if episode:
            break

    audio_url = ""
    title = ""
    duration = None
    uploader = collection_name
    release = ""
    if episode:
        title = episode.get("trackName") or ""
        audio_url = episode.get("episodeUrl") or ""
        uploader = episode.get("collectionName") or uploader
        ms = episode.get("trackTimeMillis")
        if ms:
            duration = int(ms) / 1000.0
        release = (episode.get("releaseDate") or "")[:10].replace("-", "")

    if not audio_url:
        if not feed_url:
            cols = itunes_lookup({"id": collection_id, "country": countries[0]})
            for r in cols:
                if r.get("feedUrl"):
                    feed_url = r["feedUrl"]
                    collection_name = r.get("collectionName") or collection_name
                    break
        if not feed_url:
            raise RuntimeError("无法获取 Apple Podcasts RSS feedUrl")
        rss_hit = fetch_rss_enclosure(feed_url, slug, title or slug)
        if not rss_hit:
            raise RuntimeError(
                f"未在播客最近单集/RSS 中找到 i={episode_id}；请确认链接为单集页"
            )
        title = title or rss_hit["title"]
        audio_url = rss_hit["audio_url"]
        duration = duration or rss_hit.get("duration")
        uploader = uploader or rss_hit.get("uploader") or collection_name

    webpage = (
        f"https://podcasts.apple.com/{country}/podcast/id{collection_id}?i={episode_id}"
    )
    return {
        "id": str(episode_id),
        "title": title or f"apple-{episode_id}",
        "uploader": uploader,
        "duration": duration,
        "upload_date": release,
        "language": infer_language(title or "", "apple_podcasts"),
        "webpage_url": webpage,
        "audio_url": audio_url,
        "collection_id": collection_id,
        "subtitles": {},
        "automatic_captions": {},
    }


# ----- Longbridge lives -----

LONGBRIDGE_API = "https://m.lbkrs.com/api/forward"
LONGBRIDGE_HEADERS = {
    "x-app-id": "longbridge_sg",
    "x-platform": "web",
    "x-original-app-id": "longbridge",
    "accept-language": "zh-CN",
    "referer": "https://longbridge.com/",
    "user-agent": USER_AGENT,
    "accept": "application/json, text/plain, */*",
}


def parse_longbridge_id(url: str) -> str | None:
    m = re.search(r"/lives/(\d+)", url)
    return m.group(1) if m else None


def longbridge_api_get(path: str, timeout: int = 30) -> dict:
    # macOS 自带 Python 常缺 CA 证书：优先系统校验，SSL 失败回退不校验。
    req = urllib.request.Request(f"{LONGBRIDGE_API}/{path}", headers=LONGBRIDGE_HEADERS)
    try:
        ctx = ssl.create_default_context()
    except Exception:  # noqa: BLE001
        ctx = ssl._create_unverified_context()
    for context in (ctx, ssl._create_unverified_context()):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), ssl.SSLError):
                continue
            raise
    raise urllib.error.URLError("SSL verification failed after fallback")


def srt_timestamp(value: float) -> str:
    if value < 0:
        value = 0.0
    total_ms = int(round(value * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _to_float(value: object, default: float) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# Longbridge 平台逐字稿的时间轴比实际视频画面约快 3 秒，统一后移对齐。
LONGBRIDGE_SUBTITLE_OFFSET = 3.0


def longbridge_segments_to_srt(segments: list[dict], dst: Path) -> int:
    """把 Longbridge transcript 段落写成标准 SRT，返回有效段数。"""
    blocks: list[str] = []
    idx = 0
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = _to_float(seg.get("start_time"), 0.0)
        end = _to_float(seg.get("end_time"), start + 4.0)
        if end <= start:
            end = start + 2.0
        # 平台逐字稿比视频快约 3 秒，整体后移对齐（不允许出现负值）。
        start = max(0.0, start + LONGBRIDGE_SUBTITLE_OFFSET)
        end = max(start + 0.5, end + LONGBRIDGE_SUBTITLE_OFFSET)
        idx += 1
        blocks.append(f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n")
    dst.write_text("\n".join(blocks), encoding="utf-8")
    return idx


def longbridge_upload_date(started_at: object) -> str:
    """unix 秒 -> YYYYMMDD（北京时间 UTC+8）；无效返回空串。"""
    try:
        ts = int(started_at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return time.strftime("%Y%m%d", time.gmtime(ts + 8 * 3600))


def fetch_longbridge(url: str, scratchpad: Path) -> tuple[dict, int]:
    """Longbridge 直播：走平台 REST 取逐字稿，自建 SRT + 压缩转写稿。"""
    script_dir = Path(__file__).resolve().parent
    live_id = parse_longbridge_id(url)
    if not live_id:
        return (
            {
                "status": "error",
                "platform": "longbridge",
                "error": "无法从链接解析 Longbridge live_id（需形如 …/lives/<数字>）",
            },
            1,
        )

    net_errors = (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError)

    # 1) 元信息（尽力而为：失败也继续，仅缺标题等）
    title = ""
    uploader = ""
    duration = None
    upload_date = ""
    try:
        meta = longbridge_api_get(f"v1/lives/{live_id}")
        live = ((meta.get("data") or {}).get("live")) or {}
        title = (live.get("title") or "").strip()
        uploader = ((live.get("user") or {}).get("name") or "").strip()
        try:
            duration = int(live.get("duration")) if live.get("duration") else None
        except (TypeError, ValueError):
            duration = None
        upload_date = longbridge_upload_date(live.get("started_at"))
    except net_errors:
        pass

    # 2) 逐字稿（原语言优先 zh-CN，空则回退 en）
    segments: list[dict] = []
    lang_code = "zh-CN"
    for candidate in ("zh-CN", "en"):
        try:
            data = (
                longbridge_api_get(
                    f"social/live_transcripts?live_id={live_id}&language={candidate}"
                ).get("data")
            ) or {}
        except net_errors as exc:
            return (
                {
                    "status": "error",
                    "platform": "longbridge",
                    "video_id": live_id,
                    "title": title,
                    "error": f"Longbridge 字幕接口请求失败：{exc}",
                },
                1,
            )
        segs = data.get("transcripts") or []
        if segs:
            segments = segs
            lang_code = candidate
            break

    if not segments:
        return (
            {
                "status": "error",
                "platform": "longbridge",
                "video_id": live_id,
                "title": title,
                "webpage_url": f"https://longbridge.com/zh-CN/lives/{live_id}",
                "error": "该 Longbridge 直播暂无字幕逐字稿（可能仍在直播/生成中，稍后再试）",
            },
            2,
        )

    scratchpad.mkdir(parents=True, exist_ok=True)
    sub_path = scratchpad / f"{live_id}.{lang_code}.srt"
    seg_count = longbridge_segments_to_srt(segments, sub_path)
    if seg_count == 0 or not sub_path.stat().st_size:
        return (
            {
                "status": "error",
                "platform": "longbridge",
                "video_id": live_id,
                "title": title,
                "error": "Longbridge 字幕段落均为空，无法生成转写稿",
            },
            2,
        )

    txt_path = scratchpad / f"{live_id}.txt"
    try:
        compact_line = compact_subtitle(sub_path, txt_path, script_dir)
    except subprocess.CalledProcessError as exc:
        return (
            {
                "status": "error",
                "platform": "longbridge",
                "video_id": live_id,
                "title": title,
                "error": (getattr(exc, "stderr", None) or str(exc)).strip(),
            },
            1,
        )
    line_count = sum(1 for _ in txt_path.open(encoding="utf-8"))

    return (
        {
            "status": "ok",
            "platform": "longbridge",
            "video_id": live_id,
            "title": title or f"longbridge-{live_id}",
            "uploader": uploader or "长桥直播",
            "duration_string": format_duration(duration),
            "upload_date": upload_date,
            "language": lang_code,
            "subtitle_type": "auto",
            "subtitle_lang": lang_code,
            "subtitle_reason": f"Longbridge 平台字幕（自动生成，{lang_code}）",
            "subtitle_auto": True,
            "subtitle_discovery": "platform-api",
            "subtitle_file": str(sub_path),
            "zh_subtitle_file": None,
            "zh_subtitle_lang": None,
            "zh_subtitle_type": None,
            "transcript_file": str(txt_path),
            "transcript_lines": line_count,
            "compact_stats": compact_line,
            "cookies_browser": "none",
            "webpage_url": f"https://longbridge.com/zh-CN/lives/{live_id}",
        },
        0,
    )


# ----- Xiaohongshu helpers -----


def xiaohongshu_hint(url: str) -> str | None:
    if "xsec_token=" not in url.lower():
        return (
            "小红书短链通常会被风控（error 300031）。请在浏览器打开笔记后，"
            "复制地址栏完整链接（须含 xsec_token），并确保 Chrome 已登录小红书。"
        )
    return None


def fetch_ytdlp(
    url: str,
    platform: str,
    scratchpad: Path,
    *,
    hint: str | None = None,
) -> tuple[dict, int]:
    """YouTube / Bilibili / Xiaohongshu：下载原文字幕 + 可选中文对照轨。"""
    script_dir = Path(__file__).resolve().parent

    try:
        info, browser = fetch_info(url)
    except RuntimeError as exc:
        err = str(exc)
        if platform == "xiaohongshu" and (
            "300031" in err or "No video formats" in err or "暂时无法" in err
        ):
            return (
                {
                    "status": "error",
                    "platform": platform,
                    "error": err,
                    "hint": hint
                    or (
                        "小红书需要完整链接（含 xsec_token）+ Chrome 已登录 Cookie。"
                        "请打开笔记后从地址栏复制完整 URL 重试。"
                    ),
                },
                1,
            )
        return {"status": "error", "platform": platform, "error": err}, 1

    if platform == "xiaohongshu" and not info.get("id"):
        m = re.search(r"/explore/([0-9a-fA-F]+)", url)
        if m:
            info["id"] = m.group(1)

    sub_source = enrich_subtitles(info, url, browser)
    selection = select_subtitle(info, platform)
    if not selection:
        return (
            no_srt_payload(
                platform=platform,
                info=info,
                url=url,
                browser=browser,
                sub_source=sub_source,
                hint=hint if platform == "xiaohongshu" else None,
            ),
            2,
        )

    lang, sub_type, reason, is_auto = selection
    video_id = info.get("id") or "unknown"
    scratchpad.mkdir(parents=True, exist_ok=True)

    info_json: Path | None = None
    if sub_source == "dump":
        info_json = scratchpad / f"{video_id}.info.json"
        info_json.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")

    zh_lang = None
    zh_type = None
    if base_lang(lang) != "zh":
        zh_pick = select_zh_track(info)
        if zh_pick:
            zh_lang, zh_type = zh_pick

    langs = [lang] + ([zh_lang] if zh_lang else [])
    try:
        try:
            download_subtitles(url, video_id, langs, scratchpad, browser, info_json)
        except RuntimeError:
            if len(langs) > 1:
                zh_lang = zh_type = None
                download_subtitles(url, video_id, [lang], scratchpad, browser, info_json)
            else:
                raise
        zh_path = find_zh_file(scratchpad, video_id, zh_lang) if zh_lang else None
        if zh_path:
            dedupe_srt(zh_path)
        sub_path = find_subtitle_file(
            scratchpad, video_id, lang, exclude={zh_path} if zh_path else None
        )
        if is_auto:
            dedupe_srt(sub_path)
        txt_path = scratchpad / f"{video_id}.txt"
        compact_line = compact_subtitle(sub_path, txt_path, script_dir)
        line_count = sum(1 for _ in txt_path.open(encoding="utf-8"))
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        return (
            no_srt_payload(
                platform=platform,
                info=info,
                url=url,
                browser=browser,
                sub_source=sub_source,
                error=detail.strip(),
                hint=hint if platform == "xiaohongshu" else None,
                subtitle_lang_attempted=lang,
            ),
            2,
        )

    return (
        {
            "status": "ok",
            "platform": platform,
            "video_id": video_id,
            "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel"),
            "duration_string": format_duration(info.get("duration")),
            "upload_date": info.get("upload_date") or "",
            "language": info.get("language") or "",
            "subtitle_type": sub_type,
            "subtitle_lang": lang,
            "subtitle_reason": reason,
            "subtitle_auto": is_auto,
            "subtitle_discovery": sub_source,
            "subtitle_file": str(sub_path),
            "zh_subtitle_file": str(zh_path) if zh_path else None,
            "zh_subtitle_lang": zh_lang if zh_path else None,
            "zh_subtitle_type": zh_type if zh_path else None,
            "transcript_file": str(txt_path),
            "transcript_lines": line_count,
            "compact_stats": compact_line,
            "cookies_browser": browser or "none",
            "webpage_url": info.get("webpage_url") or url,
        },
        0,
    )


def fetch(url: str, scratchpad: Path) -> tuple[dict, int]:
    """完整获取流程，返回 (payload, exit_code)；供 CLI 与外部脚本复用。"""
    platform = detect_platform(url)
    if platform == "unknown":
        return {"status": "error", "error": "unsupported platform"}, 1

    if platform == "longbridge":
        return fetch_longbridge(url, scratchpad)

    if platform in PODCAST_PLATFORMS:
        try:
            if platform == "xiaoyuzhou":
                info = fetch_xiaoyuzhou_info(url)
            else:
                info = fetch_apple_podcasts_info(url)
        except (RuntimeError, urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            return {"status": "error", "platform": platform, "error": str(exc)}, 1
        return (
            no_srt_payload(platform=platform, info=info, url=url, sub_source="platform-api"),
            2,
        )

    hint = xiaohongshu_hint(url) if platform == "xiaohongshu" else None
    return fetch_ytdlp(url, platform, scratchpad, hint=hint)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--scratchpad",
        required=True,
        help="Directory for subtitle scratch files (e.g. .scratchpad/video_summary)",
    )
    args = parser.parse_args()

    platform = detect_platform(args.url)
    if platform == "unknown":
        emit({"status": "error", "error": "unsupported platform"}, 1)

    scratchpad = Path(args.scratchpad)

    # Longbridge lives: platform transcripts → ok with a built SRT.
    if platform == "longbridge":
        payload, code = fetch_longbridge(args.url, scratchpad)
        emit(payload, code)

    # Podcast platforms: metadata only → no_srt (Whisper after user consent).
    if platform in PODCAST_PLATFORMS:
        try:
            if platform == "xiaoyuzhou":
                info = fetch_xiaoyuzhou_info(args.url)
            else:
                info = fetch_apple_podcasts_info(args.url)
        except (RuntimeError, urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            emit({"status": "error", "platform": platform, "error": str(exc)}, 1)
        emit_no_srt(platform=platform, info=info, url=args.url, sub_source="platform-api")

    hint = xiaohongshu_hint(args.url) if platform == "xiaohongshu" else None
    payload, code = fetch_ytdlp(args.url, platform, scratchpad, hint=hint)
    emit(payload, code)


if __name__ == "__main__":
    main()
