"""回放源：将已有数据集按 1 Hz 节奏重放，用于无硬件演示与测试。

支持两种 tracks jsonl 格式：
1. 批次化格式（每行一个 {"now":..., "aircraft":[...]} 对象）
2. 平铺格式（参考采集脚本格式：每行一台航空器，每秒追加一批）

平铺格式的批次边界用「hex 重复」启发式恢复：同一秒内同一 hex 只出现一次，
当某行的 hex 已在当前批次出现过，即认为进入了下一秒。
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config
from .dataset import ReplayDataset

log = logging.getLogger("adsb.replay")


class ReplaySource:
    def __init__(self, dataset: ReplayDataset | list[ReplayDataset], speed: float = 1.0, loop: bool = True):
        self.datasets = sorted(dataset if isinstance(dataset, list) else [dataset], key=lambda d: d.sort_key)
        self.dataset = self.datasets[0]
        self.batch_times: list[float] = []
        self.batch_segments: list[int] = []
        self.segment_starts: list[int] = []
        self._active_segment = None
        self.on_segment = None  # callable(dataset)
        self.speed = max(0.1, speed) if math.isfinite(speed) and speed > 0 else 1.0
        self.loop = loop
        self.batches: list[list[dict]] = []
        self.batch_offsets: list[float] = []
        self._seek_version = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cond = threading.Condition()
        self._paused = False
        self._batch_index = 0
        self._t0 = time.time()
        self.on_batch = None   # callable(records, now)
        self.on_reset = None   # callable()

    # ------------------------------------------------------------------
    def load(self) -> int:
        """解析全部回放文件为批次列表及时间戳，返回批次数。"""
        self.batches, self.batch_times, self.batch_offsets = [], [], []
        self.batch_segments, self.segment_starts = [], []
        for segment, ds in enumerate(self.datasets):
            rows = [row for f in ds.files for row in self._parse_file(f)]
            if not rows:
                raise ValueError(f"数据集 {ds.name} 无法解析出回放批次")
            self.segment_starts.append(len(self.batches))
            # 无时间戳的旧格式使用元数据起点，仍兼容历史纯平铺日志。
            anchor = time.time()
            for key in ("created_utc", "created_local"):
                try:
                    anchor = datetime.fromisoformat(ds.meta[key].replace("Z", "+00:00")).timestamp()
                    break
                except (KeyError, ValueError, TypeError, AttributeError):
                    pass
            last = None
            for ts, records in rows:
                valid = isinstance(ts, (float, int)) and math.isfinite(ts)
                now = float(ts) if valid else (last + 1 if last is not None else anchor)
                if last is not None and now < last:
                    raise ValueError(f"数据集 {ds.name} 批次时间倒退")
                step = max(0.1, now - last) if last is not None else 1.0
                self.batch_offsets.append((self.batch_offsets[-1] + step) if self.batch_offsets else 0.0)
                self.batches.append(records)
                self.batch_times.append(now)
                self.batch_segments.append(segment)
                last = now
        return len(self.batches)

    @staticmethod
    def _parse_file(path: Path) -> list[tuple[float | None, list[dict]]]:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return []
        # 1. 尝试直接作为整体 JSON 对象解析（如 readsb 的 history_*.json）
        try:
            doc = json.loads(text)
            if isinstance(doc, dict) and "aircraft" in doc:
                recs = [a for a in doc["aircraft"] if isinstance(a, dict)]
                ts = float(doc["now"]) if "now" in doc else None
                return [(ts, recs)]
        except json.JSONDecodeError:
            pass

        # 2. 逐行解析：支持批次化 jsonl（{"now":..., "aircraft":[...]}）或平铺格式（每行一个 aircraft）
        results: list[tuple[float | None, list[dict]]] = []
        flat_cur: list[dict] = []
        flat_hexes: set[str] = set()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "aircraft" in obj:
                # 批次化格式
                if flat_cur:
                    results.append((None, flat_cur))
                    flat_cur, flat_hexes = [], set()
                recs = [a for a in obj["aircraft"] if isinstance(a, dict)]
                ts = float(obj["now"]) if "now" in obj else None
                results.append((ts, recs))
            elif isinstance(obj, dict) and obj.get("hex"):
                # 平铺格式（旧版本兼容）
                hx = obj["hex"].lower()
                if hx in flat_hexes:
                    results.append((None, flat_cur))
                    flat_cur, flat_hexes = [], set()
                flat_hexes.add(hx)
                flat_cur.append(obj)
        if flat_cur:
            results.append((None, flat_cur))
        return results

    # ------------------------------------------------------------------
    def _time_at(self, index):
        return self.batch_times[index] if self.batch_times else self._t0 + self.batch_offsets[index]

    def _segment_at(self, index):
        return self.batch_segments[index] if self.batch_segments else 0

    def _prepare_segment(self, index, reset=False):
        segment = self._segment_at(index)
        if segment != self._active_segment:
            self._active_segment = segment
            self.dataset = self.datasets[segment]
            if self.on_segment:
                self.on_segment(self.dataset)
            reset = True
        if reset and self.on_reset:
            self.on_reset()

    def _emit(self, index):
        if self.on_batch:
            self.on_batch(self.batches[index], self._time_at(index))

    def start(self) -> None:
        if not self.batches:
            self.load()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="adsb-replay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("回放线程未能停止")

    def pause(self) -> None:
        with self._cond:
            self._paused = True
            self._cond.notify_all()

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def set_speed(self, v: float) -> None:
        if math.isfinite(v) and v > 0:
            with self._cond:
                self.speed = max(0.1, min(60.0, v))
                self._cond.notify_all()

    def seek(self, idx: int) -> None:
        if not self.batches:
            return
        with self._cond:
            target = max(0, min(len(self.batches) - 1, idx))
            self._seek_version += 1
            self._batch_index = target
            self._prepare_segment(target, reset=True)
            segment = self._segment_at(target)
            start = self.segment_starts[segment] if self.segment_starts else 0
            if self.on_reset:
                start = max(start, target - config.TRACK_MAX_POINTS)
                for j in range(start, target + 1):
                    self._emit(j)
            else:
                self._emit(target)
            self._cond.notify_all()

    def current_time(self) -> float:
        with self._cond:
            return self._time_at(self._batch_index) if self.batches else self._t0

    def _run(self) -> None:
        # 回调、跳转与批次索引在同一把锁内提交，不会暴露半次重建快照。
        with self._cond:
            while not self._stop.is_set():
                while self._paused and not self._stop.is_set():
                    self._cond.wait()
                if self._stop.is_set():
                    break
                i = self._batch_index
                version = self._seek_version
                self._prepare_segment(i)
                self._emit(i)
                nxt = i + 1
                remaining = (self.batch_offsets[nxt] - self.batch_offsets[i]) if nxt < len(self.batches) else 1.0
                while remaining > 0 and not self._stop.is_set() and version == self._seek_version:
                    if self._paused:
                        self._cond.wait()
                        continue
                    speed = self.speed
                    before = time.monotonic()
                    self._cond.wait(timeout=remaining / speed)
                    remaining -= (time.monotonic() - before) * speed
                if self._stop.is_set():
                    break
                if version != self._seek_version:
                    continue
                if nxt >= len(self.batches):
                    if not self.loop:
                        self._paused = True
                        continue
                    nxt = 0
                    self._prepare_segment(nxt, reset=True)
                self._batch_index = nxt

    def status(self) -> dict:
        with self._cond:
            return {
                "kind": "replay", "dataset": self.dataset.name,
                "datasets": [d.name for d in self.datasets],
                "dataset_root": str(self.dataset.root),
                "files": [f.name for d in self.datasets for f in d.files],
                "total_batches": len(self.batches), "batch_index": self._batch_index,
                "paused": self._paused, "speed": self.speed, "loop": self.loop,
            }
