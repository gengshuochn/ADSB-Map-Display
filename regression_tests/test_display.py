"""显示源隔离及多段回放回归；无需硬件或第三方依赖。"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from adsb_display import config
from adsb_display.dataset import scan_replay_datasets
from adsb_display.replay import ReplaySource
from adsb_display.server import AppContext, make_server
from adsb_display.state import AircraftStore


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(config, 'DATA_DIR', self.root)
        self.patch.start()
        self.receiver = {'lat': 31, 'lon': 118, 'gain': 40, 'source': 'test'}
        self.collector = Mock()
        self.collector.status.return_value = {'running': True}
        self.counter = Mock()
        self.counter.status.return_value = {'messages_df17': 100}
        self.ctx = AppContext('live', AircraftStore(31, 118), self.collector,
                              self.receiver, 'collecting', df17_counter=self.counter)
        self.dataset('first', 1000, 'a1', 31)
        self.dataset('second', 100000, 'b2', 40)
        self.dataset('collecting', 200000, 'c3', 31)

    def tearDown(self):
        self.ctx.close()
        self.patch.stop()
        self.tmp.cleanup()

    def dataset(self, name, timestamp, hx, lat):
        root = self.root / name
        (root / 'logs').mkdir(parents=True)
        (root / 'meta.json').write_text(json.dumps({
            'created_utc': f'2026-11-0{1 if name == "first" else 2}T00:00:00Z',
            'created_local': '2026-11-01 08:00:00', 'receiver': {'lat': lat, 'lon': 118}}))
        (root / 'logs' / 'tracks-001.jsonl').write_text('\n'.join(json.dumps({
            'now': timestamp+i, 'aircraft': [{'hex': hx, 'lat': lat+i/1000, 'lon': 118}]
        }) for i in range(4)))

    def test_live_keeps_updating_and_track_routes(self):
        self.ctx.switch_replay(['first'])
        self.ctx.replay_source.pause()
        for i in range(3):
            self.ctx.store_live.apply_batch([{'hex': 'a1', 'lat': 32+i/1000, 'lon': 118}], time.time()+i)
        self.assertEqual(self.ctx.track('a1')['count'], 1)
        self.assertEqual(self.ctx.status()['msgstats']['messages_df17'], 100)
        self.ctx.switch_live()
        self.assertEqual(self.ctx.track('a1')['count'], 3)
        self.collector.stop.assert_not_called()
        self.assertFalse(self.ctx.display_paused)

    def test_failed_load_preserves_source_and_pause(self):
        self.ctx.switch_replay(['first'])
        old = self.ctx.replay_source
        old.pause()
        with patch.object(ReplaySource, 'load', side_effect=ValueError('broken')):
            with self.assertRaises(ValueError):
                self.ctx.switch_replay(['second'])
        self.assertIs(old, self.ctx.replay_source)
        self.assertTrue(old.status()['paused'])

    def test_bad_selection_and_collecting_rejected(self):
        for names in ([], ['collecting'], ['../first'], ['absent'], ['first', 'first'], 'first', [1]):
            with self.assertRaises(ValueError):
                self.ctx.switch_replay(names)
        self.assertTrue(next(d for d in self.ctx.datasets() if d['name'] == 'collecting')['collecting'])

    def test_segment_seek_receiver_and_clock(self):
        self.ctx.switch_replay(['second', 'first'])
        src = self.ctx.replay_source
        src.pause()
        self.assertEqual([d.name for d in src.datasets], ['first', 'second'])
        self.assertEqual(src.batch_offsets[4] - src.batch_offsets[3], 1)
        revision = self.ctx.display_revision
        src.seek(5)
        self.assertGreater(self.ctx.display_revision, revision)
        self.assertEqual(self.ctx.receiver['lat'], 40)
        self.assertEqual(self.ctx.clock_now(), 100001)
        self.assertEqual(self.ctx.track('a1')['count'], 0)
        self.assertEqual(self.ctx.track('b2')['count'], 2)
        self.assertEqual(self.ctx.snapshot()['status']['aircraft_count'], 1)
        src.seek(1)
        self.assertEqual(self.ctx.track('b2')['count'], 0)
        self.assertEqual(self.ctx.receiver['lat'], 31)

    def test_loop_resets_and_close_reaps_thread(self):
        self.ctx.switch_replay(['first', 'second'])
        src = self.ctx.replay_source
        src.pause()
        src.seek(7)
        revision = self.ctx.display_revision
        src.set_speed(60)
        src.resume()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.ctx.display_revision == revision:
            time.sleep(.005)
        self.assertGreater(self.ctx.display_revision, revision)
        self.ctx.close()
        self.assertFalse(src._thread.is_alive())
        self.collector.stop.assert_not_called()

    def test_pause_heartbeat_keeps_live_metrics(self):
        self.ctx.display_paused = True
        state = self.ctx.snapshot()
        self.assertTrue(state['heartbeat'])
        self.assertEqual(state['aircraft'], [])
        self.assertEqual(state['status']['msgstats']['messages_df17'], 100)

    def test_replay_only_cannot_start_hardware(self):
        ds = next(d for d in scan_replay_datasets() if d.name == 'first')
        src = ReplaySource(ds, loop=False)
        src.load()
        ctx = AppContext('replay', AircraftStore(31, 118), src, self.receiver, ds.name)
        try:
            with self.assertRaises(ValueError):
                ctx.switch_live()
            ctx.switch_replay(['second'])
            self.assertFalse(ctx.status()['live_available'])
            self.assertFalse(ctx.replay_source.loop)
        finally:
            ctx.close()

    def test_legacy_without_timestamps_and_metadata_fallback(self):
        file = self.root / 'first/logs/tracks-001.jsonl'
        file.write_text('\n'.join(json.dumps({'hex': 'a1', 'lat': 31+i*.01, 'lon': 118}) for i in range(3)))
        self.ctx.switch_replay(['first'])
        src = self.ctx.replay_source
        src.pause()
        self.assertEqual(src.batch_times[2] - src.batch_times[0], 2)
        self.assertEqual(self.ctx.receiver['gain'], 40)

    def test_empty_tracks_history_compatibility(self):
        file = self.root / 'first/logs/tracks-001.jsonl'
        file.write_text('')
        history = self.root / 'first/live'
        history.mkdir()
        (history / 'history_99.json').write_text(json.dumps({'now': 2000, 'aircraft': [{'hex': 'a1', 'lat': 31, 'lon': 118}]}))
        (history / 'history_0.json').write_text(json.dumps({'now': 1000, 'aircraft': [{'hex': 'a1', 'lat': 31, 'lon': 118}]}))
        self.ctx.switch_replay(['first'])
        self.ctx.replay_source.pause()
        self.assertEqual(self.ctx.replay_source.batch_times, [1000, 2000])

    def test_corrupt_records_do_not_replace_display(self):
        self.ctx.switch_replay(['first'])
        old = self.ctx.replay_source
        (self.root / 'second/logs/tracks-001.jsonl').write_text(json.dumps({'now': 100, 'aircraft': [{'hex': 'b2', 'lat': 'broken'}]}))
        with self.assertRaises(ValueError):
            self.ctx.switch_replay(['second'])
        self.assertIs(old, self.ctx.replay_source)

    def test_snapshot_does_not_observe_partial_seek(self):
        self.ctx.switch_replay(['first'])
        src = self.ctx.replay_source
        src.pause()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = src.on_batch
        def hold(records, now):
            entered.set()
            release.wait(2)
            original(records, now)
        src.on_batch = hold
        seeker = threading.Thread(target=src.seek, args=(2,))
        seeker.start()
        self.assertTrue(entered.wait(1))
        snapshots = []
        def read():
            snapshots.append(self.ctx.snapshot())
            finished.set()
        reader = threading.Thread(target=read)
        reader.start()
        self.assertFalse(finished.wait(.05))
        release.set()
        seeker.join(2)
        reader.join(2)
        self.assertEqual(snapshots[0]['status']['source']['batch_index'], 2)
        self.assertEqual(snapshots[0]['aircraft'][0]['lat'], 31.002)

    def test_http_switch_and_error_contract(self):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        server = make_server(self.ctx, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def post(path, body=None):
            req = Request(base+path, data=json.dumps(body).encode() if body is not None else b'', method='POST')
            return json.load(urlopen(req, timeout=3))
        try:
            st = post('/api/display/replay', {'datasets': ['first']})['status']
            self.assertEqual(st['display_mode'], 'replay')
            post('/api/replay/pause')
            trk = json.load(urlopen(base+'/api/track?hex=a1', timeout=3))
            self.assertEqual(trk['display_revision'], self.ctx.display_revision)
            post('/api/display/live')
            with self.assertRaises(HTTPError) as err:
                post('/api/replay/resume')
            self.assertEqual(err.exception.code, 400)
            err.exception.close()
            with self.assertRaises(HTTPError) as rejected:
                post('/api/display/replay', {'datasets': ['collecting']})
            rejected.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
