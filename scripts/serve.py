#!/usr/bin/env python3
"""视频总结本地服务：静态托管工作目录 + 删除总结 API。

- GET  /api/ping    识别本服务（build_html.py 用于区分旧版纯静态服务）
- POST /api/delete  {"file": "xxx_总结.md"}：把 output/ 下该文件移入 output/.trash/
                    （带时间戳前缀，可找回），并重建 视频总结.html 快照
其余请求按静态文件处理，仅监听 127.0.0.1。
"""
import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ----- Longbridge lives 回放地址解析（供内嵌原生播放器现取新 token）-----
LONGBRIDGE_API = "https://m.lbkrs.com/api/forward"
LONGBRIDGE_HEADERS = {
    "x-app-id": "longbridge_sg",
    "x-platform": "web",
    "x-original-app-id": "longbridge",
    "accept-language": "zh-CN",
    "referer": "https://longbridge.com/",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "accept": "application/json, text/plain, */*",
}


def resolve_longbridge_media(live_id):
    """调 Longbridge REST 取该场直播的回放/直播流地址。返回 dict。"""
    if not live_id or not live_id.isdigit():
        return {"status": "error", "error": "非法 live_id"}
    req = urllib.request.Request(
        f"{LONGBRIDGE_API}/v1/lives/{live_id}", headers=LONGBRIDGE_HEADERS)
    try:
        ctx = ssl.create_default_context()
    except Exception:  # noqa: BLE001
        ctx = ssl._create_unverified_context()
    data = None
    for context in (ctx, ssl._create_unverified_context()):
        try:
            with urllib.request.urlopen(req, timeout=20, context=context) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
                break
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), ssl.SSLError):
                continue
            return {"status": "error", "error": f"请求失败：{exc}"}
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return {"status": "error", "error": f"响应异常：{exc}"}
    if data is None:
        return {"status": "error", "error": "SSL 校验失败"}
    live = ((data.get("data") or {}).get("live")) or {}
    replay_url = (live.get("replay_url") or "").strip()
    m3u8 = (live.get("m3u8_live_url") or "").strip()
    if not replay_url and not m3u8:
        return {"status": "error",
                "error": "该场直播暂无回放/直播流（可能仍在直播或未生成回放）"}
    return {
        "status": "ok",
        "replay_url": replay_url or None,
        "m3u8_live_url": m3u8 or None,
        "is_replay": bool(live.get("replay")) and bool(replay_url),
    }


# ----- 回放 MP4 faststart 流式代理 -----
# 长桥回放 MP4 的 moov（元数据）在文件尾部、体积大（数 MB）+ 文件常达 GB 级，
# 浏览器需先把尾部 moov 拉全才能起播，首帧要等数十秒。此代理把 moov 搬到 mdat
# 之前并改写 chunk 偏移（等价 ffmpeg -movflags faststart），mdat 按 Range 从源站
# 边取边转发：浏览器首个 Range 请求即可从本地缓存秒取元数据，且可正常拖动/跳转。
_UNVERIFIED_SSL = ssl._create_unverified_context()
_LB_STREAM_CACHE = {}  # live_id -> faststart 元数据（head/patched moov/布局）


def _lb_open_range(url, start, end):
    hdr = dict(LONGBRIDGE_HEADERS)
    hdr["Range"] = f"bytes={start}-{end}"
    req = urllib.request.Request(url, headers=hdr)
    return urllib.request.urlopen(req, timeout=60, context=_UNVERIFIED_SSL)


def _lb_read_range(url, start, end):
    with _lb_open_range(url, start, end) as resp:
        return resp.read()


def _patch_chunk_offsets(buf, delta):
    """把 moov 内所有 stco/co64 的 chunk 偏移整体 +delta（moov 前移的字节数）。"""
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts",
                  b"mvex", b"udta", b"moof", b"traf"}

    def walk(off, end):
        while off + 8 <= end:
            size = int.from_bytes(buf[off:off + 4], "big")
            typ = bytes(buf[off + 4:off + 8])
            header = 8
            if size == 1:
                size = int.from_bytes(buf[off + 8:off + 16], "big")
                header = 16
            elif size == 0:
                size = end - off
            box_end = off + size
            if size < header or box_end > end:
                return
            if typ in containers:
                walk(off + header, box_end)
            elif typ == b"stco":
                cnt = int.from_bytes(buf[off + header + 4:off + header + 8], "big")
                p = off + header + 8
                for _ in range(cnt):
                    v = int.from_bytes(buf[p:p + 4], "big") + delta
                    buf[p:p + 4] = (v & 0xFFFFFFFF).to_bytes(4, "big")
                    p += 4
            elif typ == b"co64":
                cnt = int.from_bytes(buf[off + header + 4:off + header + 8], "big")
                p = off + header + 8
                for _ in range(cnt):
                    v = int.from_bytes(buf[p:p + 8], "big") + delta
                    buf[p:p + 8] = v.to_bytes(8, "big")
                    p += 8
            off = box_end

    walk(0, len(buf))


def build_lb_faststart(live_id):
    """解析回放 MP4 布局 → 缓存 head + 改写后的 moov + 虚拟文件布局。失败返回 None。"""
    if live_id in _LB_STREAM_CACHE:
        return _LB_STREAM_CACHE[live_id]
    media = resolve_longbridge_media(live_id)
    if media.get("status") != "ok" or not media.get("replay_url"):
        return None
    url = media["replay_url"]
    try:
        with _lb_open_range(url, 0, 63) as resp:
            head = resp.read()
            cr = resp.headers.get("Content-Range", "")
            total = int(cr.split("/")[1]) if "/" in cr else 0
        if total <= 0:
            return None
        off = 0
        mdat_start = mdat_size = None
        while off + 8 <= len(head):
            size = int.from_bytes(head[off:off + 4], "big")
            typ = head[off + 4:off + 8]
            if size == 1:
                size = int.from_bytes(head[off + 8:off + 16], "big")
            if typ == b"mdat":
                mdat_start, mdat_size = off, size
                break
            if size <= 0:
                break
            off += size
        if mdat_start is None or mdat_size is None:
            return None
        moov_start = mdat_start + mdat_size
        moov_size = total - moov_start
        if moov_size <= 0 or moov_size > 256 * 1024 * 1024:
            return None
        moov = bytearray(_lb_read_range(url, moov_start, total - 1))
        if len(moov) < 8 or moov[4:8] != b"moov":
            return None
        head_bytes = head[:mdat_start]          # ftyp(+free)
        _patch_chunk_offsets(moov, moov_size)   # mdat 前移 moov_size 字节
        meta = {
            "url": url, "total": total,
            "head": bytes(head_bytes), "moov": bytes(moov),
            "H": len(head_bytes), "M": moov_size,
            "mdat_start": mdat_start, "mdat_size": mdat_size,
            "v_total": len(head_bytes) + moov_size + mdat_size,
        }
        _LB_STREAM_CACHE[live_id] = meta
        return meta
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        route = self.path.split('?')[0]
        if route == '/api/ping':
            return self._json(200, {'status': 'ok', 'service': 'video_summary'})
        if route == '/api/longbridge':
            res = resolve_longbridge_media(self._query_id())
            return self._json(200 if res.get('status') == 'ok' else 502, res)
        if route == '/api/lb_stream':
            return self._serve_lb_stream(self._query_id())
        return super().do_GET()

    def _query_id(self):
        qs = self.path.split('?', 1)[1] if '?' in self.path else ''
        for kv in qs.split('&'):
            if kv.startswith('id='):
                return urllib.parse.unquote(kv[3:])
        return ''

    def _serve_lb_stream(self, live_id):
        if not live_id or not live_id.isdigit():
            return self._json(400, {'status': 'error', 'error': '非法 id'})
        meta = build_lb_faststart(live_id)
        if not meta:
            return self._json(502, {'status': 'error', 'error': 'faststart 构建失败'})
        lv = meta['v_total']
        a, b, partial = 0, lv - 1, False
        rng = self.headers.get('Range', '')
        if rng.startswith('bytes='):
            spec = rng[6:].split(',')[0].split('-')
            try:
                if spec[0] == '':
                    a, b = lv - int(spec[1]), lv - 1
                else:
                    a = int(spec[0])
                    b = int(spec[1]) if len(spec) > 1 and spec[1] else lv - 1
                partial = True
            except ValueError:
                a, b, partial = 0, lv - 1, False
        a = max(0, a)
        b = min(b, lv - 1)
        if a > b:
            a, b = 0, lv - 1
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(b - a + 1))
        self.send_header('Cache-Control', 'no-store')
        if partial:
            self.send_header('Content-Range', f'bytes {a}-{b}/{lv}')
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            self._write_v_range(meta, a, b)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 浏览器拖动/关闭时中断连接，属正常

    def _write_v_range(self, meta, a, b):
        h, m = meta['H'], meta['M']
        regions = ((0, h, 'head'), (h, h + m, 'moov'),
                   (h + m, meta['v_total'], 'mdat'))
        for rs, re, kind in regions:
            s, e = max(a, rs), min(b, re - 1)
            if s > e:
                continue
            if kind == 'head':
                self.wfile.write(meta['head'][s:e + 1])
            elif kind == 'moov':
                self.wfile.write(meta['moov'][s - h:e - h + 1])
            else:
                o_start = meta['mdat_start'] + (s - (h + m))
                o_end = meta['mdat_start'] + (e - (h + m))
                with _lb_open_range(meta['url'], o_start, o_end) as resp:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        self.wfile.write(chunk)

    def do_HEAD(self):
        if self.path.split('?')[0] == '/api/lb_stream':
            return self._serve_lb_stream(self._query_id())
        return super().do_HEAD()

    def do_POST(self):
        if self.path.split('?')[0] != '/api/delete':
            return self._json(404, {'status': 'error', 'error': 'unknown api'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            name = json.loads(self.rfile.read(n) or b'{}').get('file', '')
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {'status': 'error', 'error': 'bad request body'})
        if (not name or '/' in name or '\\' in name or name.startswith('.')
                or not name.endswith('.md')):
            return self._json(400, {'status': 'error', 'error': '非法文件名'})
        src = os.path.join(self.directory, 'output', name)
        if not os.path.isfile(src):
            return self._json(404, {'status': 'error', 'error': '文件不存在'})
        trash = os.path.join(self.directory, 'output', '.trash')
        os.makedirs(trash, exist_ok=True)
        dst = os.path.join(trash, time.strftime('%Y%m%d_%H%M%S_') + name)
        try:
            shutil.move(src, dst)
        except OSError as e:
            return self._json(500, {'status': 'error', 'error': str(e)})
        try:
            subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, 'index_store.py'),
                            '--dir', self.directory, 'unregister', '--path', name],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        try:
            subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, 'build_html.py'),
                            '--dir', self.directory],
                           capture_output=True, timeout=30)
        except Exception:
            pass  # 快照重建失败不影响删除本身；实时模式下页面已是最新
        return self._json(200, {'status': 'ok', 'trash': dst})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True)
    ap.add_argument('--port', type=int, required=True)
    args = ap.parse_args()
    base = os.path.abspath(args.dir)
    ThreadingHTTPServer(('127.0.0.1', args.port),
                        partial(Handler, directory=base)).serve_forever()


if __name__ == '__main__':
    main()
