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
from pathlib import Path

from . import config
from .dataset import ReplayDataset

log = logging.getLogger("adsb.replay")


class ReplaySource:
    def __init__(self, dataset: ReplayDataset, speed: float = 1.0, loop: bool = True):
        self.dataset = dataset
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
        self.batches = []
        raw_ts: list[float | None] = []
        for f in self.dataset.files:
            for ts, recs in self._parse_file(f):
                self.batches.append(recs)
                raw_ts.append(ts)
        if not self.batches:
            log.warning("数据集 %s 未解析出任何批次", self.dataset.name)
            self.batch_offsets = []
            return 0

        # 计算各批次相对于首批的时间偏移（秒）
        offsets: list[float] = []
        first_valid_ts = next((t for t in raw_ts if t is not None), None)
        if first_valid_ts is not None:
            last_off = 0.0
            for ts in raw_ts:
                if ts is not None:
                    off = max(0.0, ts - first_valid_ts)
                    offsets.append(off)
                    last_off = off
                else:
                    last_off += 1.0
                    offsets.append(last_off)
        else:
            offsets = [float(i) for i in range(len(self.batches))]
        self.batch_offsets = offsets
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
                return [(ts, recs)] if recs else []
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
    def start(self) -> None:
        if not self.batches:
            self.load()
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._run, name="adsb-replay", daemon=True)
        self._thread.start()
        log.info("回放已启动: %s 批次=%d 速度=%.1fx loop=%s", self.dataset.name, len(self.batches), self.speed, self.loop)

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("回放已停止")

    def pause(self) -> None:
        with self._cond:
            self._paused = True

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def set_speed(self, v: float) -> None:
        if not math.isfinite(v) or v <= 0:
            return
        with self._cond:
            self.speed = max(0.1, min(60.0, v))

    def seek(self, idx: int) -> None:
        total = len(self.batches)
        if total == 0:
            return
        target = max(0, min(total - 1, idx))
        with self._cond:
            self._seek_version += 1
            self._batch_index = target
            # 无论向前或向后跳转，重置状态库并重放状态以精确复原活动航空器与完整轨迹
            if self.on_reset:
                try:
                    self.on_reset()
                    if self.on_batch:
                        # 重建目标点前最多 TRACK_MAX_POINTS 批次，保证航迹完整与视野准确
                        start_j = max(0, target - config.TRACK_MAX_POINTS)
                        for j in range(start_j, target + 1):
                            now_j = self._t0 + self.batch_offsets[j]
                            self.on_batch(self.batches[j], now_j)
                except Exception:  # noqa: BLE001
                    log.exception("Seek 状态重置异常")
            elif self._paused and self.on_batch:
                try:
                    now_target = self._t0 + self.batch_offsets[target]
                    self.on_batch(self.batches[target], now_target)
                except Exception:  # noqa: BLE001
                    pass
            self._cond.notify_all()

    def current_time(self) -> float:
        """回放时钟：随批次推进。"""
        if not self.batches or not self.batch_offsets:
            return self._t0
        idx = max(0, min(len(self.batch_offsets) - 1, self._batch_index))
        return self._t0 + self.batch_offsets[idx]

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.batches:
                self._stop.wait(1.0)
                continue
            with self._cond:
                while self._paused and not self._stop.is_set():
                    self._cond.wait(timeout=0.5)
                if self._stop.is_set():
                    break
                cur_v = self._seek_version
                i = self._batch_index
                recs = self.batches[i]
                now = self._t0 + self.batch_offsets[i]

            try:
                if self.on_batch:
                    self.on_batch(recs, now)
            except Exception:  # noqa: BLE001
                log.exception("回放回调异常")

            # 计算下一批的等待间隔
            nxt_idx = i + 1
            if nxt_idx < len(self.batch_offsets):
                step_s = max(0.1, min(30.0, self.batch_offsets[nxt_idx] - self.batch_offsets[i]))
            else:
                step_s = 1.0
            wait_s = step_s / self.speed

            with self._cond:
                self._cond.wait(timeout=wait_s)
                if self._stop.is_set():
                    break
                if self._seek_version != cur_v:
                    # 在等待或回调期间发生 seek，跳过旧索引推进
                    continue
                if self._paused:
                    continue

                nxt = self._batch_index + 1
                if nxt >= len(self.batches):
                    if self.loop:
                        nxt = 0
                        self._t0 = time.time()
                        if self.on_reset:
                            try:
                                self.on_reset()
                            except Exception:  # noqa: BLE001
                                pass
                    else:
                        self._paused = True
                        continue
                self._batch_index = nxt

    # ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "kind": "replay",
            "dataset": self.dataset.name,
            "dataset_root": str(self.dataset.root),
            "files": [f.name for f in self.dataset.files],
            "total_batches": len(self.batches),
            "batch_index": self._batch_index,
            "paused": self._paused,
            "speed": self.speed,
            "loop": self.loop,
        }
