"""浏览器测试专用服务：模拟后台采集，所有数据写入临时目录。"""
import json
import signal
import tempfile
import threading
import time
from pathlib import Path

from adsb_display import config
from adsb_display.dataset import ReplayDataset
from adsb_display.server import AppContext, make_server
from adsb_display.state import AircraftStore


def main():
    stopped = threading.Event()
    receiver = {'lat': 31.93959, 'lon': 118.7858, 'gain': 40.2, 'max_range_km': 500, 'source': '模拟采集器'}
    with tempfile.TemporaryDirectory() as folder:
        config.DATA_DIR = root = Path(folder)
        for name, timestamp, lat, day in [('older', 1600000000, 31.93959, 1), ('newer', 1700000000, 40, 2), ('collecting', time.time(), 31.93959, 3)]:
            dsroot = root / name
            (dsroot / 'logs').mkdir(parents=True)
            (dsroot / 'meta.json').write_text(json.dumps({'created_local': f'2026-11-0{day} 08:00:00', 'receiver': dict(receiver, lat=lat)}))
            (dsroot / 'logs' / 'tracks-001.jsonl').write_text('\n'.join(json.dumps({'now': timestamp+i, 'aircraft': [
                {'hex': 'abc123', 'flight': 'TEST123', 'lat': lat+i*.0001, 'lon': 118.7858, 'alt_baro': 20000, 'gs': 230},
                {'hex': 'def456', 'flight': 'TEST456', 'lat': lat+.3, 'lon': 119, 'alt_baro': 10000}]}) for i in range(100)))
        store = AircraftStore(receiver['lat'], receiver['lon'])
        class Collector:
            count = 0
            def status(self):
                return {'running': not stopped.is_set(), 'messages': self.count, 'restarts': 0}
        class Counter:
            def status(self):
                return {'messages_df17': collector.count}
        collector = Collector()
        ctx = AppContext('live', store, collector, receiver, 'collecting', df17_counter=Counter())
        def collect():
            path = root / 'collecting' / 'logs' / 'tracks-001.jsonl'
            with path.open('a') as output:
                while not stopped.is_set():
                    collector.count += 1
                    now = time.time()
                    records = [{'hex': 'abc123', 'flight': 'LIVE123', 'lat': receiver['lat']+.01*collector.count, 'lon': receiver['lon'], 'alt_baro': 20000}]
                    store.apply_batch(records, now)
                    store.prune(now)
                    output.write(json.dumps({'now': now, 'aircraft': records})+'\n')
                    output.flush()
                    stopped.wait(.1)
        worker = threading.Thread(target=collect, daemon=True)
        worker.start()
        server = make_server(ctx, 0)
        http_thread = threading.Thread(target=server.serve_forever, daemon=True)
        http_thread.start()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        print(json.dumps({'port': server.server_port, 'root': str(root)}), flush=True)
        try:
            stopped.wait()
        finally:
            server.shutdown()
            ctx.close()
            worker.join()
            server.server_close()


if __name__ == '__main__':
    main()
