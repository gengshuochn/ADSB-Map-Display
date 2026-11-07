#!/usr/bin/env bash
# ADS-B 桌面端用户级菜单项安装与更新脚本
#
# 特性:
# - 仅写入当前用户目录 (~/.local/share/applications 与 ~/.local/share/icons/hicolor)
# - 图标源自桌面子项目 resources/ 目录，分发多尺寸高清 PNG
# - 统一 adsb-desktop 桌面标识与 Wayland app_id 关联
# - 幂等设计：重复执行即可安全同步最新路径和图标
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_HOME/applications"
ICONS_DIR="$DATA_HOME/icons/hicolor"
DESKTOP_FILE="$APP_DIR/adsb-desktop.desktop"
EXEC_BIN="$SCRIPT_DIR/run_desktop.sh"
ICON_PATH="$SCRIPT_DIR/resources/icon.png"

mkdir -p "$APP_DIR"

# 1. 安装多尺寸图标至用户级 hicolor 图标主题目录（保障 GNOME Dock、Alt+Tab 与活动概览在高分屏下高保真渲染）
for s in 16 32 48 64 128 256 512; do
    target_icon_dir="$ICONS_DIR/${s}x${s}/apps"
    mkdir -p "$target_icon_dir"
    src_icon="$SCRIPT_DIR/resources/icon-${s}.png"
    if [ "$s" -eq 512 ] || [ ! -f "$src_icon" ]; then
        src_icon="$SCRIPT_DIR/resources/icon.png"
    fi
    if [ -f "$src_icon" ]; then
        cp -f "$src_icon" "$target_icon_dir/adsb-desktop.png"
    fi
done

# 2. 写入 .desktop 规范入口
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=ADS-B 航迹地图
GenericName=ADS-B Flight Tracker
Comment=基于 RTL-SDR 与 readsb 的实时 ADS-B 航空器航迹地图桌面端
Exec="${EXEC_BIN}" %u
Icon=adsb-desktop
Terminal=false
Categories=Utility;Science;Geoscience;
StartupNotify=true
StartupWMClass=adsb-desktop
EOF

chmod +x "$DESKTOP_FILE"

# 3. 刷新图标与桌面入口缓存
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$ICONS_DIR" >/dev/null 2>&1 || true
elif command -v gtk4-update-icon-cache >/dev/null 2>&1; then
    gtk4-update-icon-cache -f -t "$ICONS_DIR" >/dev/null 2>&1 || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "=============================================="
echo " ADS-B 桌面应用菜单入口与图标安装成功！"
echo " 菜单文件: $DESKTOP_FILE"
echo " 启动目标: $EXEC_BIN"
echo " 图标主题: $ICONS_DIR/*/apps/adsb-desktop.png"
echo " 提示: 现在可在系统应用启动器中搜索 'ADS-B 航迹地图' 启动。"
echo "=============================================="
