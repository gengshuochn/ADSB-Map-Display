"""实时采集源：拉起 readsb 子进程（RTL-SDR），每秒轮询 aircraft.json 并落盘 tracks jsonl。"""
from __future__ import annotations

import json
import logging
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
    ):
        self.ds = ds
        self.lat = lat
        self.lon = lon
        self.gain = gain
        self.ppm = ppm
        self.max_range = max_range

        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.restarts = 0
        self.messages_total = 0
        self.last_error: str | None = None
        self.last_poll_ts: float = 0.0

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
            "--max-range", str(self.max_range),
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
                self.proc = subprocess.Popen(
                    self._readsb_cmd(),
                    stdout=lf,
                    stderr=lf,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
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
        while not self._stop.is_set():
            now = time.time()
            # readsb 进程守护
            if self.proc and self.proc.poll() is not None:
                if self.last_error is None:
                    self.last_error = "readsb 进程退出，3 秒后重试"
                    log.warning(self.last_error)
                time.sleep(3.0)
                if self._stop.is_set():
                    break
                self.restarts += 1
                self.last_error = None
                self._spawn()
                continue

            if now >= poll_deadline:
                poll_deadline = now + 1.0
                try:
                    batch = self._poll(aircraft_json, now)
                    self.on_batch and self.on_batch(batch, now)
                except FileNotFoundError:
                    pass  # aircraft.json 尚未生成
                except Exception as e:  # noqa: BLE001
                    log.exception("轮询异常: %s", e)
            self._stop.wait(0.1)

    on_batch = None  # callable(records: list[dict], now: float)

    def _poll(self, path: Path, now: float) -> list[dict]:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        aircraft = data.get("aircraft") or []
        self.messages_total = max(self.messages_total, int(data.get("messages") or 0))
        self.last_poll_ts = now
        # 落盘：与参考采集脚本一致，仅记录带位置的航空器（一行一机，每秒一批）
        with_pos = [a for a in aircraft if a.get("lat") is not None and a.get("lon") is not None]
        if with_pos:
            line = "\n".join(json.dumps(a, separators=(",", ":")) for a in with_pos) + "\n"
            with open(self.tracks_file, "a", encoding="utf-8") as f:
                f.write(line)
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
