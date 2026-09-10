"""JSONL 回放（P1.2）。

每行一个 NormalizedEvent（bridge/events.NormalizedEvent.to_dict 格式），
可选 at_ms 字段：回放器把它同时当作虚拟单调时钟喂给 reducer
（reduce(event, monotonic_now=at_ms)）。缺省 at_ms 的行沿用上一个时间点。
"""

from __future__ import annotations

import json
from typing import Optional

from ..events import NormalizedEvent
from ..state.engine import StateEngine

DEFAULT_EPOCH = "replay-001"


def load_events(path: str) -> list:
    """读取 JSONL 文件；空行跳过；坏行抛 ValueError（带行号）。"""
    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            try:
                events.append(NormalizedEvent.from_dict(obj))
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc
    return events


def run_events(events, *, engine: Optional[StateEngine] = None,
               epoch: str = DEFAULT_EPOCH, utc_anchor_ms: int = 0) -> list:
    """把事件序列喂给 reducer，返回快照列表。确定性：相同输入 ⇒ 相同输出。"""
    if engine is None:
        engine = StateEngine(epoch, source_kind="mock", utc_anchor_ms=utc_anchor_ms)
    snapshots = []
    last_mono = 0
    for event in events:
        mono = event.at_ms if event.at_ms is not None else last_mono
        if mono < last_mono:
            mono = last_mono  # 虚拟单调时钟不回退
        last_mono = mono
        snapshots.append(engine.apply(event, mono))
    return snapshots


def run_file(path: str, **kwargs) -> list:
    return run_events(load_events(path), **kwargs)
