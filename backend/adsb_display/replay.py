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
import threading
import time
from pathlib import Path

from .dataset import ReplayDataset

log = logging.getLogger("adsb.replay")


class ReplaySource:
    def __init__(self, dataset: ReplayDataset, speed: float = 1.0, loop: bool = True):
        self.dataset = dataset
        self.speed = max(0.1, speed)
        self.loop = loop
        self.batches: list[list[dict]] = []
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
        """解析全部回放文件为批次列表，返回批次数。"""
        self.batches = []
        for f in self.dataset.files:
            self.batches.extend(self._parse_file(f))
        if not self.batches:
            log.warning("数据集 %s 未解析出任何批次", self.dataset.name)
        return len(self.batches)

    @staticmethod
    def _parse_file(path: Path) -> list[list[dict]]:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return []
        # 1. 尝试直接作为整体 JSON 对象解析（如 readsb 的 history_*.json）
        try:
            doc = json.loads(text)
            if isinstance(doc, dict) and "aircraft" in doc:
                recs = [a for a in doc["aircraft"] if isinstance(a, dict)]
                return [recs] if recs else []
        except json.JSONDecodeError:
            pass

        # 2. 逐行解析：支持批次化 jsonl（{"now":..., "aircraft":[...]}）或平铺格式（每行一个 aircraft）
        batches: list[list[dict]] = []
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
            if isinstance(obj, dict) and "aircraft" in obj and "now" in obj:
                # 批次化格式
                if flat_cur:
                    batches.append(flat_cur)
                    flat_cur, flat_hexes = [], set()
                recs = [a for a in obj["aircraft"] if isinstance(a, dict)]
                batches.append(recs)
            elif isinstance(obj, dict) and obj.get("hex"):
                # 平铺格式
                hx = obj["hex"].lower()
                if hx in flat_hexes:
                    batches.append(flat_cur)
                    flat_cur, flat_hexes = [], set()
                flat_hexes.add(hx)
                flat_cur.append(obj)
        if flat_cur:
            batches.append(flat_cur)
        return batches

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
        with self._cond:
            self.speed = max(0.1, min(60.0, v))

    def seek(self, idx: int) -> None:
        total = len(self.batches)
        if total == 0:
            return
        target = max(0, min(total - 1, idx))
        with self._cond:
            prev = self._batch_index
            self._batch_index = target
            # 若向后跳转（Seek Backward）或跳转幅度较大，重置状态库并重放累积状态
            if target < prev and self.on_reset:
                try:
                    self.on_reset()
                    if self.on_batch:
                        # 快速重放目标点前最近 75 秒（AIRCRAFT_DROP_S）内的批次，确保目标时刻视野准确
                        start_j = max(0, target - 75)
                        for j in range(start_j, target + 1):
                            self.on_batch(self.batches[j], self._t0 + j)
                except Exception:  # noqa: BLE001
                    log.exception("Seek 状态重置异常")
            elif self._paused and self.on_batch:
                try:
                    self.on_batch(self.batches[target], self._t0 + target)
                except Exception:  # noqa: BLE001
                    pass

    def current_time(self) -> float:
        """回放时钟：随批次推进（1 批 = 1 秒回放时间）。"""
        return self._t0 + self._batch_index

    def _run(self) -> None:
        interval_base = 1.0
        while not self._stop.is_set():
            if not self.batches:
                self._stop.wait(1.0)
                continue
            with self._cond:
                if self._paused:
                    self._cond.wait(timeout=1.0)
                    continue
            i = self._batch_index
            recs = self.batches[i]
            now = self._t0 + i
            try:
                if self.on_batch:
                    self.on_batch(recs, now)
            except Exception:  # noqa: BLE001
                log.exception("回放回调异常")
            # 节拍
            wait_s = interval_base / self.speed
            end_wait = self._stop.wait(wait_s)
            if end_wait:
                break
            nxt = i + 1
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
                    self.pause()
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
