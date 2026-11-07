# ADS-B 实时地图桌面端 (PySide6 + Qt WebEngine)

本项目为 ADS-B 实时航迹地图显示系统的原生桌面端扩展，面向 Ubuntu / GNOME / Wayland 环境。基于 **PySide6 6.12.0** 与 **Qt WebEngine** 构建，深度复用既有 Python 后端、地图前端及 readsb 硬件链路。

---

## 目录与功能架构

桌面端全部新增功能集中在 `desktop/` 子项目中，原项目结构及现有浏览器启动链路完全保持原样：

```
desktop/
├── .gitignore              # 忽略独立虚拟环境、运行时缓存、本地日志
├── requirements.in         # 依赖声明 (PySide6==6.12.0)
├── requirements.lock       # 包含精确版本与 sha256 哈希锁定的依赖文件
├── setup_env.sh            # 独立虚拟环境初始化与依赖安装脚本
├── run_desktop.sh          # 桌面端日常启动入口脚本
├── install_menu.sh         # 用户级桌面快捷方式安装/更新脚本
├── uninstall_menu.sh       # 用户级桌面快捷方式卸载脚本
├── README.md               # 桌面端架构与使用说明
├── resources/              # 桌面图标资源 (SVG 矢量与多尺寸 PNG)
│   ├── icon.svg
│   ├── icon.png (256x256)
│   ├── icon-128.png
│   ├── icon-64.png
│   └── icon-48.png
├── src/adsb_desktop/       # 桌面端应用核心源码
│   ├── __init__.py
│   ├── __main__.py         # 命令行参数解析与模式判定
│   ├── app.py              # 桌面应用生命周期调度器
│   ├── backend_process.py  # 后端子进程生命周期、就绪协议与进程组管理
│   ├── constants.py        # 颜色、路径、端口及超时常量
│   ├── single_instance.py  # 单实例锁 (QLockFile) 与窗口唤起 (QLocalServer)
│   ├── status_poller.py    # 异步非阻塞状态轮询器 (/api/meta)
│   ├── tray.py             # 系统托盘图标、菜单及颜色指示
│   └── window.py           # 主窗口、加载界面、持久化配置及异常窗口
└── tests/                  # 桌面端单元与集成测试 (61 项测试用例)
    ├── __init__.py
    ├── run_visual_verification.py  # Wayland 真实会话端到端截图验收脚本
    ├── take_screenshot.py          # GNOME 截图工具
    ├── test_backend_protocol.py    # 就绪协议与 JSON 通信测试
    ├── test_cli_args.py            # 命令行参数解析测试
    ├── test_idle_app_flow.py       # IDLE 待机态与引导流程测试
    ├── test_menu_scripts.py        # 菜单项安装/卸载脚本测试
    ├── test_process_manager.py     # 进程管理器核心测试
    ├── test_review_fixes.py        # 审核修复与故障注入综合回归测试
    ├── test_single_instance.py     # 单实例互斥与套接字唤起测试
    ├── test_theme_and_icon.py      # 主题同步与图标测试
    └── test_tray_and_window.py     # 托盘与主窗口测试
```

---

## 快速上手

### 1. 环境初始化

使用当前系统的 Python 3.14 与本机 `uv` 创建独立虚拟环境 `desktop/.venv`，并安装锁定依赖（包含完整 hash 校验）：

```bash
./desktop/setup_env.sh
```

日常启动时只校验环境完整性，不会重复升级或下载依赖。

### 2. 日常启动

可从任意工作目录调用，完全支持包含空格的项目路径：

```bash
# 自动检测硬件：连接 RTL-SDR blog V4 则实时采集，否则自动回放
./desktop/run_desktop.sh

# 强制实时模式并指定端口
./desktop/run_desktop.sh --mode live --port 18080

# 回放指定数据集
./desktop/run_desktop.sh --mode replay --dataset 261028_test_reference

# 定时运行 300 秒后自动退出
./desktop/run_desktop.sh --duration 300
```

#### 支持的参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--mode auto\|live\|replay` | 运行模式；auto 根据 lsusb 自动检测设备 | `auto` |
| `--dataset NAME` | 指定回放数据集名称（留空按数据量最大自动选取） | 自动选取 |
| `--duration SECONDS` | 运行秒数，0 为持续运行 | `0` |
| `--port PORT` | 桌面 Web 服务端口（优先级：命令行 > `ADS_DESKTOP_PORT` > 18080） | `18080` |
| `--lat`、`--lon`、`--gain` | 显式接收机坐标与增益（优先级高于环境变量 `ADS_*`） | 沿用后端默认 |

### 3. 应用菜单入口安装与卸载

桌面端提供了幂等的用户级菜单脚本：

- **安装或更新菜单项**：
  ```bash
  ./desktop/install_menu.sh
  ```
  将在 `~/.local/share/applications/adsb-desktop.desktop` 写入绝对路径启动项。安装后即可在 GNOME 应用中心或搜索栏输入 `ADS-B 航迹地图` 启动。如果项目目录移动，重新执行该脚本即可同步新路径。

- **卸载菜单项**：
  ```bash
  ./desktop/uninstall_menu.sh
  ```
  仅移除菜单启动文件，不会删除已采集数据或虚拟环境。

---

## 核心特性与行为说明

### 1. 独立窗口与持久化配置
- **渲染引擎**：`QMainWindow + QWebEngineView` 直接加载本实例启动的后端服务；
- **主题全栈无缝联动**：自绘标题栏（Logo、控制按钮）、外层窗口背景与边框实时监听网页内部 `#btn-theme` 按钮，深色（Dark，暖石墨 `#1B1C1E` / 铬黄 `#242528`）与浅色（Light，暖灰 `#F4F5F7` / `#EBECEF`）支持毫秒级无缝双向同步，跨重启从持久化配置中恢复；
- **配置持久化**：使用专用的 `QWebEngineProfile`，将 Cookie、Local Storage、Session 及缓存独立保存在 `desktop/runtime/web_profile/`，保证地图图层、侧栏折叠、主题切换以及 UI 缩放比例跨启动持久保留；
- **窗口几何尺寸记忆**：首次默认尺寸为 1440×900（最小 1024×768），关闭时保存窗口尺寸、位置和最大化状态至 `desktop/runtime/window_state.json`；下次启动恢复时自动校验有效屏幕区域，避免因多屏变动掉出屏幕外。

### 2. 系统托盘与状态表达
- **状态指示色**：
  - **黄色** (`#EAB308`)：正在启动、硬件检测与预热阶段（`starting`）；
  - **绿色** (`#22C55E`)：实时采集运行正常（`live`）；
  - **橙色** (`#F97316`)：当前显示历史航迹回放（`replay`）；如同时存在后台实时采集，菜单状态注明“后台采集中”；
  - **蓝色** (`#3B82F6`)：系统待机／未播放（`idle`，无硬件且无回放源，或等待用户操作）；
  - **红色** (`#EF4444`)：硬件未在位、端口冲突或后端异常（`error`），并显示简要原因；
  - **灰色** (`#9CA3AF`)：正在退出（`exiting`）。
- **只读轻量轮询**：每 2 秒异步 GET `/api/meta`，不抓取全量航迹流，完全不阻塞主界面。0 架飞机不视为异常；界面暂停也不代表后台采集停止。
- **托盘菜单**：
  - 显示／隐藏地图窗口；
  - 当前模式及后台采集状态（只读）；
  - 打开数据目录 (`data/`)；
  - 打开桌面日志目录 (`desktop/logs/`)；
  - 退出应用（若仍在实时采集，弹出对话框确认）。

### 3. 关闭与退出行为
- **点击窗口关闭按钮 (X)**：
  - 托盘可用时，窗口隐藏，后端与 readsb 采集在后台继续进行；首次隐藏时弹出通知提示；
  - 托盘不可用时，关闭按钮直接走正常退出流程，杜绝后台不可见孤儿进程。
- **多途径统一退出与代次隔离清理**：
  - 触发来源：托盘点击退出、主窗口退出按钮、页面内“终止系统”、定时时长到达或系统终止信号；
  - 退出序列：首先异步调用 `/api/system/shutdown`（带 `session_id` 隔离，超时 30 秒）；若超时向所属进程组发送 SIGTERM，再等待 5 秒；最后才发送 SIGKILL；
  - 代次隔离：后端管理器为每次会话分配递增代次（Generation），定时器与回调严格绑定所属代次与目标 PGID，旧会话定时器绝不影响新拉起的会话；
  - 严格消亡核查：看门狗与强杀兜底机制在未确认主进程及所属进程组彻底消亡前，保持等待重试，绝不提前终止 Qt 事件循环或在旧进程残留时启动新后端；
  - 绝不使用 `killall` 或 `pkill readsb`，仅清理属于本桌面实例的子进程组，绝不影响并行运行的浏览器版本或其他服务。

### 4. 单实例互斥与重复启动唤起
- 使用 `QLockFile + QLocalServer`，根据规范化项目绝对路径与当前 UID 生成实例标识；
- 同一用户在同一项目下只允许一个桌面实例运行；
- 重复启动时，新进程自动向已有实例的本地套接字发送唤起指令并立即退出，主实例窗口置顶恢复，不拉起重复后端，亦不篡改已有会话参数。

### 5. 待机引导、外部数据集与重试
- **待机引导页面**：无活动硬件且无回放数据集时，窗口展示蓝色待机引导页面，提供“重新检测并启动”、“选择外部数据集”与“打开数据目录”三大快捷入口；
- **外部数据集加载与校验**：支持通过桌面目录选择器挂载任意外部路径的航迹目录（自动校验 `logs/tracks-*.jsonl` 或 `live/history_*.json` 有效性），杜绝同名冲突；
- **硬件动态再探测**：保持 `auto` 探测模式，点击“重新检测”时动态刷新 RTL-SDR 硬件状态，支持热插拔后一键上线；
- **异常明确呈现**：端口冲突、非法数据集、后端启动超时等异常发生时，界面展示明确的错误信息与日志路径，并提供“重新尝试”和“退出应用”按钮。

---

## 故障定位与日志

- 桌面端日志：`desktop/logs/desktop-*.log`
- 后端服务日志：`logs/server-*.log`
- readsb 原始采集日志：`data/<数据集名称>/logs/readsb-*.log`
- 运行时锁与状态：`desktop/runtime/`
