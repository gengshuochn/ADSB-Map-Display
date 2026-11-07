#!/usr/bin/env bash
# ADS-B 桌面版独立环境初始化脚本
#
# 用途:
#   使用 Python 3.14 创建桌面独立虚拟环境 desktop/.venv
#   并使用 uv 严格根据 requirements.lock（包含校验哈希）安装 PySide6 及依赖
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VENV_DIR="$SCRIPT_DIR/.venv"
CACHE_DIR="$SCRIPT_DIR/.cache/uv"
LOCK_FILE="$SCRIPT_DIR/requirements.lock"

echo "=============================================="
echo " ADS-B 桌面版环境初始化"
echo " 根目录: $PROJECT_ROOT"
echo " 桌面目录: $SCRIPT_DIR"
echo " 虚拟环境: $VENV_DIR"
echo "=============================================="

# 检查系统 Python 3.14
if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3 命令" >&2
    exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "$PY_VER" != "3.14" ]; then
    echo "[警告] 当前系统 python3 版本为 $PY_VER，规划要求基线为 Python 3.14" >&2
fi

# 检查 uv
if ! command -v uv >/dev/null 2>&1; then
    echo "[错误] 未找到 uv 命令，请确保 uv 已安装并位于 PATH 中" >&2
    exit 1
fi

# 创建独立虚拟环境
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "[1/3] 正在创建独立虚拟环境 $VENV_DIR (基于 Python $PY_VER)..."
    uv venv --python python3 --no-python-downloads "$VENV_DIR"
else
    echo "[1/3] 独立虚拟环境已存在: $VENV_DIR"
fi

# 安装依赖
echo "[2/3] 使用 uv 根据 requirements.lock 安装锁定依赖..."
mkdir -p "$CACHE_DIR"
uv pip sync "$LOCK_FILE" \
    --python "$VENV_DIR/bin/python" \
    --cache-dir "$CACHE_DIR" \
    --no-python-downloads \
    --only-binary :all:

# 校验依赖可用性
echo "[3/3] 正在校验 PySide6 与 Qt WebEngine 模块..."
"$VENV_DIR/bin/python" -c "
import PySide6
from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
print(f'PySide6 {PySide6.__version__} (Qt {qVersion()}) 校验通过，QtWebEngine 正常就绪。')
"

echo "=============================================="
echo " 桌面版环境初始化成功！"
echo "=============================================="
