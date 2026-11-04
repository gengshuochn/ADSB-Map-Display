"""命令行入口。

用法:
  python -m adsb_display serve --mode live [--port 8080] [--duration 秒]
  python -m adsb_display serve --mode replay [--dataset 名称] [--speed 1] [--no-loop]
  python -m adsb_display list-datasets
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from . import config
from .dataset import create_dataset, scan_replay_datasets, write_meta_update
from .state import AircraftStore
from .server import AppContext, make_server
from .collector import LiveCollector
from .replay import ReplaySource
from .warmup import Warmup
from .msgstats import Df17Counter


def setup_logging() -> None:
    config.ensure_dirs()
    ts = time.strftime("%Y%m%d-%H%M%S")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_DIR / f"server-{ts}.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpserver").setLevel(logging.WARNING)


def pick_replay_dataset(name: str | None):
    cands = scan_replay_datasets()
    if not cands:
        return None
    if name:
        clean_name = name.strip("/ ").split("/")[-1]
        for c in cands:
            if c.name == clean_name:
                return c
        raise SystemExit(f"未找到可回放数据集: {name}（可用: {[c.name for c in cands]}）")
    # 默认选字节量最大的（数据最丰富）
    return max(cands, key=lambda c: c.total_bytes)


def _check_rtl_hardware() -> tuple[bool, str]:
    """检查 RTL-SDR 是否在位（lsusb 方式，不独占设备）。"""
    import shutil
    import subprocess
    if not shutil.which("lsusb"):
        return False, "lsusb 不可用，无法检测硬件"
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5).stdout.lower()
    except Exception as e:  # noqa: BLE001
        return False, f"lsusb 执行失败: {e}"
    if "0bda:2838" in out or "0bda:2832" in out:
        return True, "检测到 RTL-SDR (RTL2838/2832U)"
    return False, "未检测到 RTL-SDR 设备 (0bda:2838/2832)"


def cmd_serve(args) -> int:
    setup_logging()
    log = logging.getLogger("adsb.main")

    receiver = {
        "lat": args.lat if args.lat is not None else config.DEFAULT_RECEIVER_LAT,
        "lon": args.lon if args.lon is not None else config.DEFAULT_RECEIVER_LON,
        "gain": args.gain,
        "max_range_km": config.DEFAULT_MAX_RANGE_KM,
        "source": "RTL-SDR Blog V4 + readsb",
    }

    store = AircraftStore(receiver["lat"], receiver["lon"])
    source = None
    dataset_name = None
    warmup: Warmup | None = None
    df17_counter: Df17Counter | None = None
    shutdown_event = threading.Event()

    def _on_batch(recs, now):
        store.apply_batch(recs, now)
        store.prune(now)

    if args.mode == "live":
        warmup = Warmup()
        # ---- 阶段1: 检查物理硬件（真实 lsusb 检测）----
        ok, detail = _check_rtl_hardware()
        if not ok:
            warmup.set_error(detail)
            log.error("硬件检查失败: %s", detail)
        else:
            warmup.set("checking_hardware", detail)
            # ---- 阶段2: 初始化（创建数据集 + 拉起 readsb）----
            warmup.set("initializing", "创建数据集…")
            ds = create_dataset(receiver, note=f"run_display {args.duration or '∞'}s")
            dataset_name = ds.name
            log.info("创建数据集: data/%s", ds.name)
            write_meta_update(ds, started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              duration_s=args.duration or None)
            warmup.set("initializing", f"数据集 data/{ds.name} 就绪，启动 readsb…")
            src = LiveCollector(ds, receiver["lat"], receiver["lon"], gain=args.gain)

            def _live_batch(recs, now):
                # 显示暂停时: 落盘与状态库照常, 但不对外可见（snapshot 由 SSE 侧拦截）
                _on_batch(recs, now)
                # 预热推进: 报文接收中 → 首个带位置航空器即就绪
                if warmup.phase == "collecting":
                    msgs = df17_counter.messages_total if df17_counter else 0
                    warmup.set("collecting", f"正在收集数据… 累计报文 {msgs}")
                    if recs:
                        warmup.set("ready", f"已解算 {len(recs)} 架航空器位置")
                    elif st_error := (src.status().get("last_error")):
                        warmup.set_error(str(st_error))
                # 就绪阶段无需再推进，detail 保留"已解算 N 架"作为摘要

            # DF17 报文统计：直连 readsb raw 端口独立校验 CRC，仅计 DF17 且校验通过
            df17_counter = Df17Counter(config.READSB_RAW_HOST, config.READSB_RAW_PORT)
            src.on_batch = _live_batch
    else:
        ds = pick_replay_dataset(args.dataset)
        if ds is None:
            raise SystemExit("data/ 下没有可回放数据集；请先运行 live 模式采集一次。")
        dataset_name = ds.name
        src = ReplaySource(ds, speed=args.speed, loop=not args.no_loop)
        src.on_batch = _on_batch
        src.on_reset = lambda: store.reset()
        if src.load() == 0:
            raise SystemExit(f"数据集 {ds.name} 无法解析出回放批次。")
        log.info("回放数据集: data/%s", ds.name)

    httpd = None
    http_thread = None
    source = None
    try:
        if args.mode == "live":
            df17_counter.start()
            src.start()
            source = src
            warmup.set("collecting", "正在收集数据… 等待报文")
        else:
            src.start()
            source = src

        ctx = AppContext(
            mode=args.mode, store=store, source=source, receiver=receiver,
            dataset_name=dataset_name, warmup=warmup, df17_counter=df17_counter,
            shutdown_cb=shutdown_event.set,
        )
        httpd = make_server(ctx, args.port)
        log.info("服务已启动: http://127.0.0.1:%d  (mode=%s dataset=%s)", args.port, args.mode, dataset_name)

        def _sig(_sig, _frm):
            shutdown_event.set()

        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        http_thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        http_thread.start()

        deadline = time.time() + args.duration if args.duration else None
        # 主循环: 响应前端终止请求 / 信号 / 定时时长
        while not shutdown_event.is_set():
            time.sleep(0.5)
            if deadline and time.time() >= deadline:
                log.info("到达设定采集时长 %s 秒，退出", args.duration)
                break
    finally:
        log.info("正在停止服务…")
        if httpd is not None:
            httpd.shutdown()
        if http_thread is not None:
            http_thread.join(timeout=3)
        if df17_counter is not None:
            df17_counter.stop()
        if source is not None:
            source.stop()
        log.info("已退出")
    return 0


def cmd_list(_args) -> int:
    for d in scan_replay_datasets():
        print(f"{d.name}\t{d.kind}\t{d.size_mb:.2f}MB\tfiles={[f.name for f in d.files]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adsb_display", description="ADS-B 实时地图显示后端")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="启动采集/回放 + Web 服务")
    sp.add_argument("--mode", choices=["live", "replay"], default="live")
    sp.add_argument("--port", type=int, default=config.HTTP_PORT)
    sp.add_argument("--duration", type=int, default=0, help="运行秒数，0=持续运行")
    sp.add_argument("--lat", type=float, default=None)
    sp.add_argument("--lon", type=float, default=None)
    sp.add_argument("--gain", type=float, default=config.DEFAULT_GAIN)
    sp.add_argument("--dataset", type=str, default=None, help="回放数据集名")
    sp.add_argument("--speed", type=float, default=1.0)
    sp.add_argument("--no-loop", action="store_true")

    sub.add_parser("list-datasets", help="列出可回放数据集")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "list-datasets":
        return cmd_list(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
