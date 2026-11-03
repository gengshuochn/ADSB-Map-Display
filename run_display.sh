#!/usr/bin/env bash
# ADS-B 实时地图显示 — 一键启动脚本
#
# 用法:
#   ./run_display.sh                  # 自动检测: 有 RTL-SDR 则实时采集, 否则回放最新数据集
#   ./run_display.sh live             # 强制实时采集模式
#   ./run_display.sh live 300         # 实时采集 300 秒后自动退出
#   ./run_display.sh replay           # 回放最新可回放数据集
#   ./run_display.sh replay 261028_test_reference
#
# 环境变量:
#   PORT=8080        Web 服务端口
#   ADS_LAT/ADS_LON  接收机坐标 (WGS-84), 默认与参考采集项目一致
#   ADS_GAIN         RTL-SDR 增益, 默认 40.2
#   NO_OPEN=1        不自动打开浏览器
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT="${PORT:-8080}"
MODE="${1:-auto}"
DURATION_ARG="${2:-}"

# ---------------------------------------------------------------- venv
if [ ! -x ".venv/bin/python" ]; then
  echo "[init] 创建虚拟环境 .venv (无需 pip, 后端为纯标准库实现)…"
  python3 -m venv --without-pip .venv
fi
PY="$ROOT/.venv/bin/python"

# ---------------------------------------------------------------- 模式检测
detect_rtl() {
  # RTL2838 / RTL2832U (RTL-SDR Blog V4 等)
  lsusb 2>/dev/null | grep -qiE "0bda:2838|0bda:2832"
}

if [ "$MODE" = "auto" ]; then
  if detect_rtl; then
    MODE="live"
  else
    MODE="replay"
    echo "[warn] 未检测到 RTL-SDR 设备, 自动切换到回放模式"
  fi
fi

LIVE_ARGS=(serve --mode live --port "$PORT")
if [ -n "${ADS_LAT:-}" ]; then LIVE_ARGS+=(--lat "$ADS_LAT"); fi
if [ -n "${ADS_LON:-}" ]; then LIVE_ARGS+=(--lon "$ADS_LON"); fi
if [ -n "${ADS_GAIN:-}" ]; then LIVE_ARGS+=(--gain "$ADS_GAIN"); fi
if [ -n "$DURATION_ARG" ]; then LIVE_ARGS+=(--duration "$DURATION_ARG"); fi

REPLAY_ARGS=(serve --mode replay --port "$PORT")
if [ -n "${2:-}" ] && [ "$MODE" = "replay" ]; then REPLAY_ARGS+=(--dataset "$2"); fi

# ---------------------------------------------------------------- 提示
echo "=============================================="
echo " ADS-B 实时地图显示"
echo " 模式: $MODE   端口: $PORT"
if [ "$MODE" = "live" ]; then
  echo " 数据集: data/<当天日期>_display_<NN>/ (自动命名)"
  echo " 提示: 如有其他程序占用 RTL-SDR, 请先停止"
else
  echo " 数据集: data/ 下已收集数据集 (可用 $PY -m adsb_display list-datasets 查看)"
fi
echo " 打开: http://127.0.0.1:$PORT"
echo " Ctrl-C 停止 (采集数据已自动保存到 data/)"
echo "=============================================="

if [ -z "${NO_OPEN:-}" ] && command -v xdg-open >/dev/null 2>&1; then
  ( sleep 1.5; xdg-open "http://127.0.0.1:$PORT" >/dev/null 2>&1 || true ) &
fi

# ---------------------------------------------------------------- 启动
cd "$ROOT/backend"
if [ "$MODE" = "live" ]; then
  exec "$PY" -m adsb_display "${LIVE_ARGS[@]}"
else
  exec "$PY" -m adsb_display "${REPLAY_ARGS[@]}"
fi
