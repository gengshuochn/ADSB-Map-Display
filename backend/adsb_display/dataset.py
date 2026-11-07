"""数据集管理：按 <YYMMDD>_<tag>_<NN> 规则自动命名，存放于 data/ 子目录。

数据集目录结构与参考项目 261028_test 保持一致：
    <name>/meta.json                元数据
    <name>/live/                    readsb 原始 JSON 输出（aircraft.json、history_*.json 等）
    <name>/logs/tracks-<TS>.jsonl   每秒航迹快照
    <name>/beast/                   readsb 原始报文(zstd 压缩)
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config


@dataclass
class DatasetPaths:
    name: str
    root: Path

    @property
    def live(self) -> Path:
        return self.root / "live"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def beast(self) -> Path:
        return self.root / "beast"

    def ensure(self) -> None:
        for d in (self.live, self.logs, self.beast):
            d.mkdir(parents=True, exist_ok=True)


def dataset_prefix(ts: float | None = None) -> str:
    """日期前缀，如 2026-10-29 -> 261029"""
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).strftime("%y%m%d")


def next_dataset_name(data_root: Path | None = None, prefix: str | None = None) -> str:
    """生成下一个可用数据集名：<prefix>_display_<NN>，NN 从 01 开始递增。"""
    data_root = data_root or config.DATA_DIR
    prefix = prefix or dataset_prefix()
    pat = re.compile(rf"^{re.escape(prefix)}_{config.DATASET_TAG}_(\d{{2,}})$")
    max_n = 0
    if data_root.is_dir():
        for p in data_root.iterdir():
            m = pat.match(p.name)
            if m and p.is_dir():
                max_n = max(max_n, int(m.group(1)))
    return f"{prefix}_{config.DATASET_TAG}_{max_n + 1:02d}"


def create_dataset(
    receiver: dict,
    data_root: Path | None = None,
    name: str | None = None,
    note: str = "",
) -> DatasetPaths:
    """创建新数据集目录并写入 meta.json。"""
    config.ensure_dirs()
    data_root = data_root or config.DATA_DIR
    if name is None:
        name = next_dataset_name(data_root)
    ds = DatasetPaths(name=name, root=data_root / name)
    ds.ensure()
    meta = {
        "name": name,
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "readsb/rtlsdr",
        "receiver": receiver,
        "note": note,
    }
    (ds.root / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return ds


def write_meta_update(ds: DatasetPaths, **fields) -> None:
    meta_path = ds.root / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    meta.update(fields)
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 回放数据集扫描
# ---------------------------------------------------------------------------

@dataclass
class ReplayDataset:
    name: str
    root: Path
    files: list[Path]          # 有序回放文件（tracks jsonl 优先）
    kind: str                  # "tracks" | "history"
    total_bytes: int

    meta: dict = field(default_factory=dict)

    @property
    def sort_key(self):
        return (self.meta.get("created_utc") or self.meta.get("created_local") or self.name, self.name)

    @property
    def size_mb(self) -> float:
        return self.total_bytes / 1e6


_TRACKS_RE = re.compile(r"^tracks-.*\.jsonl$")
_HIST_NUM_RE = re.compile(r"history_(\d+)\.json$")


def _history_file_now(p: Path) -> float:
    """读取 history 快照内的绝对时间戳（支持 readsb 环形缓冲区跨界排序）。"""
    try:
        doc = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        if isinstance(doc, dict) and "now" in doc:
            return float(doc["now"])
    except Exception:
        pass
    m = _HIST_NUM_RE.search(p.name)
    return float(m.group(1)) if m else 0.0


def _has_track_records(p: Path) -> bool:
    """检查 tracks 文件中是否包含可解析的有效航空器记录。"""
    try:
        if p.stat().st_size == 0:
            return False
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if '"hex"' in line:
                    return True
                if '"aircraft"' in line and '"aircraft":[]' not in line:
                    return True
    except Exception:
        pass
    return False


def _has_history_records(p: Path) -> bool:
    """检查 history 文件中是否包含有效航空器记录。"""
    try:
        if p.stat().st_size == 0:
            return False
        doc = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        if isinstance(doc, dict):
            ac = doc.get("aircraft")
            if isinstance(ac, list) and len(ac) > 0:
                return True
    except Exception:
        pass
    return False


def inspect_dataset_dir(d: Path) -> ReplayDataset | None:
    """检查并构造单个指定目录的数据集对象。若有效则返回 ReplayDataset，否则返回 None。"""
    if not d.is_dir():
        return None
    tracks_candidates = sorted((d / "logs").glob("tracks-*.jsonl")) if (d / "logs").is_dir() else []
    history_candidates = sorted((d / "live").glob("history_*.json"), key=_history_file_now) if (d / "live").is_dir() else []

    valid_tracks = [f for f in tracks_candidates if _has_track_records(f)]
    has_history = any(_has_history_records(f) for f in history_candidates)

    if valid_tracks:
        files = valid_tracks
        kind = "tracks"
    elif has_history:
        # history 文件按快照 now 排序，滤除 0 字节空文件
        files = [f for f in history_candidates if f.stat().st_size > 0]
        kind = "history"
    else:
        return None

    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, ValueError):
        meta = {}

    return ReplayDataset(
        name=d.name,
        meta=meta,
        root=d,
        files=files,
        kind=kind,
        total_bytes=sum(f.stat().st_size for f in files),
    )


def scan_replay_datasets(data_root: Path | None = None) -> list[ReplayDataset]:
    """扫描 data/ 下可回放的数据集目录。"""
    data_root = data_root or config.DATA_DIR
    out: list[ReplayDataset] = []
    if not data_root.is_dir():
        return sorted(out, key=lambda ds: ds.sort_key)
    for d in sorted(data_root.iterdir()):
        ds = inspect_dataset_dir(d)
        if ds is not None:
            out.append(ds)
    return sorted(out, key=lambda ds: ds.sort_key)
