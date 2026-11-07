"""桌面应用程序命令行入口。

用法:
  python -m adsb_desktop [--mode auto|live|replay] [--port 18080] [--dataset 名称] [--duration 秒]
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .app import DesktopApp
from .constants import (
    DEFAULT_HTTP_PORT,
    DESKTOP_LOGS_DIR,
    check_rtl_hardware,
    get_app_icon,
    get_available_datasets,
)


def setup_desktop_logging() -> None:
    DESKTOP_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    log_file = DESKTOP_LOGS_DIR / f"desktop-{ts}.log"

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(sh)
    root_logger.addHandler(fh)




def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adsb_desktop", description="ADS-B 实时航迹地图桌面端")
    p.add_argument("--mode", choices=["auto", "live", "replay"], default="auto",
                   help="运行模式 (auto: 自动检测硬件; live: 实时采集; replay: 回放，默认 auto)")
    p.add_argument("--dataset", type=str, default=None, help="指定回放数据集名称")
    p.add_argument("--duration", type=int, default=0, help="运行秒数，0 为持续运行")

    default_port = int(os.environ.get("ADS_DESKTOP_PORT", DEFAULT_HTTP_PORT))
    p.add_argument("--port", type=int, default=default_port,
                   help=f"桌面 Web 服务端口 (默认 {DEFAULT_HTTP_PORT} 或环境变量 ADS_DESKTOP_PORT)")

    env_lat = float(os.environ["ADS_LAT"]) if "ADS_LAT" in os.environ else None
    env_lon = float(os.environ["ADS_LON"]) if "ADS_LON" in os.environ else None
    env_gain = float(os.environ["ADS_GAIN"]) if "ADS_GAIN" in os.environ else None

    p.add_argument("--lat", type=float, default=env_lat, help="接收机纬度 (默认使用环境变量 ADS_LAT 或后端配置)")
    p.add_argument("--lon", type=float, default=env_lon, help="接收机经度 (默认使用环境变量 ADS_LON 或后端配置)")
    p.add_argument("--gain", type=float, default=env_gain, help="RTL-SDR 增益 (默认使用环境变量 ADS_GAIN 或后端配置)")
    return p


def main(argv: list[str] | None = None) -> int:
    setup_desktop_logging()
    log = logging.getLogger("adsb.desktop.main")

    args = build_parser().parse_args(argv)

    launch_params = {
        "mode": args.mode,
        "port": args.port,
        "dataset": args.dataset,
        "duration": args.duration,
        "lat": args.lat,
        "lon": args.lon,
        "gain": args.gain,
    }

    log.info("初始化桌面应用… 参数: %s", launch_params)

    # Qt 应用配置（确保在任何窗口创建前统一设置应用身份、图标与桌面关联）
    QApplication.setApplicationName("adsb-desktop")
    QApplication.setOrganizationName("adsb")
    QApplication.setApplicationDisplayName("ADS-B 地图")
    QApplication.setDesktopFileName("adsb-desktop")

    qapp = QApplication.instance()
    if qapp is None:
        qapp = QApplication(sys.argv)
    qapp.setDesktopFileName("adsb-desktop")
    qapp.setQuitOnLastWindowClosed(False)

    app_icon = get_app_icon()
    if not app_icon.isNull():
        qapp.setWindowIcon(app_icon)

    app = DesktopApp(qapp, launch_params)

    # 支持终端 Ctrl+C (SIGINT) 与外部 SIGTERM 触发优雅退出
    import signal
    from PySide6.QtCore import QTimer

    def _sig_handler(_sig, _frame):
        log.info("捕获系统退出信号 (%s)，触发桌面程序优雅关闭…", signal.Signals(_sig).name)
        app.quit_gracefully()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    sig_timer = QTimer()
    sig_timer.setInterval(500)
    sig_timer.timeout.connect(lambda: None)
    sig_timer.start()

    return app.run()


if __name__ == "__main__":
    sys.exit(main())
