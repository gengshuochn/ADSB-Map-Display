"""桌面主窗口与界面管理。

包含：
- QMainWindow + QWebEngineView；
- 自适应深浅主题同步（双向跟随前端 data-theme / localStorage，避免白屏闪烁）；
- 统一桌面视觉规范与自适应标题栏（保留系统拖动、缩放、最大化/还原、最小化及托盘关闭）；
- 独立持久化 QWebEngineProfile（网页配置与缓存写入 desktop/runtime/web_profile）；
- 首次尺寸 1440x900（最小 1024x768），保存与恢复窗口尺寸/最大化，避免越界；
- 本地启动加载界面与异常错误处理界面（支持“重试”与“退出”）；
- 窗口关闭拦截（托盘可用时隐藏并首次通知；托盘不可用时正常退出）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QMouseEvent,
    QPalette,
    QPixmap,
)
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .constants import (
    DATA_DIR,
    DESKTOP_LOGS_DIR,
    DESKTOP_RUNTIME_DIR,
    LOGS_DIR,
    THEME_DARK,
    THEME_LIGHT,
    WEB_PROFILE_DIR,
    get_app_icon,
)
from .tray import SystemTrayManager

log = logging.getLogger("adsb.desktop.window")

# 注入到 WebEngine 页面的轻量级双向监听脚本（无侵入、不修改浏览器版行为）
JS_BRIDGE_CODE = """
(function() {
    function getTheme() {
        try {
            var docEl = document.documentElement;
            if (docEl && docEl.dataset && docEl.dataset.theme) {
                return docEl.dataset.theme;
            }
            if (docEl && docEl.getAttribute) {
                var attr = docEl.getAttribute('data-theme');
                if (attr) return attr;
            }
            return localStorage.getItem('adsb-theme');
        } catch (e) {
            return null;
        }
    }

    function reportTheme() {
        try {
            var t = getTheme();
            if (t === 'dark' || t === 'light') {
                console.info('[ADS_DESKTOP_THEME:' + t + ']');
            }
        } catch (e) {}
    }

    function reportMode() {
        try {
            var badge = document.getElementById('mode-badge');
            if (badge) {
                var m = (badge.textContent || '').trim().toLowerCase();
                if (m === 'live' || m === 'replay' || m === 'idle' || m === 'standby') {
                    console.info('[ADS_DESKTOP_MODE:' + m + ']');
                }
            }
        } catch (e) {}
    }

    function setupThemeObserver() {
        if (!document.documentElement) return false;
        try {
            var themeObserver = new MutationObserver(function(mutations) {
                for (var i = 0; i < mutations.length; i++) {
                    if (mutations[i].attributeName === 'data-theme') {
                        reportTheme();
                        break;
                    }
                }
            });
            themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
            return true;
        } catch (e) {
            return false;
        }
    }

    function setupModeObserver() {
        try {
            var badge = document.getElementById('mode-badge');
            if (!badge) return false;
            var modeObserver = new MutationObserver(function() {
                reportMode();
            });
            modeObserver.observe(badge, { childList: true, characterData: true, subtree: true, attributes: true });
            return true;
        } catch (e) {
            return false;
        }
    }

    function initBridge() {
        // 如果已成功安装过监听器，仅补报状态
        if (window.__adsb_desktop_bridge_injected) {
            reportTheme();
            reportMode();
            return;
        }

        // 必须确保 documentElement 可用
        if (!document.documentElement) {
            return;
        }

        // 安装主题 MutationObserver
        var themeOk = setupThemeObserver();
        if (!themeOk) {
            // 安装失败时不打标记，等待下次事件再次尝试
            return;
        }

        // 仅在安装成功后记录标记
        window.__adsb_desktop_bridge_injected = true;

        // 立即报告一次当前主题
        reportTheme();

        // 尝试挂载模式监听
        if (!setupModeObserver()) {
            function attachMode() {
                reportMode();
                setupModeObserver();
            }
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', attachMode);
            } else {
                attachMode();
            }
        } else {
            reportMode();
        }
    }

    // 1. 若当前 DOM 已就绪，立即初始化
    initBridge();

    // 2. 若尚未成功安装（例如在 DocumentCreation 阶段 documentElement 尚未创建），监听就绪事件
    if (!window.__adsb_desktop_bridge_injected) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initBridge);
            document.addEventListener('readystatechange', function() {
                if (document.readyState === 'interactive' || document.readyState === 'complete') {
                    initBridge();
                }
            });
        }
    }
})();
"""


class CustomWebPage(QWebEnginePage):
    """拦截特定格式控制台消息以实现低延迟主题与显示模式同步。"""

    theme_detected = Signal(str)
    mode_detected = Signal(str)

    def javaScriptConsoleMessage(
        self,
        level: QWebEnginePage.JavaScriptConsoleMessageLevel,
        message: str,
        line_number: int,
        source_id: str,
    ) -> None:
        if message.startswith("[ADS_DESKTOP_THEME:") and message.endswith("]"):
            theme = message[len("[ADS_DESKTOP_THEME:"):-1].strip()
            if theme in ("dark", "light"):
                self.theme_detected.emit(theme)
                return
        elif message.startswith("[ADS_DESKTOP_MODE:") and message.endswith("]"):
            mode = message[len("[ADS_DESKTOP_MODE:"):-1].strip()
            if mode in ("live", "replay", "idle", "standby"):
                self.mode_detected.emit(mode)
                return
        super().javaScriptConsoleMessage(level, message, line_number, source_id)


class HeaderBarWidget(QWidget):
    """自适应主题的窗口标题栏（适配 Wayland CSD 与桌面外观）。"""

    def __init__(self, parent: MainWindow) -> None:
        super().__init__(parent)
        self._window = parent
        self.setFixedHeight(38)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(8)

        # 图标
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(20, 20)
        icon = get_app_icon()
        if not icon.isNull():
            self.icon_label.setPixmap(icon.pixmap(20, 20))
        self.icon_label.setScaledContents(True)

        # 标题文本
        self.title_label = QLabel("ADS-B 实时航迹地图")
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        self.title_label.setFont(font)

        layout.addWidget(self.icon_label)
        layout.addWidget(self.title_label)
        layout.addStretch()

        # 窗口控制按钮
        self.btn_min = QPushButton("—")
        self.btn_min.setToolTip("最小化")
        self.btn_max = QPushButton("□")
        self.btn_max.setToolTip("最大化 / 还原")
        self.btn_close = QPushButton("✕")
        self.btn_close.setToolTip("关闭 (隐藏到托盘)")
        self.btn_close.setObjectName("btnClose")

        for b in (self.btn_min, self.btn_max, self.btn_close):
            b.setFixedSize(28, 28)
            layout.addWidget(b)

        self.btn_min.clicked.connect(self._window.showMinimized)
        self.btn_max.clicked.connect(self._toggle_maximize)
        self.btn_close.clicked.connect(self._window.close)

    def _toggle_maximize(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
            self.btn_max.setText("□")
        else:
            self._window.showMaximized()
            self.btn_max.setText("❐")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            wh = self._window.windowHandle()
            if wh and hasattr(wh, "startSystemMove"):
                wh.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def apply_theme(self, theme: str) -> None:
        t = THEME_DARK if theme == "dark" else THEME_LIGHT
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {t['chrome_bg']};
                border-bottom: 1px solid {t['line']};
            }}
            QLabel {{
                color: {t['fg']};
                border: none;
                background: transparent;
            }}
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 6px;
                color: {t['fg']};
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {t['surface_2']};
            }}
            QPushButton#btnClose:hover {{
                background-color: #e11d48;
                color: #ffffff;
            }}
        """)


class LoadingWidget(QWidget):
    """简洁的本地启动加载界面。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(16)

        self.title = QLabel("ADS-B 实时地图系统")
        title_font = QFont()
        title_font.setPointSize(20)
        title_font.setBold(True)
        self.title.setFont(title_font)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.status_label = QLabel("正在启动后端服务，准备地图渲染环境…")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # 循环流动指示
        self.bar.setFixedWidth(360)
        self.bar.setFixedHeight(6)
        self.bar.setTextVisible(False)

        layout.addWidget(self.title)
        layout.addWidget(self.status_label)
        layout.addWidget(self.bar, alignment=Qt.AlignmentFlag.AlignCenter)

        self.apply_theme("dark")

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def apply_theme(self, theme: str) -> None:
        t = THEME_DARK if theme == "dark" else THEME_LIGHT
        self.setStyleSheet(f"background-color: {t['bg']};")
        self.title.setStyleSheet(f"color: {t['fg']}; margin-bottom: 8px; border: none; background: transparent;")
        self.status_label.setStyleSheet(f"color: {t['muted']}; font-size: 14px; border: none; background: transparent;")
        self.bar.setStyleSheet(f"""
            QProgressBar {{
                background: {t['surface_2']};
                border-radius: 3px;
                border: none;
            }}
            QProgressBar::chunk {{
                background: {t['accent']};
                border-radius: 3px;
            }}
        """)


class ErrorWidget(QWidget):
    """异常展示窗口（提供错误原因、日志位置、重试与退出按钮）。"""

    retry_clicked = Signal()
    quit_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(16)

        self.title_label = QLabel("服务启动或运行异常")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.error_text = QTextEdit()
        self.error_text.setReadOnly(True)
        self.error_text.setFixedSize(680, 220)

        self.log_hint = QLabel()
        self.log_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(20)

        self.btn_retry = QPushButton("重新尝试")
        self.btn_retry.setFixedSize(140, 40)
        self.btn_retry.clicked.connect(self.retry_clicked.emit)

        self.btn_quit = QPushButton("退出应用")
        self.btn_quit.setFixedSize(140, 40)
        self.btn_quit.clicked.connect(self.quit_clicked.emit)

        btn_layout.addWidget(self.btn_retry)
        btn_layout.addWidget(self.btn_quit)

        layout.addWidget(self.title_label)
        layout.addWidget(self.error_text, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.log_hint)
        layout.addLayout(btn_layout)

        self.apply_theme("dark")

    def set_error(self, title: str, message: str) -> None:
        self.title_label.setText(title)
        self.error_text.setPlainText(message)
        self.log_hint.setText(f"桌面日志目录: {DESKTOP_LOGS_DIR}\n后端日志目录: {LOGS_DIR}")

    def apply_theme(self, theme: str) -> None:
        t = THEME_DARK if theme == "dark" else THEME_LIGHT
        self.setStyleSheet(f"background-color: {t['bg']};")
        self.title_label.setStyleSheet(f"color: {t['err']}; border: none; background: transparent;")
        self.error_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: {t['chrome_bg']};
                color: {t['fg']};
                font-family: monospace;
                font-size: 13px;
                border: 1px solid {t['line']};
                border-radius: 6px;
                padding: 10px;
            }}
        """)
        self.log_hint.setStyleSheet(f"color: {t['muted']}; font-size: 13px; border: none; background: transparent;")
        self.btn_retry.setStyleSheet(f"""
            QPushButton {{
                background-color: {t['accent']};
                color: #ffffff;
                font-weight: bold;
                border-radius: 6px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: {t['surface_2']};
            }}
        """)
        self.btn_quit.setStyleSheet(f"""
            QPushButton {{
                background-color: {t['surface_2']};
                color: {t['fg']};
                font-weight: bold;
                border-radius: 6px;
                border: 1px solid {t['line']};
            }}
            QPushButton:hover {{
                background-color: {t['line']};
            }}
        """)


class IdleWidget(QWidget):
    """待机与无播放源状态界面（展示待机提示、提供重新检测、选择数据集、打开目录与退出按钮）。"""

    retry_clicked = Signal()
    open_data_clicked = Signal()
    select_dataset_clicked = Signal()
    quit_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(18)

        # 待机徽标
        self.badge_label = QLabel("待机模式 (IDLE)")
        badge_font = QFont()
        badge_font.setPointSize(11)
        badge_font.setBold(True)
        self.badge_label.setFont(badge_font)
        self.badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 标题
        self.title_label = QLabel("系统待机／未播放")
        title_font = QFont()
        title_font.setPointSize(20)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 详细说明
        self.desc_text = QLabel()
        self.desc_text.setWordWrap(True)
        self.desc_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.desc_text.setFixedWidth(580)
        desc_font = QFont()
        desc_font.setPointSize(13)
        self.desc_text.setFont(desc_font)

        # 操作提示卡片
        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setFixedWidth(580)

        # 按钮栏
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(14)
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.btn_retry = QPushButton("重新检测并启动")
        self.btn_retry.setFixedSize(150, 40)
        self.btn_retry.setToolTip("重新检测 RTL-SDR 硬件及 data/ 目录下新增的数据集")
        self.btn_retry.clicked.connect(self.retry_clicked.emit)

        self.btn_select_dataset = QPushButton("选择数据集目录…")
        self.btn_select_dataset.setFixedSize(150, 40)
        self.btn_select_dataset.setToolTip("选择外部航迹数据集文件夹进行回放")
        self.btn_select_dataset.clicked.connect(self.select_dataset_clicked.emit)

        self.btn_open_data = QPushButton("打开数据目录")
        self.btn_open_data.setFixedSize(140, 40)
        self.btn_open_data.setToolTip("在系统文件管理器中打开 data/ 存储目录")
        self.btn_open_data.clicked.connect(self.open_data_clicked.emit)

        self.btn_quit = QPushButton("退出应用")
        self.btn_quit.setFixedSize(110, 40)
        self.btn_quit.clicked.connect(self.quit_clicked.emit)

        btn_layout.addWidget(self.btn_retry)
        btn_layout.addWidget(self.btn_select_dataset)
        btn_layout.addWidget(self.btn_open_data)
        btn_layout.addWidget(self.btn_quit)

        layout.addWidget(self.badge_label, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_text, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.hint_label, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(8)
        layout.addLayout(btn_layout)

        self.apply_theme("dark")

    def set_idle_info(self, title: str, message: str) -> None:
        self.title_label.setText(title)
        self.desc_text.setText(message)
        self.hint_label.setText(
            f"• 实时采集：接入 RTL-SDR Blog V4 / RTL2832U 后点击“重新检测并启动”\n"
            f"• 数据回放：将历史采集文件夹放入 {DATA_DIR} 或通过“选择数据集目录”载入"
        )

    def apply_theme(self, theme: str) -> None:
        t = THEME_DARK if theme == "dark" else THEME_LIGHT
        self.setStyleSheet(f"background-color: {t['bg']};")
        badge_style = (
            "background-color: #1e3a8a; color: #60a5fa; border: 1px solid #3b82f6; border-radius: 12px; padding: 4px 16px;"
            if theme == "dark" else
            "background-color: #dbeafe; color: #1d4ed8; border: 1px solid #93c5fd; border-radius: 12px; padding: 4px 16px;"
        )
        self.badge_label.setStyleSheet(badge_style)
        self.title_label.setStyleSheet(f"color: {t['fg']}; border: none; background: transparent;")
        self.desc_text.setStyleSheet(f"color: {t['fg']}; border: none; background: transparent; line-height: 1.5;")
        self.hint_label.setStyleSheet(f"""
            color: {t['muted']};
            font-size: 13px;
            border: 1px solid {t['line']};
            background-color: {t['chrome_bg']};
            border-radius: 6px;
            padding: 12px 16px;
            line-height: 1.6;
        """)
        self.btn_retry.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6;
                color: #ffffff;
                font-weight: bold;
                border-radius: 6px;
                border: none;
            }
            QPushButton:hover {
                background-color: #2563eb;
            }
        """)
        self.btn_select_dataset.setStyleSheet(f"""
            QPushButton {{
                background-color: {t['surface_2']};
                color: {t['fg']};
                font-weight: bold;
                border-radius: 6px;
                border: 1px solid {t['line']};
            }}
            QPushButton:hover {{
                background-color: {t['line']};
            }}
        """)
        self.btn_open_data.setStyleSheet(f"""
            QPushButton {{
                background-color: {t['surface_2']};
                color: {t['fg']};
                font-weight: bold;
                border-radius: 6px;
                border: 1px solid {t['line']};
            }}
            QPushButton:hover {{
                background-color: {t['line']};
            }}
        """)
        self.btn_quit.setStyleSheet(f"""
            QPushButton {{
                background-color: {t['surface']};
                color: {t['muted']};
                border-radius: 6px;
                border: 1px solid {t['line']};
            }}
            QPushButton:hover {{
                background-color: {t['line']};
                color: {t['fg']};
            }}
        """)


_shared_web_profile: QWebEngineProfile | None = None


def get_shared_web_profile(parent: QObject | None = None) -> QWebEngineProfile:
    """获取单例持久化 QWebEngineProfile，避免同进程多窗口或测试重入时的目录锁冲突。"""
    global _shared_web_profile
    if _shared_web_profile is None:
        WEB_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        storage_path = str(WEB_PROFILE_DIR / "storage")
        cache_path = str(WEB_PROFILE_DIR / "cache")

        qapp = QApplication.instance()
        profile = QWebEngineProfile("adsb_desktop_profile", parent or qapp)
        profile.setPersistentStoragePath(storage_path)
        profile.setCachePath(cache_path)
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)

        # 注入双向主题与模式同步脚本
        bridge_script = QWebEngineScript()
        bridge_script.setName("adsb_theme_mode_bridge")
        bridge_script.setSourceCode(JS_BRIDGE_CODE)
        bridge_script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        bridge_script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        bridge_script.setRunsOnSubFrames(False)
        profile.scripts().insert(bridge_script)
        _shared_web_profile = profile
    return _shared_web_profile


class MainWindow(QMainWindow):
    quit_requested = Signal()
    retry_requested = Signal()
    dataset_chosen = Signal(str)

    def __init__(
        self,
        tray: SystemTrayManager | None = None,
        profile: QWebEngineProfile | None = None,
    ) -> None:
        # 支持环境配置是否强行回退至系统原生标题栏（默认启用自适应 CSD 标题栏以确保深色模式下完美无缝跟随）
        self._use_system_titlebar = os.environ.get("ADS_USE_SYSTEM_TITLEBAR", "0") == "1"
        window_flags = Qt.WindowType.Window
        if not self._use_system_titlebar:
            window_flags |= Qt.WindowType.FramelessWindowHint

        super().__init__(None, window_flags)
        self._tray = tray
        self.setWindowTitle("ADS-B 实时航迹地图")
        self.setMinimumSize(1024, 768)

        # 统一设置窗口图标
        app_icon = get_app_icon()
        if not app_icon.isNull():
            self.setWindowIcon(app_icon)

        self._state_file = DESKTOP_RUNTIME_DIR / "window_state.json"
        self._has_notified_hide = False
        self._current_theme = "dark"

        # 恢复窗口尺寸与持久化主题设置
        self._restore_geometry_and_state()

        # 主界面与布局构造
        self._container = QWidget(self)
        self._container.setObjectName("mainContainer")
        self.setCentralWidget(self._container)
        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # 标题栏
        self._title_bar = HeaderBarWidget(self)
        if not self._use_system_titlebar:
            container_layout.addWidget(self._title_bar)
            self.setMouseTracking(True)
            self._container.setMouseTracking(True)
        else:
            self._title_bar.hide()

        # 核心堆叠组件
        self._stack = QStackedWidget(self._container)
        container_layout.addWidget(self._stack)

        self._loading_widget = LoadingWidget(self._stack)
        self._error_widget = ErrorWidget(self._stack)
        self._error_widget.retry_clicked.connect(self.retry_requested.emit)
        self._error_widget.quit_clicked.connect(self.quit_requested.emit)

        self._idle_widget = IdleWidget(self._stack)
        self._idle_widget.retry_clicked.connect(self.retry_requested.emit)
        self._idle_widget.open_data_clicked.connect(self._on_open_data_clicked)
        self._idle_widget.select_dataset_clicked.connect(self._on_select_dataset_clicked)
        self._idle_widget.quit_clicked.connect(self.quit_requested.emit)

        # 独立持久化 WebEngine 配置（复用同进程单例配置以防目录冲突）
        self._profile = profile or get_shared_web_profile(self)

        self._web_view = QWebEngineView(self._stack)
        self._web_page = CustomWebPage(self._profile, self._web_view)
        self._web_view.setPage(self._web_page)

        # 关联网页主题和模式同步信号
        self._web_page.theme_detected.connect(self.set_theme)
        self._web_page.mode_detected.connect(self._on_web_mode_detected)
        self._web_view.loadFinished.connect(self._on_load_finished)

        self._stack.addWidget(self._loading_widget)  # index 0
        self._stack.addWidget(self._web_view)        # index 1
        self._stack.addWidget(self._error_widget)    # index 2
        self._stack.addWidget(self._idle_widget)     # index 3

        # 启动时立即生效恢复的主题，防止首帧亮白闪烁
        self.apply_theme(self._current_theme)

        self.show_loading("正在初始化服务…")

    @property
    def current_theme(self) -> str:
        return self._current_theme

    def set_theme(self, theme: str) -> None:
        """设置主题并持久化（由页面切换、初始化检测或外部触发）。"""
        if theme not in ("dark", "light"):
            return
        if self._current_theme != theme:
            log.info("同步界面主题: %s -> %s", self._current_theme, theme)
            self._current_theme = theme
            self.apply_theme(theme)
            self._save_geometry_and_state()

        # 同步至网页端（若已加载且网页端尚未同步）
        js = (
            f"if (document.documentElement && document.documentElement.dataset.theme !== '{theme}') {{"
            f"  document.documentElement.dataset.theme = '{theme}';"
            f"  try {{ localStorage.setItem('adsb-theme', '{theme}'); }} catch(e) {{}}"
            f"}}"
        )
        self._web_view.page().runJavaScript(js)

    def apply_theme(self, theme: str) -> None:
        """应用单应用调色板、样式表与各组件配色。"""
        t = THEME_DARK if theme == "dark" else THEME_LIGHT

        # 1. 标题栏
        self._title_bar.apply_theme(theme)

        # 2. 本地子页面
        self._loading_widget.apply_theme(theme)
        self._error_widget.apply_theme(theme)
        self._idle_widget.apply_theme(theme)

        # 3. WebEngine 视口与背景色，彻底杜绝刷新/加载时的白屏闪烁
        self._web_page.setBackgroundColor(QColor(t["bg"]))
        self._web_view.setStyleSheet(f"background-color: {t['bg']}; border: none;")

        # 4. 主容器边框及背景
        border_style = f"1px solid {t['line']}" if not self._use_system_titlebar else "none"
        self._container.setStyleSheet(f"""
            QWidget#mainContainer {{
                background-color: {t['bg']};
                border: {border_style};
            }}
        """)

        # 5. Qt 单应用颜色方案 (Qt 6.5+)
        qapp = QApplication.instance()
        if qapp:
            scheme = Qt.ColorScheme.Dark if theme == "dark" else Qt.ColorScheme.Light
            if hasattr(qapp.styleHints(), "setColorScheme"):
                qapp.styleHints().setColorScheme(scheme)

            # 6. 设置全局调色板（使未显式样式的弹窗、上下文菜单及原生对话框自动匹配）
            pal = QPalette()
            pal.setColor(QPalette.ColorRole.Window, QColor(t["bg"]))
            pal.setColor(QPalette.ColorRole.WindowText, QColor(t["fg"]))
            pal.setColor(QPalette.ColorRole.Base, QColor(t["chrome_bg"]))
            pal.setColor(QPalette.ColorRole.AlternateBase, QColor(t["surface"]))
            pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(t["surface_2"]))
            pal.setColor(QPalette.ColorRole.ToolTipText, QColor(t["fg"]))
            pal.setColor(QPalette.ColorRole.Text, QColor(t["fg"]))
            pal.setColor(QPalette.ColorRole.Button, QColor(t["surface"]))
            pal.setColor(QPalette.ColorRole.ButtonText, QColor(t["fg"]))
            pal.setColor(QPalette.ColorRole.Highlight, QColor(t["accent"]))
            pal.setColor(QPalette.ColorRole.HighlightedText, QColor(t["bg"]))
            qapp.setPalette(pal)

            # 7. 弹窗与菜单统一样式定义
            qapp.setStyleSheet(f"""
                QMenu {{
                    background-color: {t['surface']};
                    color: {t['fg']};
                    border: 1px solid {t['line']};
                    padding: 4px 0px;
                }}
                QMenu::item {{
                    padding: 6px 24px 6px 20px;
                }}
                QMenu::item:selected {{
                    background-color: {t['surface_2']};
                    color: {t['fg']};
                }}
                QMenu::separator {{
                    height: 1px;
                    background: {t['line']};
                    margin: 4px 8px;
                }}
                QMessageBox {{
                    background-color: {t['surface']};
                    color: {t['fg']};
                }}
                QMessageBox QLabel {{
                    color: {t['fg']};
                }}
                QMessageBox QPushButton {{
                    background-color: {t['surface_2']};
                    color: {t['fg']};
                    border: 1px solid {t['line']};
                    border-radius: 4px;
                    padding: 6px 16px;
                    min-width: 64px;
                }}
                QMessageBox QPushButton:hover {{
                    background-color: {t['line']};
                }}
                QToolTip {{
                    background-color: {t['surface_2']};
                    color: {t['fg']};
                    border: 1px solid {t['line']};
                }}
            """)

    def _on_web_mode_detected(self, mode: str) -> None:
        if self._tray:
            self._tray.set_display_mode(mode)

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            # 网页加载完成后主动读取一次当前实际主题以补漏，保证以页面实际状态为准
            self._web_page.runJavaScript(
                "document.documentElement ? (document.documentElement.dataset.theme || localStorage.getItem('adsb-theme')) : ''",
                self._on_js_theme_result,
            )

    def _on_js_theme_result(self, result: object) -> None:
        if isinstance(result, str) and result in ("dark", "light"):
            self.set_theme(result)

    def _detect_resize_edge(self, pos: QPoint) -> Qt.Edge:
        if self.isMaximized():
            return Qt.Edge(0)
        margin = 6
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        edge = Qt.Edge(0)
        if x <= margin:
            edge |= Qt.Edge.LeftEdge
        elif x >= w - margin:
            edge |= Qt.Edge.RightEdge
        if y <= margin:
            edge |= Qt.Edge.TopEdge
        elif y >= h - margin:
            edge |= Qt.Edge.BottomEdge
        return edge

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._use_system_titlebar and event.button() == Qt.MouseButton.LeftButton:
            edge = self._detect_resize_edge(event.position().toPoint())
            if edge != Qt.Edge(0):
                wh = self.windowHandle()
                if wh and hasattr(wh, "startSystemResize"):
                    wh.startSystemResize(edge)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._use_system_titlebar and not self.isMaximized():
            edge = self._detect_resize_edge(event.position().toPoint())
            if edge in (Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.Edge.BottomEdge | Qt.Edge.RightEdge):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif edge in (Qt.Edge.TopEdge | Qt.Edge.RightEdge, Qt.Edge.BottomEdge | Qt.Edge.LeftEdge):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif edge & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif edge & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.unsetCursor()
        super().mouseMoveEvent(event)

    def show_loading(self, msg: str = "正在加载…") -> None:
        self._loading_widget.set_status(msg)
        self._stack.setCurrentWidget(self._loading_widget)

    def load_url(self, url: str) -> None:
        log.info("主窗口加载地图 URL: %s", url)
        self._web_view.load(QUrl(url))
        self._stack.setCurrentWidget(self._web_view)

    def show_error(self, title: str, message: str) -> None:
        log.error("主窗口显示错误页面: %s: %s", title, message)
        self._error_widget.set_error(title, message)
        self._stack.setCurrentWidget(self._error_widget)
        self.show()
        self.raise_()
        self.activateWindow()

    def show_idle(self, title: str, message: str) -> None:
        log.info("主窗口显示待机页面: %s", title)
        self._idle_widget.set_idle_info(title, message)
        self._stack.setCurrentWidget(self._idle_widget)
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_open_data_clicked(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR)))

    def _on_select_dataset_clicked(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        chosen = QFileDialog.getExistingDirectory(
            self,
            "选择 ADS-B 航迹数据集目录",
            str(DATA_DIR),
            QFileDialog.Option.ShowDirsOnly,
        )
        if chosen:
            self.dataset_chosen.emit(chosen)

    def activate_window(self) -> None:
        if self.isMinimized() or not self.isVisible():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_geometry_and_state()
        if self._tray and self._tray.is_available:
            event.ignore()
            self.hide()
            if not self._has_notified_hide:
                self._has_notified_hide = True
                self._save_state_flags()
                self._tray.show_message(
                    "ADS-B 地图",
                    "应用已转入后台运行，采集仍在持续。\n可双击或右键托盘图标恢复窗口或退出。"
                )
        else:
            # 托盘不可用时触发正常退出流程；
            # 使用 event.ignore() 阻止 Qt 因 quitOnLastWindowClosed 提前结束事件循环，
            # 隐藏窗口并交由 app 异步优雅关闭后端后再统一退出。
            log.info("托盘不可用，窗口关闭事件触发应用退出流程。")
            event.ignore()
            self.hide()
            self.quit_requested.emit()

    def _restore_geometry_and_state(self) -> None:
        DESKTOP_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        restored = False
        if self._state_file.exists():
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                x = data.get("x")
                y = data.get("y")
                w = data.get("width", 1440)
                h = data.get("height", 900)
                maximized = data.get("maximized", False)
                self._has_notified_hide = data.get("has_notified_hide", False)
                self._current_theme = data.get("theme", "dark")

                if x is not None and y is not None:
                    rect = QRect(x, y, w, h)
                    # 检查是否落在有效屏幕内
                    is_on_screen = False
                    for screen in QGuiApplication.screens():
                        if screen.availableGeometry().intersects(rect):
                            is_on_screen = True
                            break
                    if is_on_screen:
                        self.setGeometry(rect)
                        if maximized:
                            self.showMaximized()
                        restored = True
            except Exception as e:
                log.warning("恢复窗口尺寸状态失败: %s", e)

        if not restored:
            self._apply_default_geometry()

    def _apply_default_geometry(self) -> None:
        target_w, target_h = 1440, 900
        screen = QGuiApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            w = min(target_w, avail.width() - 40)
            h = min(target_h, avail.height() - 40)
            w = max(w, 1024)
            h = max(h, 768)
            x = avail.x() + (avail.width() - w) // 2
            y = avail.y() + (avail.height() - h) // 2
            self.setGeometry(x, y, w, h)
        else:
            self.resize(target_w, target_h)

    def _save_geometry_and_state(self) -> None:
        try:
            is_max = self.isMaximized()
            geom = self.normalGeometry() if is_max else self.geometry()
            data = {
                "x": geom.x(),
                "y": geom.y(),
                "width": geom.width(),
                "height": geom.height(),
                "maximized": is_max,
                "has_notified_hide": self._has_notified_hide,
                "theme": self._current_theme,
            }
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning("保存窗口状态失败: %s", e)

    def _save_state_flags(self) -> None:
        self._save_geometry_and_state()
