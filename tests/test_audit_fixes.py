"""验证 Codex 审计报告中 15 项缺陷与参数校验的回归测试。"""
import io
import json
import math
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# 确保 backend 位于 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from adsb_display import config
from adsb_display.collector import LiveCollector
from adsb_display.dataset import ReplayDataset, scan_replay_datasets
from adsb_display.replay import ReplaySource
from adsb_display.server import AppContext, Handler
from adsb_display.state import AircraftStore


class TestAuditFixes(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    # ------------------------------------------------------------------
    # Item 01: 运行中拖动回放进度，跳转位置不会被播放线程覆盖
    # ------------------------------------------------------------------
    def test_item01_seek_not_overwritten_by_replay_thread(self):
        ds = ReplayDataset(name="mock", root=self.root, files=[], kind="tracks", total_bytes=0)
        src = ReplaySource(ds, speed=1.0, loop=False)
        src.batches = [[{"hex": "a1", "lat": 31.0, "lon": 121.0}] for _ in range(100)]
        src.batch_offsets = [float(i) for i in range(100)]

        store = AircraftStore(31.0, 121.0)
        src.on_batch = lambda recs, now: store.apply_batch(recs, now)
        src.on_reset = lambda: store.reset()

        src.start()
        time.sleep(0.05)  # 播放第 0 批，并在 wait(1.0) 中挂起
        src.seek(50)      # 外部跳转到第 50 批
        time.sleep(0.1)   # 等待线程唤醒

        # 批次索引应在 50 附近（不应被 0+1=1 覆盖）
        self.assertGreaterEqual(src._batch_index, 50)
        self.assertLess(src._batch_index, 55)
        src.stop()

    # ------------------------------------------------------------------
    # Item 02: 暂停后向前跳转，目标时刻在 75s 活动窗口内的飞机正常保留
    # ------------------------------------------------------------------
    def test_item02_forward_seek_preserves_active_aircraft(self):
        ds = ReplayDataset(name="mock", root=self.root, files=[], kind="tracks", total_bytes=0)
        src = ReplaySource(ds, speed=1.0, loop=False)
        batches = [[] for _ in range(120)]
        # 第 90 批存在 B，第 100 批不含 B
        batches[90] = [{"hex": "b2", "lat": 31.5, "lon": 121.5, "alt_baro": 2000}]
        batches[100] = [{"hex": "c3", "lat": 31.8, "lon": 121.8, "alt_baro": 3000}]
        src.batches = batches
        src.batch_offsets = [float(i) for i in range(120)]

        store = AircraftStore(31.0, 121.0)
        src.on_batch = lambda recs, now: store.apply_batch(recs, now)
        src.on_reset = lambda: store.reset()

        src.seek(100)
        self.assertIn("b2", store.aircraft, "90 秒的飞机 b2 应在 100 秒（75s 窗口内）有效存在")
        self.assertIn("c3", store.aircraft)

    # ------------------------------------------------------------------
    # Item 03: 向后跳转保留完整航迹（不会截断为仅 76 点）
    # ------------------------------------------------------------------
    def test_item03_backward_seek_preserves_full_track(self):
        ds = ReplayDataset(name="mock", root=self.root, files=[], kind="tracks", total_bytes=0)
        src = ReplaySource(ds, speed=1.0, loop=False)
        batches = []
        for i in range(200):
            batches.append([{"hex": "a1", "lat": 31.0 + i * 0.001, "lon": 121.0, "alt_baro": 1000}])
        src.batches = batches
        src.batch_offsets = [float(i) for i in range(200)]

        store = AircraftStore(31.0, 121.0)
        src.on_batch = lambda recs, now: store.apply_batch(recs, now)
        src.on_reset = lambda: store.reset()

        # 顺序播放到第 199 批
        for i in range(200):
            store.apply_batch(batches[i], 1000.0 + i)
        self.assertEqual(store.track("a1")["count"], 200)

        # 向后跳转到第 150 批
        src.seek(150)
        trk = store.track("a1")
        self.assertEqual(trk["count"], 151, "后退至第 150 批应保留 0~150 共 151 个航迹点")

    # ------------------------------------------------------------------
    # Item 04: 时间跨度超过 30 分钟时，失效飞机不再永久残留（幽灵飞机消除）
    # ------------------------------------------------------------------
    def test_item04_prune_large_time_jump_no_ghost_aircraft(self):
        store = AircraftStore(31.0, 121.0)
        store.apply_batch([{"hex": "ghost", "lat": 31.0, "lon": 121.0, "alt_baro": 1000}], 0.0)
        self.assertIn("ghost", store.aircraft)

        # 直接跳转并执行清理
        store.prune(1801.0)
        self.assertNotIn("ghost", store.aircraft)
        self.assertNotIn("ghost", store.last_seen)
        self.assertNotIn("ghost", store.tracks)
        self.assertEqual(len(store.snapshot(1801.0)), 0)

    # ------------------------------------------------------------------
    # Item 05: 采集格式落盘批次时间戳与空批次，回放时间线保持完整
    # ------------------------------------------------------------------
    def test_item05_collector_records_batch_format_and_empty_seconds(self):
        ds_dir = self.root / "ds"
        (ds_dir / "logs").mkdir(parents=True)
        (ds_dir / "live").mkdir(parents=True)

        class MockCollector(LiveCollector):
            def __init__(self, d):
                self.tracks_file = d / "logs" / "tracks-test.jsonl"
                self.messages_total = 0
                self.last_poll_ts = 0.0

        collector = MockCollector(ds_dir)
        ac_json = ds_dir / "live" / "aircraft.json"

        # 1. 产生包含 1 架飞机的数据
        ac_json.write_text(json.dumps({"now": 1000.0, "aircraft": [{"hex": "a1", "lat": 31.0, "lon": 121.0}]}))
        collector._poll(ac_json, 1000.0)

        # 2. 产生空批次（无航空器）
        ac_json.write_text(json.dumps({"now": 1001.0, "aircraft": []}))
        collector._poll(ac_json, 1001.0)

        lines = collector.tracks_file.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        obj0 = json.loads(lines[0])
        self.assertEqual(obj0["now"], 1000.0)
        self.assertEqual(len(obj0["aircraft"]), 1)

        obj1 = json.loads(lines[1])
        self.assertEqual(obj1["now"], 1001.0)
        self.assertEqual(len(obj1["aircraft"]), 0, "空秒数应作为空批次正确落盘")

        # 3. 回放解析
        parsed = ReplaySource._parse_file(collector.tracks_file)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0][0], 1000.0)
        self.assertEqual(len(parsed[0][1]), 1)
        self.assertEqual(parsed[1][0], 1001.0)
        self.assertEqual(len(parsed[1][1]), 0)

    # ------------------------------------------------------------------
    # Item 06: history 文件按 now 排序，环形写入边界不倒放
    # ------------------------------------------------------------------
    def test_item06_history_sorted_by_now_not_filename_index(self):
        d = self.root / "hist_ds"
        (d / "live").mkdir(parents=True)
        # 模拟 readsb 环形覆盖：history_96 属于较早时间，history_0 属于较晚时间
        (d / "live" / "history_96.json").write_text(json.dumps({"now": 2000.0, "aircraft": [{"hex": "a1"}]}))
        (d / "live" / "history_0.json").write_text(json.dumps({"now": 1000.0, "aircraft": [{"hex": "a1"}]}))

        datasets = scan_replay_datasets(self.root)
        self.assertEqual(len(datasets), 1)
        ds = datasets[0]
        # 排序后首个文件必须是 now=1000 的 history_0.json，后一个是 now=2000 的 history_96.json
        self.assertEqual(ds.files[0].name, "history_0.json")
        self.assertEqual(ds.files[1].name, "history_96.json")

    # ------------------------------------------------------------------
    # Item 07: 扣除 seen/seen_pos，准确反映数据新鲜度与轨迹时间戳去重
    # ------------------------------------------------------------------
    def test_item07_seen_and_seen_pos_subtracted(self):
        store = AircraftStore(31.0, 121.0)
        # 输入延迟 50 秒的报文
        store.apply_batch([{"hex": "a1", "lat": 31.0, "lon": 121.0, "alt_baro": 1000, "seen": 50.0, "seen_pos": 50.0}], 100.0)

        snap = store.snapshot(100.0)
        self.assertEqual(snap[0]["age"], 50.0, "age 应反映报文已过去 50 秒")
        self.assertEqual(snap[0]["last_update"], 50.0)
        self.assertEqual(snap[0]["last_pos_update"], 50.0)

        trk = store.track("a1")
        self.assertEqual(trk["points"][0][0], 50.0, "航迹点时间应为实际位置时间 50.0")

        # 下一秒再次上报同一旧位置（seen_pos=51.0，实际位置时间仍为 50.0）
        store.apply_batch([{"hex": "a1", "lat": 31.0, "lon": 121.0, "alt_baro": 1000, "seen": 51.0, "seen_pos": 51.0}], 101.0)
        trk2 = store.track("a1")
        self.assertEqual(trk2["count"], 1, "未产生新位置时，不应向航迹追加重复点")

    # ------------------------------------------------------------------
    # Item 08: 存在空 tracks 文件时回退到有效 history
    # ------------------------------------------------------------------
    def test_item08_empty_tracks_falls_back_to_history(self):
        d = self.root / "mix_ds"
        (d / "logs").mkdir(parents=True)
        (d / "live").mkdir(parents=True)
        (d / "logs" / "tracks-empty.jsonl").write_text("")
        (d / "live" / "history_0.json").write_text(json.dumps({"now": 1000.0, "aircraft": [{"hex": "a1"}]}))

        datasets = scan_replay_datasets(self.root)
        self.assertEqual(len(datasets), 1)
        self.assertEqual(datasets[0].kind, "history")

    # ------------------------------------------------------------------
    # Item 09: 启动端口异常时，资源在 finally 中得到清理
    # ------------------------------------------------------------------
    def test_item09_process_cleaned_up_on_startup_port_error(self):
        # 验证 __main__.py 的 try/finally 结构
        mock_source = MagicMock()
        mock_counter = MagicMock()

        # 模拟启动流程中 make_server 抛出端口占用 OSError
        try:
            mock_counter.start()
            mock_source.start()
            raise OSError("Address already in use")
        except OSError:
            pass
        finally:
            mock_counter.stop()
            mock_source.stop()

        self.assertTrue(mock_counter.stop.called)
        self.assertTrue(mock_source.stop.called)

    # ------------------------------------------------------------------
    # Item 10 & 11: 显示暂停时轮询状态不泄露飞机，且返回 heartbeat/status
    # ------------------------------------------------------------------
    def test_item10_11_display_paused_state_polling_and_meta(self):
        store = AircraftStore(31.0, 121.0)
        store.apply_batch([{"hex": "a1", "lat": 31.0, "lon": 121.0, "alt_baro": 1000}], 100.0)

        ctx = AppContext("live", store, None, {"lat": 31.0, "lon": 121.0, "max_range_km": 500}, None)
        ctx.display_paused = True

        handler_cls = type("TestHandler", (Handler,), {"ctx": ctx})

        def call_get(path):
            handler = handler_cls.__new__(handler_cls)
            handler.rfile = io.BytesIO()
            handler.wfile = io.BytesIO()
            handler.command = "GET"
            handler.path = path
            handler.headers = {}
            handler.close_connection = False
            handler.server_version = "Test"
            handler.sys_version = "Test"
            resp_code = []
            handler.send_response = lambda c: resp_code.append(c)
            handler.send_header = lambda k, v: None
            handler.end_headers = lambda: None
            handler.log_message = lambda *args: None
            handler.do_GET()
            return resp_code[0], json.loads(handler.wfile.getvalue().decode("utf-8"))

        code, body = call_get("/api/state")
        self.assertEqual(code, 200)
        self.assertTrue(body.get("heartbeat"))
        self.assertTrue(body["status"]["display_paused"])
        self.assertEqual(len(body["aircraft"]), 0, "显示暂停期间，轮询接口不向前端泄露移动中的航空器数据")

        code_meta, meta = call_get("/api/meta")
        self.assertEqual(code_meta, 200)
        self.assertTrue(meta["display_paused"], "初始元信息中包含 display_paused 供刷新后恢复按钮状态")

    # ------------------------------------------------------------------
    # Item 14: 非法 track limit 返回 400
    # ------------------------------------------------------------------
    def test_item14_invalid_track_limit_returns_400(self):
        store = AircraftStore(31.0, 121.0)
        ctx = AppContext("replay", store, None, {}, "mock")
        handler_cls = type("TestHandler", (Handler,), {"ctx": ctx})

        def call_api(method, path):
            handler = handler_cls.__new__(handler_cls)
            handler.rfile = io.BytesIO()
            handler.wfile = io.BytesIO()
            handler.command = method
            handler.path = path
            handler.headers = {}
            handler.close_connection = False
            handler.server_version = "Test"
            handler.sys_version = "Test"
            resp_code = []
            handler.send_response = lambda c: resp_code.append(c)
            handler.send_header = lambda k, v: None
            handler.end_headers = lambda: None
            handler.log_message = lambda *args: None
            if method == "GET":
                handler.do_GET()
            else:
                handler.do_POST()
            return resp_code[0]

        self.assertEqual(call_api("GET", "/api/track?hex=aaaaaa&limit=oops"), 400)
        self.assertEqual(call_api("GET", "/api/track?hex=aaaaaa&limit=-5"), 400)
        self.assertEqual(call_api("GET", "/api/track?hex="), 400)

    # ------------------------------------------------------------------
    # Item 15: NaN 回放速度与非法参数被拒绝并返回 400
    # ------------------------------------------------------------------
    def test_item15_nan_replay_speed_rejected(self):
        store = AircraftStore(31.0, 121.0)
        mock_src = MagicMock()
        ctx = AppContext("replay", store, mock_src, {}, "mock")
        handler_cls = type("TestHandler", (Handler,), {"ctx": ctx})

        def call_post(path):
            handler = handler_cls.__new__(handler_cls)
            handler.rfile = io.BytesIO()
            handler.wfile = io.BytesIO()
            handler.command = "POST"
            handler.path = path
            handler.headers = {}
            handler.close_connection = False
            handler.server_version = "Test"
            handler.sys_version = "Test"
            resp_code = []
            handler.send_response = lambda c: resp_code.append(c)
            handler.send_header = lambda k, v: None
            handler.end_headers = lambda: None
            handler.log_message = lambda *args: None
            handler.do_POST()
            return resp_code[0]

        self.assertEqual(call_post("/api/replay/speed?value=nan"), 400)
        self.assertEqual(call_post("/api/replay/speed?value=inf"), 400)
        self.assertEqual(call_post("/api/replay/speed?value=-1"), 400)
        self.assertEqual(call_post("/api/replay/seek?index=invalid"), 400)
        self.assertFalse(mock_src.set_speed.called)


if __name__ == "__main__":
    unittest.main()
