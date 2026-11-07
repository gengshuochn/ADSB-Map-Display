#!/usr/bin/env bash
# ADS-B 桌面端用户级菜单项卸载脚本
#
# 仅移除当前用户菜单入口及图标缓存，不影响项目源码、独立虚拟环境及已采集数据。
#
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_HOME/applications"
ICONS_DIR="$DATA_HOME/icons/hicolor"
DESKTOP_FILE="$APP_DIR/adsb-desktop.desktop"

# 1. 移除 .desktop 入口
if [ -f "$DESKTOP_FILE" ]; then
    rm -f "$DESKTOP_FILE"
    echo "已移除用户菜单项: $DESKTOP_FILE"
else
    echo "用户菜单项不存在: $DESKTOP_FILE"
fi

# 2. 移除已安装的 hicolor 图标
for s in 16 32 48 64 128 256 512; do
    icon_file="$ICONS_DIR/${s}x${s}/apps/adsb-desktop.png"
    if [ -f "$icon_file" ]; then
        rm -f "$icon_file"
    fi
done

# 3. 刷新缓存
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$ICONS_DIR" >/dev/null 2>&1 || true
elif command -v gtk4-update-icon-cache >/dev/null 2>&1; then
    gtk4-update-icon-cache -f -t "$ICONS_DIR" >/dev/null 2>&1 || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "卸载完成：已清理用户菜单和图标，项目代码与虚拟环境完好保留。"
