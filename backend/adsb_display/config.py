"""全局配置与路径。所有路径均基于项目根目录推导，不依赖外部环境。"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录: backend/adsb_display/config.py -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

BACKEND_DIR = PROJECT_ROOT / "backend"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"

# 数据集命名规则: <YYMMDD>_display_<NN>，存放在 data/ 下
DATASET_TAG = "display"

# 接收机默认参数（与参考采集项目 261028_test 一致）
DEFAULT_RECEIVER_LAT = 31.93959020432679   # WGS-84
DEFAULT_RECEIVER_LON = 118.78579982888606  # WGS-84
DEFAULT_GAIN = 40.2
DEFAULT_PPM = 0
DEFAULT_MAX_RANGE_KM = 500

# 显示/状态维护参数
AIRCRAFT_STALE_S = 45        # 超过该秒数未更新，前端调暗
AIRCRAFT_DROP_S = 75         # 超过该秒数未更新，从活动列表移除
TRACK_KEEP_S = 30 * 60       # 轨迹在飞机消失后保留时长
TRACK_MAX_POINTS = 5400      # 单机轨迹最大点数（1 Hz 约 90 分钟）

HTTP_PORT = int(os.environ.get("PORT", "8080"))

READSB_BIN = "/usr/bin/readsb"

# readsb raw 输出端口（--net-ro-port）：后端直连该端口逐帧统计 DF17 且 CRC 通过的报文
READSB_RAW_HOST = "127.0.0.1"
READSB_RAW_PORT = int(os.environ.get("ADS_RAW_PORT", "30002"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
