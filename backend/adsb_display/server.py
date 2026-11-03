"""HTTP 服务：静态前端 + REST API + SSE 实时推送（纯标准库实现）。"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import config
from .dataset import scan_replay_datasets
from .state import AircraftStore

log = logging.getLogger("adsb.server")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".map": "application/json",
}


class AppContext:
    """服务运行上下文：状态库 + 数据源(live/replay) + 预热/暂停控制 + 元信息。"""

    def __init__(self, mode: str, store: AircraftStore, source, receiver: dict, dataset_name: str | None,
                 warmup=None, shutdown_cb=None):
        self.mode = mode  # "live" | "replay"
        self.store = store
        self.source = source  # LiveCollector | ReplaySource | None
        self.receiver = receiver
        self.dataset_name = dataset_name
        self.started_at = time.time()
        self.state_version = 0
        self.warmup = warmup          # Warmup 实例（live 模式）
        self.display_paused = False   # True: 后台继续采集, 前端停止映射
        self.shutdown_cb = shutdown_cb or (lambda: None)

    def clock_now(self) -> float:
        if self.mode == "replay" and self.source is not None:
            return self.source.current_time()
        return time.time()

    def status(self) -> dict:
        st = {
            "mode": self.mode,
            "dataset": self.dataset_name,
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1),
            "receiver": self.receiver,
            "aircraft_count": self.store.aircraft_count(),
            "display_paused": self.display_paused,
        }
        if self.warmup is not None:
            st["warmup"] = self.warmup.status()
        if self.source is not None and hasattr(self.source, "status"):
            st["source"] = self.source.status()
        return st

    def snapshot(self) -> dict:
        now = self.clock_now()
        return {
            "now": now,
            "status": self.status(),
            "aircraft": self.store.snapshot(now),
        }


class Handler(BaseHTTPRequestHandler):
    ctx: AppContext  # 由 make_server 注入（类变量）
    protocol_version = "HTTP/1.1"
    server_version = "ADS-BDisplay/1.0"

    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: N802
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                self._json({"ok": True})
            elif path == "/api/state":
                self.ctx.state_version += 1
                self._json(self.ctx.snapshot())
            elif path == "/api/stream":
                self._handle_stream()
            elif path == "/api/track":
                qs = parse_qs(parsed.query)
                hx = (qs.get("hex") or [""])[0].strip().lower()
                limit = int((qs.get("limit") or ["0"])[0] or 0)
                if not hx:
                    self._json({"error": "missing hex"}, 400)
                    return
                trk = self.ctx.store.track(hx, limit=limit or None)
                self._json(trk or {"hex": hx, "count": 0, "points": []})
            elif path == "/api/datasets":
                items = []
                for d in scan_replay_datasets():
                    items.append({
                        "name": d.name,
                        "kind": d.kind,
                        "files": [f.name for f in d.files],
                        "size_mb": round(d.size_mb, 3),
                    })
                self._json({"datasets": items})
            elif path == "/api/meta":
                self._json(self.ctx.status())
            else:
                self._serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("GET %s 处理异常", path)
            try:
                self._json({"error": "internal error"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/system/shutdown":
            log.info("收到前端终止请求，正在关闭服务…")
            self._json({"ok": True, "message": "shutting down"})
            threading.Thread(target=self.ctx.shutdown_cb, daemon=True).start()
            return
        if path == "/api/display/pause":
            self.ctx.display_paused = True
            self._json({"ok": True, "display_paused": True})
            return
        if path == "/api/display/resume":
            self.ctx.display_paused = False
            self._json({"ok": True, "display_paused": False})
            return
        if path == "/api/warmup/skip":
            if self.ctx.warmup is None:
                self._json({"error": "warmup not active"}, 400)
                return
            changed = self.ctx.warmup.skip()
            self._json({"ok": True, "skipped": changed, "warmup": self.ctx.warmup.status()})
            return
        if path.startswith("/api/replay/"):
            if self.ctx.mode != "replay" or self.ctx.source is None:
                self._json({"error": "not in replay mode"}, 400)
                return
            action = path.rsplit("/", 1)[-1]
            src = self.ctx.source
            if action == "pause":
                src.pause()
            elif action == "resume":
                src.resume()
            elif action == "speed":
                qs = parse_qs(parsed.query)
                try:
                    src.set_speed(float((qs.get("value") or ["1"])[0]))
                except ValueError:
                    self._json({"error": "bad speed"}, 400)
                    return
            elif action == "seek":
                qs = parse_qs(parsed.query)
                try:
                    idx = int((qs.get("index") or ["0"])[0])
                except ValueError:
                    self._json({"error": "bad index"}, 400)
                    return
                src.seek(idx)
            else:
                self._json({"error": "unknown action"}, 404)
                return
            self._json({"ok": True, "source": src.status()})
        else:
            self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------------
    def _handle_stream(self) -> None:
        """SSE：每秒推送一次全量状态快照；显示暂停期间仅发心跳保持连接。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        log.info("SSE 客户端接入: %s", self.address_string())
        try:
            while True:
                if self.ctx.display_paused:
                    # 暂停期间不发航空器快照（前端冻结画面），仅心跳证明连接存活
                    payload = json.dumps({
                        "now": self.ctx.clock_now(),
                        "heartbeat": True,
                        "status": {"mode": self.ctx.mode, "display_paused": True},
                    }, ensure_ascii=False)
                else:
                    payload = json.dumps(self.ctx.snapshot(), ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.info("SSE 客户端断开: %s", self.address_string())
        finally:
            try:
                self.wfile.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            fp = config.FRONTEND_DIR / "index.html"
        else:
            rel = path.lstrip("/")
            fp = (config.FRONTEND_DIR / rel).resolve()
            # 防目录穿越
            try:
                fp.relative_to(config.FRONTEND_DIR.resolve())
            except ValueError:
                self._send(403, b"forbidden", "text/plain")
                return
        if not fp.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = MIME.get(fp.suffix.lower(), "application/octet-stream")
        self._send(200, fp.read_bytes(), ctype)


def make_server(ctx: AppContext, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True
    return httpd
