"""系统托盘管理。

提供系统托盘图标、上下文菜单及状态感知指示：
- 黄色：正在启动、预热
- 绿色：实时采集正常
- 蓝色：当前显示回放（后台采集时注明“后台采集中”）
- 红色：硬件、采集或后端异常（附带简短原因）
- 灰色：正在退出
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import QMenu, QMessageBox, QSystemTrayIcon, QWidget

from .constants import (
    COLOR_ERROR,
    COLOR_EXITING,
    COLOR_IDLE,
    COLOR_LIVE_OK,
    COLOR_REPLAY,
    COLOR_STARTING,
    DATA_DIR,
    DESKTOP_LOGS_DIR,
)

log = logging.getLogger("adsb.desktop.tray")


def make_status_icon(color_hex: str, is_active: bool = True) -> QIcon:
    """动态绘制托盘状态图标：彩色圆形指示环与飞机轮廓（适配 GNOME 顶栏多尺寸与高分屏）。"""
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 绘制背景圆盘
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color_hex))
        margin = 1.0 if size <= 24 else 2.0
        p.drawEllipse(margin, margin, size - margin * 2.0, size - margin * 2.0)

        # 绘制内部白色飞机剪影
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FFFFFF"))

        scale = (size - margin * 2.0) / 44.0
        cx, cy = size / 2.0, size / 2.0

        path = QPainterPath()
        path.moveTo(cx, cy - 14 * scale)       # 机头
        path.lineTo(cx + 3 * scale, cy - 6 * scale)    # 机身右上方
        path.lineTo(cx + 14 * scale, cy + 1 * scale)   # 右机翼外尖
        path.lineTo(cx + 14 * scale, cy + 4 * scale)   # 右机翼底
        path.lineTo(cx + 3 * scale, cy + 2 * scale)    # 右机翼根部
        path.lineTo(cx + 3 * scale, cy + 9 * scale)    # 右尾翼前
        path.lineTo(cx + 7 * scale, cy + 13 * scale)   # 右尾翼尖
        path.lineTo(cx + 7 * scale, cy + 14 * scale)   # 右尾翼底
        path.lineTo(cx, cy + 12 * scale)               # 尾椎
        path.lineTo(cx - 7 * scale, cy + 14 * scale)   # 左尾翼底
        path.lineTo(cx - 7 * scale, cy + 13 * scale)   # 左尾翼尖
        path.lineTo(cx - 3 * scale, cy + 9 * scale)    # 左尾翼前
        path.lineTo(cx - 3 * scale, cy + 2 * scale)    # 左机翼根部
        path.lineTo(cx - 14 * scale, cy + 4 * scale)   # 左机翼底
        path.lineTo(cx - 14 * scale, cy + 1 * scale)   # 左机翼外尖
        path.lineTo(cx - 3 * scale, cy - 6 * scale)    # 机身左上方
        path.closeSubpath()
        p.drawPath(path)

        p.end()
        icon.addPixmap(pixmap)
    return icon


class SystemTrayManager(QObject):
    toggle_window_requested = Signal()
    restart_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._available = QSystemTrayIcon.isSystemTrayAvailable()
        self._tray_icon: QSystemTrayIcon | None = None
        self._current_state = "starting"
        self._status_text = "正在启动…"
        self._is_live_mode = False

        self._icons = {
            "starting": make_status_icon(COLOR_STARTING),
            "live_normal": make_status_icon(COLOR_LIVE_OK),
            "replay": make_status_icon(COLOR_REPLAY),
            "idle": make_status_icon(COLOR_IDLE),
            "error": make_status_icon(COLOR_ERROR),
            "exiting": make_status_icon(COLOR_EXITING),
        }

        if self._available:
            self._tray_icon = QSystemTrayIcon(self)
            self._tray_icon.setIcon(self._icons["starting"])
            self._tray_icon.setToolTip("ADS-B 地图 (正在启动…)")
            self._setup_menu()
            self._tray_icon.activated.connect(self._on_tray_activated)
            self._tray_icon.show()
            log.info("系统托盘图标已初始化并显示。")
        else:
            log.warning("当前环境系统托盘不可用 (isSystemTrayAvailable=False)。")

    @property
    def is_available(self) -> bool:
        return self._available and self._tray_icon is not None

    @property
    def current_state(self) -> str:
        return self._current_state

    @property
    def status_text(self) -> str:
        return self._status_text

    def _setup_menu(self) -> None:
        if not self._tray_icon:
            return
        menu = QMenu()

        # 显示 / 隐藏窗口
        self._act_toggle = QAction("显示／隐藏地图窗口", menu)
        self._act_toggle.triggered.connect(self.toggle_window_requested.emit)
        menu.addAction(self._act_toggle)

        # 状态只读项
        self._act_status = QAction(f"状态: {self._status_text}", menu)
        self._act_status.setEnabled(False)
        menu.addAction(self._act_status)

        # 重新检测 / 启动服务
        self._act_restart = QAction("重新检测／启动服务", menu)
        self._act_restart.triggered.connect(self.restart_requested.emit)
        menu.addAction(self._act_restart)

        menu.addSeparator()

        # 打开数据目录
        act_data = QAction("打开数据目录", menu)
        act_data.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR))))
        menu.addAction(act_data)

        # 打开日志目录
        act_logs = QAction("打开运行日志目录", menu)
        act_logs.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(DESKTOP_LOGS_DIR))))
        menu.addAction(act_logs)

        menu.addSeparator()

        # 退出应用
        act_quit = QAction("退出应用", menu)
        act_quit.triggered.connect(self._on_quit_triggered)
        menu.addAction(act_quit)

        self._tray_icon.setContextMenu(menu)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.toggle_window_requested.emit()

    def _on_quit_triggered(self) -> None:
        if self._is_live_mode:
            reply = QMessageBox.question(
                None,
                "退出确认",
                "当前正在进行实时采集，退出应用将终止采集会话并保存数据。\n确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.set_state("exiting", "正在退出…")
        self.quit_requested.emit()

    def set_state(self, state: str, text: str) -> None:
        self._current_state = state
        self._status_text = text
        if not self._tray_icon:
            return

        icon = self._icons.get(state, self._icons["starting"])
        self._tray_icon.setIcon(icon)
        self._tray_icon.setToolTip(f"ADS-B 地图 ({text})")
        if hasattr(self, "_act_status") and self._act_status:
            self._act_status.setText(f"状态: {text}")

    def set_display_mode(self, display_mode: str) -> None:
        """当网页端发生模式切换时即时响应配色与菜单，不阻塞也不等待轮询。"""
        if self._current_state in ("error", "starting", "exiting"):
            return
        if display_mode == "live":
            self.set_state("live_normal", "实时采集正常")
        elif display_mode == "replay":
            if self._is_live_mode:
                self.set_state("replay", "当前显示回放 (后台采集中)")
            else:
                self.set_state("replay", "当前显示回放")
        elif display_mode in ("idle", "standby", "none", "stopped", "waiting"):
            self.set_state("idle", "待机／未播放")
        else:
            self.set_state("idle", "待机／未播放")

    def update_from_backend_meta(self, data: dict) -> None:
        display_mode = data.get("display_mode", data.get("mode", "unknown"))
        live_available = bool(data.get("live_available", False))
        warmup = data.get("warmup")
        col_source = data.get("collecting_source")
        self._is_live_mode = (display_mode == "live" or live_available)

        # 1. 检查硬件 / 预热初始化阶段的显式致命错误
        if warmup and isinstance(warmup, dict):
            err = warmup.get("error")
            if err:
                self.set_state("error", f"硬件异常: {err}")
                return

        # 2. 采集器显式故障检查（优先级必须高于预热中状态，避免 readsb 异常退出被预热 phase 掩盖）
        if col_source and isinstance(col_source, dict):
            if col_source.get("last_error"):
                prefix = "采集异常" if display_mode == "live" else "后台采集异常"
                self.set_state("error", f"{prefix}: {col_source['last_error']}")
                return
            if not col_source.get("running"):
                err = "采集进程已退出" if display_mode == "live" else "后台采集已停止"
                prefix = "采集异常" if display_mode == "live" else "后台采集异常"
                self.set_state("error", f"{prefix}: {err}")
                return
        elif display_mode == "replay" and live_available:
            self.set_state("error", "后台采集已停止")
            return

        # 3. 预热进行中状态（采集器正常或尚未初始化时，向用户反馈预热阶段）
        if warmup and isinstance(warmup, dict):
            phase = warmup.get("phase", "")
            if phase in ("checking_hardware", "initializing", "collecting"):
                detail = warmup.get("detail", "")
                self.set_state("starting", f"预热中: {detail or phase}")
                return

        # 4. 判断是否有活跃的实时源或回放源（0 架飞机、仅暂停显示不等于待机）
        has_active_live = live_available or bool(
            col_source and isinstance(col_source, dict) and col_source.get("running", False)
        )

        # 判断回放源是否处于活跃回放状态还是尚未选择数据集/未启动播放源
        has_active_replay = True
        if display_mode == "replay":
            if "dataset" in data and not data.get("dataset") and not data.get("datasets"):
                has_active_replay = False
            elif data.get("has_source") is False or (
                "source" in data and data.get("source") is None and "dataset" in data and not data.get("dataset")
            ):
                has_active_replay = False

        # 5. 待机与播放状态分流
        # 5.1 显式待机 / 未启动播放源 / 正常等待操作
        if display_mode in ("idle", "standby", "none", "stopped", "waiting"):
            if has_active_live:
                self.set_state("replay", "待机 (后台采集中)")
            else:
                self.set_state("idle", "待机／未播放")
            return

        # 5.2 实时模式
        if display_mode == "live":
            if has_active_live:
                self.set_state("live_normal", "实时采集正常")
            else:
                # 实时模式下未启动播放源或尚未开始，属于正常待机态而非报错
                self.set_state("idle", "待机／未播放")
            return

        # 5.3 回放模式
        if display_mode == "replay":
            if has_active_replay:
                if has_active_live:
                    self.set_state("replay", "当前显示回放 (后台采集中)")
                else:
                    self.set_state("replay", "当前显示回放")
            else:
                # 回放模式但尚未选择数据集 / 播放源未启动
                if has_active_live:
                    self.set_state("replay", "待机 (后台采集中)")
                else:
                    self.set_state("idle", "待机／未播放")
            return

        # 5.4 未知模式兜底
        if has_active_live:
            self.set_state("live_normal", f"模式: {display_mode}")
        else:
            self.set_state("idle", "待机／未播放")

    def show_message(self, title: str, message: str, icon=QSystemTrayIcon.MessageIcon.Information) -> None:
        if self._tray_icon and self._tray_icon.isVisible():
            self._tray_icon.showMessage(title, message, icon, 5000)

    def hide(self) -> None:
        if self._tray_icon:
            self._tray_icon.hide()
