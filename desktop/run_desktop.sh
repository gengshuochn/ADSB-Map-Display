#!/usr/bin/env bash
# ADS-B 实时地图桌面端启动脚本
#
# 支持从任意工作目录调用，支持项目路径含空格。
# 日常启动只校验虚拟环境与依赖可用性，不自动安装或升级。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VENV_PY="$SCRIPT_DIR/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "[错误] 桌面虚拟环境未就绪 ($VENV_PY 不存在或不可执行)" >&2
    echo "请先在项目目录运行初始化脚本: ./desktop/setup_env.sh" >&2
    exit 1
fi

# 快速环境完整性校验（日常启动不执行 pip 或升级）
if ! "$VENV_PY" -c "import PySide6, PySide6.QtWebEngineWidgets" >/dev/null 2>&1; then
    echo "[错误] 桌面虚拟环境中 PySide6 或 QtWebEngine 依赖缺失或损坏" >&2
    echo "请重新运行环境初始化: ./desktop/setup_env.sh" >&2
    exit 1
fi

# 设置模块搜索路径（桌面源码与原后端包）
export PYTHONPATH="$SCRIPT_DIR/src:$PROJECT_ROOT/backend:${PYTHONPATH:-}"

exec "$VENV_PY" -m adsb_desktop "$@"
