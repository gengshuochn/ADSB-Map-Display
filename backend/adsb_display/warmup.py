"""预热阶段状态机：以真实动作完成情况驱动（非定时模拟），经 SSE 下发前端展示进度。

阶段: checking_hardware → initializing → collecting → ready
- checking_hardware: lsusb 检查 RTL-SDR（不独占设备，不用 rtl_test）
- initializing:      创建数据集、拉起 readsb 并确认存活/输出
- collecting:        报文持续接收；就绪判据 = 首个带位置航空器解算完成
- ready:             进入正常实时显示

允许用户手动跳过（skip）直接进入显示；出错时记录 error 并等待用户决策。
"""
from __future__ import annotations

import threading
import time

PHASES = ("checking_hardware", "initializing", "collecting", "ready")


class Warmup:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "checking_hardware"
        self.detail = ""
        self.error: str | None = None
        self.skipped = False
        self._phase_since = time.time()
        self._steps: list[dict] = []  # 已完成阶段: {phase, seconds, detail}
        self._completed: set[str] = set()

    # ------------------------------------------------------------------
    def set(self, phase: str, detail: str = "") -> None:
        """进入下一阶段；重复调用仅刷新 detail（如报文计数变化）。"""
        with self._lock:
            now = time.time()
            if phase == self.phase:
                if detail:
                    self.detail = detail
                return
            self._steps.append({
                "phase": self.phase,
                "seconds": round(now - self._phase_since, 2),
                "detail": self.detail,
            })
            self._completed.add(self.phase)
            self.phase = phase
            self.detail = detail
            self.error = None  # 推进到新阶段时清除暂态错误
            self._phase_since = now

    def set_error(self, error: str) -> None:
        with self._lock:
            self.error = error

    def skip(self) -> bool:
        """手动跳过当前阶段直接进入显示。返回是否发生了跳转。"""
        with self._lock:
            if self.phase == "ready":
                return False
            now = time.time()
            self._steps.append({
                "phase": self.phase,
                "seconds": round(now - self._phase_since, 2),
                "detail": ((self.detail + " ") if self.detail else "") + "（手动跳过）",
            })
            self._completed.add(self.phase)
            self.skipped = True
            self.error = None
            self.phase = "ready"
            self.detail = "已手动跳过预热，直接进入显示"
            self._phase_since = now
            return True

    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "phase": self.phase,
                "detail": self.detail,
                "error": self.error,
                "skipped": self.skipped,
                "steps": list(self._steps),
                "phase_elapsed": round(time.time() - self._phase_since, 1),
            }
