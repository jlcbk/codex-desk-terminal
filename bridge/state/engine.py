"""StateEngine：单 owner 串行应用事件并产出快照（INTERFACES §5 StateEngine 行）。

- apply(event, monotonic_now) -> AppState dict：先 reduce 再 render；
  每次实际产出快照 seq 严格递增（同 bridge_epoch），首份快照 seq=1。
- utc_anchor_ms：单调毫秒 → UTC 毫秒的固定锚点（构造时给定，不取墙钟），
  保证"相同事件序列 + 相同时钟序列 ⇒ 字节相同输出"。
- 网络输出与 reducer 分离：本类不做任何 IO/transport。
"""

from __future__ import annotations

from typing import Optional

from . import reducer, render as render_mod

SOURCE_MOCK = "mock"
SOURCE_BRIDGE_OWNED = "codex_bridge_owned"
SOURCE_DESKTOP_OBSERVED = "codex_desktop_observed"

MAX_EPOCH_BYTES = render_mod.MAX_EPOCH_BYTES


def _validate_epoch(bridge_epoch: str) -> str:
    raw = bridge_epoch.encode("utf-8")
    if not 1 <= len(raw) <= MAX_EPOCH_BYTES:
        raise ValueError(
            f"bridge_epoch must be 1..{MAX_EPOCH_BYTES} UTF-8 bytes, got {len(raw)}"
        )
    return bridge_epoch


class StateEngine:
    def __init__(self, bridge_epoch: str, *, source_kind: str = SOURCE_MOCK,
                 utc_anchor_ms: int = 0) -> None:
        if source_kind not in (SOURCE_MOCK, SOURCE_BRIDGE_OWNED, SOURCE_DESKTOP_OBSERVED):
            raise ValueError(f"unknown source kind: {source_kind!r}")
        self.bridge_epoch = _validate_epoch(bridge_epoch)
        self.source_kind = source_kind
        self.utc_anchor_ms = int(utc_anchor_ms)
        self.seq = 0
        self.internal = reducer.new_internal_state()

    def apply(self, event, monotonic_now: int) -> dict:
        """应用一个事件，返回新快照（seq 递增）。monotonic_now 为单调毫秒。"""
        if monotonic_now is None:
            raise ValueError("monotonic_now is required (no wall clock in reducer)")
        reducer.reduce(self.internal, event, int(monotonic_now))
        self.seq += 1
        if self.seq > render_mod.MAX_SEQ:
            raise OverflowError("seq exhausted for this bridge_epoch")
        return render_mod.render(
            self.internal,
            bridge_epoch=self.bridge_epoch,
            seq=self.seq,
            source_kind=self.source_kind,
            now_mono=int(monotonic_now),
            anchor_ms=self.utc_anchor_ms,
        )

    # 便捷只读视图（测试/诊断用，不参与协议）。
    @property
    def user_selected(self) -> Optional[str]:
        return self.internal.get("user_selected")

    def thread_ids(self):
        return list(self.internal["threads"].keys())
