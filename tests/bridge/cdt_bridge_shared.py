"""tests/bridge 共享工具（唯一命名模块，规避 pytest conftest 同名竞态）。

背景（A0 收编 2026-09-13）：pytest prepend 模式下，多个目录的 conftest.py
共用顶层模块名 "conftest"——ZC10 新增 tests/transport/ble 目录后，全量收集
时 tests/transport/wss/conftest.py 抢占 sys.modules["conftest"]，导致桥测试
运行期的 `from cdt_bridge_shared import ...`（懒加载处）拿到错误模块（v12_branch 假红）。
修复 = 共享代码进唯一命名模块；conftest.py 只保留 pytest fixture 定义与转发。
"""

import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "bridge"


def encode(snapshot: dict) -> bytes:
    """与 bridge 渲染一致的紧凑编码（字节预算检查用）。"""
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def depth(obj) -> int:
    if isinstance(obj, dict):
        return 1 + max([depth(v) for v in obj.values()] or [0])
    if isinstance(obj, list):
        return 1 + max([depth(v) for v in obj] or [0])
    return 0


def assert_invariants(snap: dict) -> None:
    """schema 无法表达的跨字段一致性（protocol/state.schema.json 描述第 3 条）。"""
    assert snap["threads_total"] >= len(snap["threads"])
    assert snap["threads_total"] <= 65535
    assert len(snap["threads"]) <= 8
    usage = snap["usage"]
    assert usage["windows_total"] >= len(usage["windows"])
    assert len(usage["windows"]) <= 4
    ids = [t["id"] for t in snap["threads"]]
    assert len(ids) == len(set(ids)), "thread ids must be unique"
    assert snap["selected_thread_id"] is None or snap["selected_thread_id"] in ids
    assert len(encode(snap)) <= 16384, "snapshot must fit 16KiB"
    assert depth(snap) <= 12, "snapshot depth must be <= 12"


class FrozenZcodeWallClock:
    """给 bridge.sources.zcode 冻结墙钟的测试替身（ZC3 引入）。

    背景：fixture 的 1308 重置时刻是固定历史字符串（2026-09-12 23:01:32），
    而观察器对"重置时刻已过"的限额窗口做墙钟抑制（ZC1-fix 裁决）——不冻结
    墙钟的用例每天 23:01 后都会假红（时间炸弹）。monkeypatch.setattr(zc,
    "time", FrozenZcodeWallClock(<epoch>)) 后，zcode.py 视角的 time.time()
    返回冻结值，time.mktime 等其余实现走真模块（重置时刻解析不受影响）。
    """

    def __init__(self, frozen_epoch: float) -> None:
        self._frozen = float(frozen_epoch)

    def time(self) -> float:
        return self._frozen

    def mktime(self, t):
        import time as _real

        return _real.mktime(t)

    def __getattr__(self, name):  # 其余 time.* 透传真模块
        import time as _real

        return getattr(_real, name)
