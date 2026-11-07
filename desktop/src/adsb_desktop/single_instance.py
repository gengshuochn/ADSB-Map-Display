"""单实例管理与唤起机制。

基于 QLockFile 与 QLocalServer 实现：
- 同一用户、同一项目目录只允许一个桌面实例；
- 实例标识由规范化项目路径与当前 UID 生成；
- 重复启动时向主实例发送 ACTIVATE 消息并退出当前进程。
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .constants import DESKTOP_RUNTIME_DIR, PROJECT_ROOT

log = logging.getLogger("adsb.desktop.instance")


class SingleInstanceManager(QObject):
    activate_requested = Signal()

    def __init__(self, parent: QObject | None = None, custom_id: str | None = None) -> None:
        super().__init__(parent)
        DESKTOP_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

        if custom_id:
            self._key = custom_id
        else:
            canonical_path = os.path.realpath(str(PROJECT_ROOT))
            uid = os.getuid()
            raw = f"{uid}:{canonical_path}".encode("utf-8")
            self._key = hashlib.sha256(raw).hexdigest()[:16]

        self._lock_path = DESKTOP_RUNTIME_DIR / f"instance_{self._key}.lock"
        self._server_name = f"adsb_desktop_{self._key}"
        self._lock_file = QLockFile(str(self._lock_path))
        self._lock_file.setStaleLockTime(5000)
        self._server: QLocalServer | None = None
        self._is_primary = False

    @property
    def is_primary(self) -> bool:
        return self._is_primary

    def try_acquire(self) -> bool:
        """尝试获取单实例锁。如果成功返回 True；若已存在实例，通知已有实例并返回 False。"""
        if self._lock_file.tryLock(100):
            self._is_primary = True
            self._start_server()
            log.info("获得单实例锁: %s (server: %s)", self._lock_path, self._server_name)
            return True

        # 锁被占用，尝试向既有实例发送唤起请求
        log.info("检测到已有桌面实例运行，尝试唤起对应窗口…")
        notified = self._notify_existing_instance()
        if notified:
            log.info("已成功向主实例发送唤起请求。")
            return False

        # 如果未响应本地套接字，检查持有锁的 PID 是否还存活
        lock_pid, hostname, appname = self._lock_file.getLockInfo()
        is_alive = False
        if lock_pid:
            try:
                os.kill(lock_pid, 0)
                is_alive = True
            except (ProcessLookupError, PermissionError):
                is_alive = False

        if not is_alive:
            log.warning("持有锁的进程 (PID=%s) 已不存在，清理僵死锁文件并重新获取…", lock_pid)
            self._lock_file.removeStaleLockFile()
            QLocalServer.removeServer(self._server_name)
            if self._lock_file.tryLock(100):
                self._is_primary = True
                self._start_server()
                log.info("成功接管并获得单实例锁: %s (server: %s)", self._lock_path, self._server_name)
                return True

        log.warning("锁仍被活跃进程 (PID=%s) 持有，但未响应套接字。", lock_pid)
        return False

    def _start_server(self) -> None:
        # 清理可能残留的同名套接字
        QLocalServer.removeServer(self._server_name)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        if not self._server.listen(self._server_name):
            log.error("无法启动本地唤起服务: %s", self._server.errorString())
        else:
            log.debug("本地唤起服务已监听: %s", self._server_name)

    def _on_new_connection(self) -> None:
        if not self._server:
            return
        socket = self._server.nextPendingConnection()
        if not socket:
            return

        def _read():
            data = socket.readAll().data().decode("utf-8", errors="ignore")
            if "ACTIVATE" in data:
                log.info("收到唤起请求，触发窗口激活信号")
                self.activate_requested.emit()
                socket.write(b"OK\n")
                socket.flush()
            socket.disconnectFromServer()

        socket.readyRead.connect(_read)

    def _notify_existing_instance(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self._server_name)
        if not socket.waitForConnected(1500):
            log.warning("连接主实例套接字失败: %s", socket.errorString())
            return False
        socket.write(b"ACTIVATE\n")
        if not socket.waitForBytesWritten(1000):
            socket.abort()
            return False
        if socket.waitForReadyRead(1500):
            resp = socket.readAll().data().decode("utf-8", errors="ignore")
            log.debug("主实例应答: %s", resp.strip())
        socket.disconnectFromServer()
        return True

    def release(self) -> None:
        if self._server:
            self._server.close()
            QLocalServer.removeServer(self._server_name)
            self._server = None
        if self._is_primary:
            self._lock_file.unlock()
            self._is_primary = False
            log.info("已释放单实例锁: %s", self._lock_path)
