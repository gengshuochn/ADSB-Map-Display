"""航空器状态库：维护最新状态、轨迹历史与老化逻辑（线程安全）。"""
from __future__ import annotations

import math
import threading
from collections import deque
from typing import Any

from . import config

# 需要透传给前端的 readsb 字段
_PASSTHROUGH = (
    "type", "alt_geom", "gs", "ias", "tas", "mach", "track", "track_rate",
    "mag_heading", "true_heading", "baro_rate", "geom_rate", "squawk",
    "category", "emergency", "nav_qnh", "nav_altitude_mcp", "nav_heading",
    "nic", "rc", "seen", "seen_pos", "rssi", "messages", "alert", "spi",
    "wd", "ws", "oat",
)


def _dist_bearing_km(lat1, lon1, lat2, lon2):
    """球面距离(km)与初始方位角(deg)。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * r * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return d, brg


class AircraftStore:
    """按 ICAO 24 位地址维护航空器最新状态与轨迹。"""

    def __init__(self, receiver_lat: float, receiver_lon: float):
        self._lock = threading.Lock()
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        # hex -> 最新（归一化后）记录
        self.aircraft: dict[str, dict[str, Any]] = {}
        # hex -> deque[(ts, lat, lon, alt_ft, gs, track)]
        self.tracks: dict[str, deque] = {}
        # hex -> 最后出现时间（用于轨迹清理）
        self.last_seen: dict[str, float] = {}
        self.update_count = 0

    # ------------------------------------------------------------------
    def apply_batch(self, records: list[dict], now: float) -> int:
        """合并一批航空器记录（同一秒的快照），返回本批条数。"""
        n = 0
        with self._lock:
            for rec in records:
                hx = rec.get("hex")
                if not hx:
                    continue
                hx = hx.lower()
                norm = self._normalize(rec, now)
                old = self.aircraft.get(hx)
                if old is not None:
                    # 保留首次出现时间等信息
                    norm["first_seen"] = old.get("first_seen", now)
                    # readsb 未更新时沿用旧位置字段会被新批覆盖，无需额外处理
                else:
                    norm["first_seen"] = now
                self.aircraft[hx] = norm
                self.last_seen[hx] = now
                if norm.get("lat") is not None and norm.get("lon") is not None:
                    trk = self.tracks.setdefault(hx, deque(maxlen=config.TRACK_MAX_POINTS))
                    pts = trk
                    if not pts or now - pts[-1][0] >= 0.4:  # 防重复时间戳
                        pts.append((now, norm["lat"], norm["lon"], norm.get("alt_ft"), norm.get("gs"), norm.get("track")))
                n += 1
            self.update_count += 1
        return n

    def _normalize(self, rec: dict, now: float) -> dict:
        hx = (rec.get("hex") or "").lower()
        alt_baro = rec.get("alt_baro")
        if alt_baro == "ground":
            alt_ft = 0.0
        elif isinstance(alt_baro, (int, float)):
            alt_ft = float(alt_baro)
        else:
            alt_ft = None
        lat, lon = rec.get("lat"), rec.get("lon")
        flight = (rec.get("flight") or "").strip() or None
        out = {"hex": hx, "flight": flight, "alt_baro": alt_baro, "alt_ft": alt_ft}
        for k in _PASSTHROUGH:
            v = rec.get(k)
            if v is not None:
                out[k] = v
        out["lat"] = lat
        out["lon"] = lon
        out["last_update"] = now
        if lat is not None and lon is not None:
            out["last_pos_update"] = now
            d, b = _dist_bearing_km(self.receiver_lat, self.receiver_lon, lat, lon)
            out["dist_km"] = round(d, 2)
            out["bearing"] = round(b, 1)
        else:
            old = self.aircraft.get(hx)
            if old:
                for k in ("lat", "lon", "last_pos_update", "dist_km", "bearing"):
                    if k in old:
                        out[k] = old[k]
        return out

    # ------------------------------------------------------------------
    def snapshot(self, now: float | None = None) -> list[dict]:
        """当前活动航空器列表（含老化标记 age）。"""
        now = now if now is not None else self._now()
        with self._lock:
            out = []
            for hx, rec in self.aircraft.items():
                r = dict(rec)
                r["age"] = round(max(0.0, now - self.last_seen.get(hx, now)), 1)
                out.append(r)
            return out

    def track(self, hx: str, limit: int | None = None) -> dict | None:
        hx = hx.lower()
        with self._lock:
            trk = self.tracks.get(hx)
            if not trk:
                return None
            pts = list(trk)
            if limit:
                pts = pts[-limit:]
            return {
                "hex": hx,
                "count": len(pts),
                "points": [
                    [round(t, 2), round(la, 6), round(lo, 6), alt, gs, tr]
                    for (t, la, lo, alt, gs, tr) in pts
                ],
            }

    def prune(self, now: float | None = None) -> None:
        now = now if now is not None else self._now()
        with self._lock:
            drop_a = [h for h, t in self.last_seen.items() if now - t > config.AIRCRAFT_DROP_S]
            for h in drop_a:
                self.aircraft.pop(h, None)
                self.last_seen.pop(h, None)
            drop_t = [h for h, t in self.last_seen.items() if now - t > config.TRACK_KEEP_S]
            for h in drop_t:
                self.tracks.pop(h, None)

    def _now(self) -> float:
        import time
        return time.time()

    def reset(self) -> None:
        """清空全部状态与轨迹（回放循环重开时使用）。"""
        with self._lock:
            self.aircraft.clear()
            self.last_seen.clear()
            self.tracks.clear()

    def aircraft_count(self) -> int:
        with self._lock:
            return len(self.aircraft)
