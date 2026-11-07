"""异步状态轮询器。

每两秒异步请求一次 /api/meta，获取模式、采集状态及预热进度；
不订阅 SSE，不复制全量飞机数据流，完全不阻塞 Qt 主线程。
"""
from __future__ import annotations

import json
import logging
import threading
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, QTimer, Signal

from .constants import STATUS_POLL_INTERVAL_MS

log = logging.getLogger("adsb.desktop.poller")


class AsyncStatusPoller(QObject):
    status_updated = Signal(dict)
    status_error = Signal(str)

    _worker_success = Signal(dict)
    _worker_error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url: str | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(STATUS_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_once)
        self._is_polling = False
        self._busy = False

        self._worker_success.connect(self._on_success)
        self._worker_error.connect(self._on_error)

    def start(self, base_url: str) -> None:
        self._url = f"{base_url.rstrip('/')}/api/meta"
        self._is_polling = True
        self._timer.start()
        # 立即先查一次
        self._poll_once()

    def stop(self) -> None:
        self._is_polling = False
        self._timer.stop()

    def _poll_once(self) -> None:
        if not self._is_polling or not self._url or self._busy:
            return
        self._busy = True

        def _worker():
            try:
                req = Request(self._url, headers={"User-Agent": "adsb-desktop-poller"})
                with urlopen(req, timeout=1.8) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        self._worker_success.emit(data)
                        return
                    else:
                        msg = f"HTTP {resp.status}"
                        self._worker_error.emit(msg)
            except (URLError, OSError, TimeoutError) as exc:
                self._worker_error.emit(str(exc))
            except Exception as exc:
                self._worker_error.emit(str(exc))

        threading.Thread(target=_worker, name="adsb-status-worker", daemon=True).start()

    def _on_success(self, data: dict) -> None:
        self._busy = False
        if not self._is_polling:
            return
        self.status_updated.emit(data)

    def _on_error(self, err: str) -> None:
        self._busy = False
        if not self._is_polling:
            return
        self.status_error.emit(err)
