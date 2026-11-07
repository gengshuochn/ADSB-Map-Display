"""实时采集源：拉起 readsb 子进程（RTL-SDR），每秒轮询 aircraft.json 并落盘 tracks jsonl。"""
from __future__ import annotations

import json
import logging
import math
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .dataset import DatasetPaths

log = logging.getLogger("adsb.collector")


class LiveCollector:
    def __init__(
        self,
        ds: DatasetPaths,
        lat: float,
        lon: float,
        gain: float = config.DEFAULT_GAIN,
        ppm: int = config.DEFAULT_PPM,
        max_range: int = config.DEFAULT_MAX_RANGE_KM,
        desktop_managed: bool = False,
    ):
        self.ds = ds
        self.lat = lat
        self.lon = lon
        self.gain = gain
        self.ppm = ppm
        self.max_range = max_range
        self.desktop_managed = desktop_managed

        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.restarts = 0
        self.messages_total = 0
        self.last_error: str | None = None
        self.last_poll_ts: float = 0.0
        self.last_snapshot_ts: float | None = None

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.tracks_file = ds.logs / f"tracks-{ts}.jsonl"
        self.readsb_log = ds.logs / f"readsb-{ts}.log"

    # ------------------------------------------------------------------
    def _readsb_cmd(self) -> list[str]:
        return [
            config.READSB_BIN,
            "--device-type", "rtlsdr",
            "--device", "0",
            "--gain", str(self.gain),
            "--ppm", str(self.ppm),
            "--lat", str(self.lat),
            "--lon", str(self.lon),
            # 配置、元数据与地图使用 km；readsb 的 --max-range 使用海里。
            "--max-range", str(self.max_range / 1.852),
            "--write-json", str(self.ds.live),
            "--write-json-every", "1",
            "--dump-beast", f"{self.ds.beast},3600,10",
            "--net",
            "--net-bind-address", "127.0.0.1",
            "--net-ro-port", str(config.READSB_RAW_PORT),
            "--net-sbs-port", "30003",
            "--net-bo-port", "30005",
            "--net-json-port", "30102",
            "--stats-every", "60",
            "--quiet",
        ]

    def start(self) -> None:
        self.ds.ensure()
        self._spawn()
        self._thread = threading.Thread(target=self._run, name="adsb-poll", daemon=True)
        self._thread.start()
        log.info("采集已启动: dataset=%s tracks=%s", self.ds.name, self.tracks_file.name)

    def _spawn(self) -> bool:
        try:
            with open(self.readsb_log, "ab") as lf:
                start_new_session = not self.desktop_managed
                self.proc = subprocess.Popen(
                    self._readsb_cmd(),
                    stdout=lf,
                    stderr=lf,
                    stdin=subprocess.DEVNULL,
                    start_new_session=start_new_session,
                )
            log.info("readsb 已启动 pid=%s (gain=%s lat=%.5f lon=%.5f)", self.proc.pid, self.gain, self.lat, self.lon)
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = f"readsb 启动失败: {e}"
            log.error(self.last_error)
            return False

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        log.info("采集已停止 messages=%s", self.messages_total)

    # ------------------------------------------------------------------
    def _run(self) -> None:
        """轮询线程：每秒读取 aircraft.json，推送回调并追加 tracks jsonl。"""
        aircraft_json = self.ds.live / "aircraft.json"
        poll_deadline = 0.0
        retry_deadline = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now >= poll_deadline:
                poll_deadline = now + 1.0
                try:
                    # 重试等待不阻塞状态老化，也不读取退出进程遗留的快照。
                    if self.proc is None or self.proc.poll() is not None:
                        if self.last_error is None:
                            self.last_error = "readsb 进程退出，3 秒后重试"
                            log.warning(self.last_error)
                        if not retry_deadline:
                            retry_deadline = now + 3.0
                        elif now >= retry_deadline:
                            self.restarts += 1
                            self.last_error = None
                            self._spawn()
                            retry_deadline = now + 3.0
                    else:
                        retry_deadline = 0.0
                        batch = self._poll(aircraft_json, now)
                        if batch is not None and self.on_batch:
                            self.on_batch(batch, self.last_snapshot_ts)
                except FileNotFoundError:
                    pass  # aircraft.json 尚未生成
                except Exception as e:  # noqa: BLE001
                    log.exception("轮询异常: %s", e)
                finally:
                    # 文件缺失、损坏、停止更新或进程重启期间仍按墙钟清理失联目标。
                    if self.on_tick:
                        self.on_tick(time.time())
            self._stop.wait(0.1)

    on_batch = None  # callable(records: list[dict], now: float)
    on_tick = None   # callable(now: float)，独立于解码器是否产生新快照

    def _poll(self, path: Path, now: float) -> list[dict] | None:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        snapshot_ts = data.get("now")
        if (isinstance(snapshot_ts, bool) or not isinstance(snapshot_ts, (int, float))
                or not math.isfinite(snapshot_ts) or snapshot_ts < 0):
            raise ValueError("aircraft.json 缺少有效的快照时间 now")
        self.last_poll_ts = now
        # 重复或倒序快照不能刷新 last_seen、追加航迹或再次落盘。
        if self.last_snapshot_ts is not None and snapshot_ts <= self.last_snapshot_ts:
            return None
        aircraft = data.get("aircraft") or []
        self.messages_total = max(self.messages_total, int(data.get("messages") or 0))
        # 使用解码器原始时刻；seen/seen_pos 均相对于该时刻，不能改成轮询时刻。
        # 保留真实空快照，保证回放时间线完整。
        with_pos = [a for a in aircraft if a.get("lat") is not None and a.get("lon") is not None]
        record = {
            "now": snapshot_ts,
            "aircraft": with_pos,
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with open(self.tracks_file, "a", encoding="utf-8") as f:
            f.write(line)
        self.last_snapshot_ts = snapshot_ts
        return with_pos

    # ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "running": bool(self.proc and self.proc.poll() is None),
            "pid": self.proc.pid if self.proc else None,
            "restarts": self.restarts,
            "messages": self.messages_total,
            "last_error": self.last_error,
            "tracks_file": self.tracks_file.name,
        }
