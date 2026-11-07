"""常量与路径定义。"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（包含 backend, frontend, data, logs 等）
DESKTOP_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = DESKTOP_DIR.parent

BACKEND_DIR = PROJECT_ROOT / "backend"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

# 桌面专用隔离目录
DESKTOP_RUNTIME_DIR = DESKTOP_DIR / "runtime"
DESKTOP_LOGS_DIR = DESKTOP_DIR / "logs"
DESKTOP_CACHE_DIR = DESKTOP_DIR / ".cache"
DESKTOP_RESOURCES_DIR = DESKTOP_DIR / "resources"
WEB_PROFILE_DIR = DESKTOP_RUNTIME_DIR / "web_profile"

# 默认网络与超时设置
DEFAULT_HTTP_PORT = 18080
STARTUP_TIMEOUT_SECONDS = 60
SHUTDOWN_GRACE_SECONDS = 30
SHUTDOWN_KILL_SECONDS = 5
STATUS_POLL_INTERVAL_MS = 2000

# 状态指示色彩 (Hex)
COLOR_STARTING = "#EAB308"   # 黄色：正在启动、预热
COLOR_LIVE_OK = "#22C55E"    # 绿色：实时采集正常
COLOR_REPLAY = "#F97316"     # 橙色：当前显示回放（按要求从蓝色调整为橙色）
COLOR_IDLE = "#3B82F6"       # 蓝色：待机／未播放
COLOR_ERROR = "#EF4444"      # 红色：硬件、采集或后端异常
COLOR_EXITING = "#9CA3AF"    # 灰色：正在退出

# 主题配色定义（与前端 style.css 严格保持一致）
THEME_DARK = {
    "bg": "#181715",
    "chrome_bg": "#1d1c19",
    "surface": "#292722",
    "surface_2": "#333029",
    "line": "#484237",
    "fg": "#f0ebe3",
    "muted": "#b3aa9d",
    "accent": "#d1a078",
    "err": "#f0564f",
    "live": "#3ecf8e",
}

THEME_LIGHT = {
    "bg": "#f4f2ee",
    "chrome_bg": "#e8e5df",
    "surface": "#fcfbf8",
    "surface_2": "#e9e5de",
    "line": "#d7d1c7",
    "fg": "#292723",
    "muted": "#6b655d",
    "accent": "#8d5736",
    "err": "#bb3832",
    "live": "#12744b",
}


def get_app_icon():
    """获取包含多尺寸高清图层的桌面应用图标。"""
    from PySide6.QtGui import QIcon
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256):
        p = DESKTOP_RESOURCES_DIR / f"icon-{size}.png"
        if p.exists():
            icon.addFile(str(p))
    master = DESKTOP_RESOURCES_DIR / "icon.png"
    if master.exists():
        icon.addFile(str(master))
    return icon


def check_rtl_hardware() -> bool:
    """检查系统是否连接 RTL-SDR 硬件设备 (lsusb 方式，不独占设备)。"""
    import shutil
    import subprocess
    if not shutil.which("lsusb"):
        return False
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5).stdout.lower()
        return "0bda:2838" in out or "0bda:2832" in out
    except Exception:
        return False


def get_available_datasets(data_dir: Path | None = None) -> list[str]:
    """获取 data/ 目录下可供回放的数据集列表。"""
    d_dir = data_dir or DATA_DIR
    try:
        from adsb_display.dataset import scan_replay_datasets
        cands = scan_replay_datasets(d_dir)
        return [c.name for c in cands]
    except Exception:
        if not d_dir.is_dir():
            return []
        res: list[str] = []
        for p in d_dir.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                res.append(p.name)
        return sorted(res)


def validate_dataset_path(dataset_path: str | Path) -> tuple[bool, str]:
    """检查数据集路径是否合法并返回 (is_valid, error_reason)。"""
    p = Path(dataset_path)
    if not p.exists():
        return False, f"目录不存在: {dataset_path}"
    if not p.is_dir():
        return False, f"路径不是有效目录: {dataset_path}"
    try:
        from adsb_display.dataset import inspect_dataset_dir
        ds = inspect_dataset_dir(p)
        if ds is None:
            return False, f"目录 {p.name} 不是有效的数据集格式（需包含 logs/tracks-*.jsonl 或 live/history_*.json）"
        return True, ""
    except Exception as e:
        return False, f"解析数据集失败: {e}"


