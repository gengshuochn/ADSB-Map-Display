"""HTTP 服务：静态前端 + REST API + SSE 实时推送（纯标准库实现）。"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import config, __version__
from .dataset import scan_replay_datasets
from .state import AircraftStore
from .replay import ReplaySource

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
    """采集生命周期独立于显示源；回放快照、跳转和回调共享可重入锁。"""

    def __init__(self, mode: str, store: AircraftStore, source, receiver: dict, dataset_name: str | None,
                 warmup=None, df17_counter=None, shutdown_cb=None):
        self._lock = threading.RLock()
        self._switch_lock = threading.Lock()
        self.display_mode = mode
        self.live_available = mode == "live" and source is not None
        self.store_live = store if mode == "live" else None
        self.store_replay = store if mode == "replay" else None
        self.live_source = source if self.live_available else None
        self.replay_source = source if mode == "replay" else None
        self.live_receiver = dict(receiver)
        self.replay_receiver = dict(receiver)
        self.collecting_dataset = dataset_name if self.live_available else None
        self.dataset_name = dataset_name
        self.started_at = time.time()
        self.session_id = uuid.uuid4().hex
        self.state_version = 0
        self.display_revision = 0
        self.warmup = warmup
        self.df17_counter = df17_counter
        self.display_paused = False
        self.shutdown_cb = shutdown_cb or (lambda: None)
        self._closed = False
        self.replay_loop = source.loop if isinstance(source, ReplaySource) else True
        if isinstance(self.replay_source, ReplaySource):
            self._bind_replay(self.replay_source)

    @property
    def mode(self):
        return self.display_mode

    @property
    def store(self):
        return self.store_live if self.display_mode == "live" else self.store_replay

    @property
    def source(self):
        return self.live_source if self.display_mode == "live" else self.replay_source

    @property
    def receiver(self):
        return self.live_receiver if self.display_mode == "live" else self.replay_receiver

    def _bind_replay(self, src):
        src._cond = threading.Condition(self._lock)

        def segment(ds):
            self.replay_receiver = dict(self.live_receiver)
            metadata = ds.meta.get("receiver")
            if isinstance(metadata, dict):
                for key in ("lat", "lon", "gain", "max_range_km"):
                    value = metadata.get(key)
                    if isinstance(value, (int, float)) and math.isfinite(value):
                        if (key == "lat" and abs(value) > 90) or (key == "lon" and abs(value) > 180):
                            continue
                        self.replay_receiver[key] = value
                if isinstance(metadata.get("source"), str):
                    self.replay_receiver["source"] = metadata["source"]
            self.store_replay = AircraftStore(self.replay_receiver.get("lat", 0), self.replay_receiver.get("lon", 0))
            self.dataset_name = ds.name

        def reset():
            self.store_replay.reset()
            self.display_revision += 1

        def batch(records, now):
            self.store_replay.apply_batch(records, now)
            self.store_replay.prune(now)

        src.on_segment, src.on_reset, src.on_batch = segment, reset, batch

    def clock_now(self) -> float:
        if self.display_mode == "replay" and self.replay_source is not None:
            return self.replay_source.current_time()
        return time.time()

    def status(self) -> dict:
        with self._lock:
            st = {
                "mode": self.display_mode, "display_mode": self.display_mode,
                "display_revision": self.display_revision,
                "session_id": self.session_id,
                "live_available": self.live_available,
                "dataset": self.collecting_dataset if self.display_mode == "live" else self.dataset_name,
                "datasets": [d.name for d in self.replay_source.datasets] if isinstance(self.replay_source, ReplaySource) and self.display_mode == "replay" else [],
                "started_at": self.started_at,
                "uptime_s": round(time.time() - self.started_at, 1),
                "receiver": self.receiver,
                "aircraft_count": self.store.aircraft_count(),
                "display_paused": self.display_paused,
                "collecting_source": self.live_source.status() if self.live_source is not None else None,
            }
            if self.warmup is not None:
                st["warmup"] = self.warmup.status()
            if self.df17_counter is not None:
                st["msgstats"] = self.df17_counter.status()
            if self.source is not None:
                st["source"] = self.source.status()
            return st

    def snapshot(self) -> dict:
        with self._lock:
            now = self.clock_now()
            result = {"now": now, "status": self.status(), "aircraft": []}
            if self.display_paused:
                result["heartbeat"] = True
            else:
                result["aircraft"] = self.store.snapshot(now)
                result["status"]["aircraft_count"] = len(result["aircraft"])
            return result

    def track(self, hx, limit=None):
        with self._lock:
            result = self.store.track(hx, limit) or {"hex": hx, "count": 0, "points": []}
            return dict(result, display_revision=self.display_revision, session_id=self.session_id)

    def datasets(self):
        return [{"name": d.name, "kind": d.kind, "files": [f.name for f in d.files],
                 "size_mb": round(d.size_mb, 3), "created_local": d.meta.get("created_local"),
                 "collecting": d.name == self.collecting_dataset}
                for d in scan_replay_datasets()]

    def switch_replay(self, names):
        if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
            raise ValueError("请选择至少一个有效数据集")
        with self._switch_lock:
            if self._closed:
                raise ValueError("服务正在退出")
            available = {d.name: d for d in scan_replay_datasets()}
            if len(set(names)) != len(names) or any(n not in available or n == self.collecting_dataset for n in names):
                raise ValueError("数据集无效或正在采集中")
            candidate = ReplaySource([available[n] for n in names], loop=self.replay_loop)
            try:
                candidate.load()  # 加载失败时，原回放和显示状态不变。
            except (ValueError, TypeError, AttributeError, KeyError) as exc:
                raise ValueError(f"无法读取回放数据：{exc}") from exc
            old = self.replay_source
            if old is not None:
                old.stop()  # join 不能持有回放共享锁。
            with self._lock:
                self._bind_replay(candidate)
                self.replay_source = candidate
                self.display_mode = "replay"
                self.display_paused = False
                candidate.seek(0)
                candidate.start()
                return self.status()

    def switch_live(self):
        with self._switch_lock:
            if not self.live_available or self._closed:
                raise ValueError("未启动实时采集，无法返回实时")
            if self.replay_source is not None:
                self.replay_source.stop()
            with self._lock:
                self.replay_source = None
                self.display_mode = "live"
                self.display_paused = False
                self.display_revision += 1
                return self.status()

    def close(self):
        with self._switch_lock:
            self._closed = True
            if self.replay_source is not None:
                self.replay_source.stop()




class Handler(BaseHTTPRequestHandler):
    ctx: AppContext  # 由 make_server 注入（类变量）
    protocol_version = "HTTP/1.1"
    server_version = f"ADS-BDisplay/{__version__}"

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
                self._json(self.ctx.snapshot())
            elif path == "/api/stream":
                self._handle_stream()
            elif path == "/api/track":
                qs = parse_qs(parsed.query)
                hx = (qs.get("hex") or [""])[0].strip().lower()
                limit_raw = (qs.get("limit") or ["0"])[0] or "0"
                try:
                    limit = int(limit_raw)
                    if limit < 0 or limit > 100000:
                        self._json({"error": "invalid limit"}, 400)
                        return
                except ValueError:
                    self._json({"error": "bad limit"}, 400)
                    return
                if not hx:
                    self._json({"error": "missing hex"}, 400)
                    return
                trk = self.ctx.track(hx, limit=limit or None)
                self._json(trk or {"hex": hx, "count": 0, "points": []})
            elif path == "/api/datasets":
                self._json({"datasets": self.ctx.datasets()})
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
            req_session = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= 65536:
                    body = json.loads(self.rfile.read(length))
                    if isinstance(body, dict):
                        req_session = body.get("session_id")
            except Exception:
                pass
            if not req_session:
                req_session = self.headers.get("X-Session-ID")
            ctx_session = getattr(self.ctx, "session_id", None)
            if req_session and ctx_session and req_session != ctx_session:
                log.warning("收到过期的 shutdown 请求 (expect=%s, got=%s)，忽略以防误杀新会话",
                            ctx_session, req_session)
                self._json({"ok": False, "error": "session mismatch"}, 409)
                return
            log.info("收到前端/桌面端终止请求，正在关闭服务…")
            self._json({"ok": True, "message": "shutting down"})
            threading.Thread(target=self.ctx.shutdown_cb, daemon=True).start()
            return
        if path in ("/api/display/replay", "/api/display/live"):
            try:
                if path.endswith("/replay"):
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536:
                        raise ValueError("请求正文大小无效")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError("请求正文必须是 JSON 对象")
                    status = self.ctx.switch_replay(body.get("datasets"))
                else:
                    status = self.ctx.switch_live()
                self._json({"ok": True, "status": status})
            except (ValueError, OSError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if path in ("/api/display/pause", "/api/display/resume"):
            with self.ctx._lock:
                if self.ctx.display_mode != "live":
                    self._json({"error": "not in live display mode"}, 400)
                    return
                self.ctx.display_paused = path.endswith("/pause")
                self._json({"ok": True, "display_paused": self.ctx.display_paused})
            return
        if path == "/api/warmup/skip":
            if self.ctx.warmup is None:
                self._json({"error": "warmup not active"}, 400)
                return
            changed = self.ctx.warmup.skip()
            self._json({"ok": True, "skipped": changed, "warmup": self.ctx.warmup.status()})
            return
        if path.startswith("/api/replay/"):
            with self.ctx._switch_lock:
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
                        val = float((qs.get("value") or ["1"])[0])
                        if not math.isfinite(val) or val <= 0:
                            self._json({"error": "bad speed"}, 400)
                            return
                        src.set_speed(val)
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


def make_server(ctx: AppContext, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
