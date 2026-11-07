"""桌面应用协调器。

负责连接单实例管理、后端进程、托盘图标、主窗口与状态轮询器。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

from .backend_process import BackendProcessManager
from .constants import (
    DEFAULT_HTTP_PORT,
    SHUTDOWN_GRACE_SECONDS,
    SHUTDOWN_KILL_SECONDS,
    check_rtl_hardware,
    get_available_datasets,
    validate_dataset_path,
)
from .single_instance import SingleInstanceManager
from .status_poller import AsyncStatusPoller
from .tray import SystemTrayManager
from .window import MainWindow

log = logging.getLogger("adsb.desktop.app")


class DesktopApp(QObject):
    def __init__(self, qapp: QApplication, launch_params: dict[str, Any]) -> None:
        super().__init__()
        self._qapp = qapp
        self._params = dict(launch_params)
        self._is_quitting = False
        self._is_restarting = False
        self._restart_pending = False
        self._quit_finished = False
        self._restart_safety_timer: QTimer | None = None
        self._quit_safety_timer: QTimer | None = None

        self._instance_mgr = SingleInstanceManager(self)
        self._backend = BackendProcessManager(self)
        self._poller = AsyncStatusPoller(self)
        self._tray: SystemTrayManager | None = None
        self._window: MainWindow | None = None

    def run(self) -> int:
        # 1. 尝试获取单实例锁
        if not self._instance_mgr.try_acquire():
            log.info("已有实例在运行，已唤起对应窗口，当前进程正常退出。")
            return 0

        # 2. 初始化托盘与主窗口
        self._tray = SystemTrayManager(self)
        self._window = MainWindow(tray=self._tray)

        # 3. 关联托盘与主窗口交互及各组件生命周期
        self._setup_connections()

        # 4. 显示主窗口并启动后端
        self._window.show()
        self._start_backend()

        return self._qapp.exec()

    def _setup_connections(self) -> None:
        """关联托盘、窗口与后端、轮询器交互信号。"""
        if self._tray:
            self._tray.toggle_window_requested.connect(self._on_toggle_window)
            self._tray.restart_requested.connect(self.restart_backend)
            self._tray.quit_requested.connect(self.quit_gracefully)

        if self._window:
            self._window.quit_requested.connect(self.quit_gracefully)
            self._window.retry_requested.connect(self.restart_backend)
            self._window.dataset_chosen.connect(self._on_dataset_chosen)

        # 关联单实例外部唤起
        if self._window:
            self._instance_mgr.activate_requested.connect(self._window.activate_window)

        # 关联后端生命周期信号
        self._backend.ready.connect(self._on_backend_ready)
        self._backend.startup_failed.connect(self._on_backend_startup_failed)
        self._backend.crashed.connect(self._on_backend_crashed)
        self._backend.shutdown_completed.connect(self._on_backend_shutdown_completed)

        # 关联状态轮询器
        if self._tray:
            self._poller.status_updated.connect(self._tray.update_from_backend_meta)
            self._poller.status_error.connect(lambda err: self._tray.set_state("error", f"后端状态异常: {err}"))

    def _is_no_source_reason(self, error_message: str) -> bool:
        """检查错误信息是否表明当前无可用播放源（属于正常待机而非真实崩溃故障）。"""
        low = error_message.lower()
        no_source_indicators = [
            "没有可回放数据集",
            "未找到可回放数据集",
            "请先运行 live 模式采集一次",
            "暂无可回放数据",
            "no replay dataset",
        ]
        return any(k.lower() in low for k in no_source_indicators)

    def _start_backend(self) -> None:
        if self._is_quitting:
            log.info("应用正在退出中，放弃启动后端。")
            return
        if self._backend.is_running():
            log.warning("后端主进程或所属进程组仍在运行，等待清理完成后再启动后端。")
            return
        requested_mode = self._params.get("mode", "auto")
        dataset = self._params.get("dataset")
        effective_mode = requested_mode

        # 自动判定物理硬件与数据源（不覆盖 self._params["mode"] 的 auto 设定）
        if requested_mode == "auto":
            has_rtl = check_rtl_hardware()
            if has_rtl:
                effective_mode = "live"
                log.info("自动检测到 RTL-SDR 设备，选择实时采集模式")
            else:
                effective_mode = "replay"
                log.info("未检测到 RTL-SDR 设备，尝试选择回放模式")

        # 若为回放模式且未显式指定数据集，先扫描 data/ 下是否存在可用数据集
        if effective_mode == "replay" and not dataset:
            cands = get_available_datasets()
            if not cands:
                log.info("无活动播放源（未连接 RTL-SDR 且暂无可回放数据集），应用进入待机状态。")
                self._poller.stop()
                if self._tray:
                    self._tray.set_state("idle", "待机／未播放")
                if self._window:
                    self._window.show_idle(
                        "系统待机／未播放",
                        "当前未检测到活动播放源：未连接 RTL-SDR 硬件设备，且 data/ 目录下暂无可回放数据集。\n\n"
                        "您可插入 RTL-SDR 接收机开启实时采集，或将航迹数据集放入数据目录进行回放。"
                    )
                return

        if self._window:
            self._window.show_loading("正在启动后端服务，建立数据通信…")
        if self._tray:
            self._tray.set_state("starting", "正在启动…")

        ok = self._backend.start(
            mode=effective_mode,
            port=self._params.get("port", DEFAULT_HTTP_PORT),
            dataset=dataset,
            duration=self._params.get("duration", 0),
            lat=self._params.get("lat"),
            lon=self._params.get("lon"),
            gain=self._params.get("gain"),
        )
        if not ok:
            log.warning("启动后端命令未发出，等待错误回调处理。")

    def restart_backend(self) -> None:
        if self._is_quitting:
            log.info("应用正在退出中，忽略重启后端请求。")
            return
        if self._is_restarting:
            log.info("后端正在重启中，忽略重复的重启请求。")
            return

        log.info("用户请求重新检测播放源并启动后端服务…")
        self._poller.stop()

        if self._backend.is_running():
            self._is_restarting = True
            self._restart_pending = True
            if self._window:
                self._window.show_loading("正在停止当前后端服务并准备重新启动…")
            if self._tray:
                self._tray.set_state("starting", "正在重启…")

            # 启动重启安全兜底定时器（必须长于 BackendProcessManager 的完整优雅退出与强杀升级周期）
            if self._restart_safety_timer is None:
                self._restart_safety_timer = QTimer(self)
                self._restart_safety_timer.setSingleShot(True)
                self._restart_safety_timer.timeout.connect(self._on_restart_timeout)
            wait_seconds = (
                getattr(self._backend, "shutdown_grace_seconds", SHUTDOWN_GRACE_SECONDS)
                + getattr(self._backend, "shutdown_kill_seconds", SHUTDOWN_KILL_SECONDS)
                + 3
            )
            self._restart_safety_timer.start(int(wait_seconds * 1000))

            self._backend.request_shutdown()
        else:
            self._is_restarting = False
            self._restart_pending = False
            self._start_backend()

    def _on_restart_timeout(self) -> None:
        """旧后端在重启时关闭超时后的安全兜底：强制清理并确保完全消亡后再启动新后端。"""
        if not self._is_restarting or self._is_quitting:
            return
        log.warning("旧后端关闭等待超时，执行强制兜底清理（SIGKILL）…")
        # 绝不直接启动新后端，必须先强制终止旧进程组并等待消亡
        cleaned = self._backend.force_kill_and_cleanup(synchronous=True)
        if not cleaned:
            log.warning("强制清理后旧后端仍未完全消亡，保持重启等待状态，绝不提前拉起新后端；稍后重试检查…")
            if self._restart_safety_timer is not None:
                self._restart_safety_timer.start(500)
            return

        if self._restart_pending and not self._is_quitting:
            log.info("旧后端强制清理已确认完成，开始启动新后端会话…")
            self._is_restarting = False
            self._restart_pending = False
            self._start_backend()

    def _on_dataset_chosen(self, dataset_path: str) -> None:
        log.info("用户选择数据集: %s，校验有效性…", dataset_path)
        ok, reason = validate_dataset_path(dataset_path)
        if not ok:
            log.error("用户选择的数据集无效: %s", reason)
            if self._tray:
                self._tray.set_state("idle", "数据集无效")
            if self._window:
                self._window.show_idle(
                    "数据集无效",
                    f"所选目录无效：\n{reason}\n\n请重新选择包含有效航迹数据（logs/ 或 live/）的目录。"
                )
            return

        resolved_path = str(Path(dataset_path).resolve())
        log.info("数据集校验通过: %s，切换至回放模式重启…", resolved_path)
        self._params["mode"] = "replay"
        self._params["dataset"] = resolved_path
        self.restart_backend()

    def _on_backend_ready(self, url: str, ready_info: dict) -> None:
        log.info("后端服务就绪: %s", url)
        if self._window:
            self._window.load_url(url)
        if self._tray:
            self._tray.update_from_backend_meta(ready_info)
        self._poller.start(url)

    def _on_backend_startup_failed(self, error_message: str) -> None:
        self._poller.stop()

        # 区分正常无源与真实故障
        if self._is_no_source_reason(error_message):
            log.info("后端反馈无可用播放源，进入待机模式: %s", error_message)
            if self._tray:
                self._tray.set_state("idle", "待机／未播放")
            if self._window:
                self._window.show_idle(
                    "系统待机／未播放",
                    f"当前未检测到活动播放源：\n{error_message.strip()}"
                )
            return

        # 真实故障（端口冲突、进程异常崩溃、权限错误等）
        log.error("后端启动失败 (真实故障): %s", error_message)
        if self._tray:
            self._tray.set_state("error", "启动失败")
        if self._window:
            self._window.show_error("后端服务启动失败", error_message)

    def _on_backend_crashed(self, exit_code: int, error_message: str) -> None:
        log.error("后端服务意外退出 (%d): %s", exit_code, error_message)
        self._poller.stop()
        self._tray.set_state("error", f"服务终止 ({exit_code})")
        self._window.show_error("后端服务异常退出", error_message)

    def _on_toggle_window(self) -> None:
        if not self._window:
            return
        if self._window.isVisible() and not self._window.isMinimized():
            self._window.hide()
        else:
            self._window.activate_window()

    def quit_gracefully(self) -> None:
        """优雅关闭整个桌面程序。"""
        if self._is_quitting:
            return
        self._is_quitting = True
        log.info("正在执行桌面程序优雅关闭流程…")

        # 取消可能正在进行的重启流程
        self._restart_pending = False
        self._is_restarting = False
        if self._restart_safety_timer and self._restart_safety_timer.isActive():
            self._restart_safety_timer.stop()

        self._poller.stop()
        if self._tray:
            self._tray.set_state("exiting", "正在退出…")
        if self._window:
            self._window.show_loading("正在终止采集会话并保存数据，请稍候…")

        # 如果后端当前未运行，直接收尾退出
        if not self._backend.is_running():
            log.info("后端未在运行，直接执行退出收尾。")
            self._finish_quit()
            return

        # 启动退出安全兜底定时器（必须长于 BackendProcessManager 的完整退出升级周期）
        if self._quit_safety_timer is None:
            self._quit_safety_timer = QTimer(self)
            self._quit_safety_timer.setSingleShot(True)
            self._quit_safety_timer.timeout.connect(self._on_quit_timeout)
        wait_seconds = (
            getattr(self._backend, "shutdown_grace_seconds", SHUTDOWN_GRACE_SECONDS)
            + getattr(self._backend, "shutdown_kill_seconds", SHUTDOWN_KILL_SECONDS)
            + 3
        )
        self._quit_safety_timer.start(int(wait_seconds * 1000))

        self._backend.request_shutdown()

    def _on_quit_timeout(self) -> None:
        """退出等待超时兜底：推动后端升级强制清理，确保子进程完全消亡后再退出 Qt。"""
        log.warning("优雅退出等待超时，推动后端升级强制清理（SIGKILL）…")
        cleaned = self._backend.force_kill_and_cleanup(synchronous=True)
        if not cleaned:
            log.warning("强制清理后后端进程组仍未完全消亡，保持退出等待状态，绝不提前终止 Qt 事件循环；稍后重试检查…")
            if self._quit_safety_timer is not None:
                self._quit_safety_timer.start(500)
            return

        log.info("后端及进程组已彻底消亡，执行桌面退出收尾。")
        self._finish_quit()

    def _on_backend_shutdown_completed(self) -> None:
        if self._restart_safety_timer and self._restart_safety_timer.isActive():
            self._restart_safety_timer.stop()
        if self._quit_safety_timer and self._quit_safety_timer.isActive():
            self._quit_safety_timer.stop()

        # 1. 用户主动退出流程
        if self._is_quitting:
            log.info("后端已完成清理并退出，继续执行桌面退出收尾。")
            self._finish_quit()
            return

        # 2. 重启流程：旧后端已完全退出，现在启动新会话
        if self._restart_pending:
            log.info("旧后端已完全关闭，开始启动新后端会话…")
            self._is_restarting = False
            self._restart_pending = False
            self._start_backend()
            return

        # 3. 后端自发正常结束（如 duration 倒计时结束、页面请求终止等）
        log.info("后端子进程自发正常结束，触发桌面应用统一退出。")
        self.quit_gracefully()

    def _finish_quit(self) -> None:
        """统一收尾并退出 QApplication（幂等）。"""
        if getattr(self, "_quit_finished", False):
            return
        if self._backend.is_running():
            log.warning("后端主进程或所属进程组仍存活，暂不终止 Qt 事件循环，启动等待重试…")
            if self._quit_safety_timer is not None:
                self._quit_safety_timer.start(500)
            return
        self._quit_finished = True

        if self._quit_safety_timer and self._quit_safety_timer.isActive():
            self._quit_safety_timer.stop()
        if self._restart_safety_timer and self._restart_safety_timer.isActive():
            self._restart_safety_timer.stop()

        self._poller.stop()
        if self._tray:
            self._tray.hide()
        if self._window:
            self._window.close()
        self._instance_mgr.release()
        self._qapp.quit()

