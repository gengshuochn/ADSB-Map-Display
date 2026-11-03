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
from dataclasses import dataclass
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

    @property
    def size_mb(self) -> float:
        return self.total_bytes / 1e6


_TRACKS_RE = re.compile(r"^tracks-.*\.jsonl$")


def scan_replay_datasets(data_root: Path | None = None) -> list[ReplayDataset]:
    """扫描 data/ 下可回放的数据集目录。"""
    data_root = data_root or config.DATA_DIR
    out: list[ReplayDataset] = []
    if not data_root.is_dir():
        return out
    for d in sorted(data_root.iterdir()):
        if not d.is_dir():
            continue
        tracks_files = sorted((d / "logs").glob("tracks-*.jsonl")) if (d / "logs").is_dir() else []
        history_files = sorted((d / "live").glob("history_*.json")) if (d / "live").is_dir() else []
        if tracks_files:
            files = tracks_files
            kind = "tracks"
        elif history_files:
            files = history_files
            kind = "history"
        else:
            continue
        out.append(
            ReplayDataset(
                name=d.name,
                root=d,
                files=files,
                kind=kind,
                total_bytes=sum(f.stat().st_size for f in files),
            )
        )
    return out
