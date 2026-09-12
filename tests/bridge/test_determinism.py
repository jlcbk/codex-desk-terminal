"""确定性：相同事件序列 + 相同时钟序列 ⇒ 字节相同的 AppState。"""

import pathlib

import bridge.events as ev
from bridge.sources import mock, replay
from bridge.state.engine import StateEngine

from conftest import REPO_ROOT, encode


def test_mock_lifecycle_byte_identical_across_runs():
    a = [encode(s) for s in mock.run("lifecycle")]
    b = [encode(s) for s in mock.run("lifecycle")]
    assert a == b and len(a) == 10


def test_all_scenarios_byte_identical_across_runs():
    for name in mock.SCENARIO_NAMES:
        assert [encode(s) for s in mock.run(name)] == [encode(s) for s in mock.run(name)], name


def test_replay_fixture_matches_mock_lifecycle_bytes():
    """JSONL 回放与代码构造的同一事件序列产出字节相同的快照。"""
    fixture = REPO_ROOT / "tests" / "fixtures" / "bridge" / "lifecycle_events.jsonl"
    replayed = [encode(s) for s in replay.run_file(str(fixture), epoch=mock.DEFAULT_EPOCH,
                                                   utc_anchor_ms=mock.ANCHOR_MS)]
    built = [encode(s) for s in mock.run("lifecycle")]
    assert replayed == built


def test_clock_sequence_drives_output():
    """相同事件、不同时钟序列 ⇒ 不同字节；同序列重放 ⇒ 相同字节。"""
    def build(times):
        engine = StateEngine("e", utc_anchor_ms=0)
        out = []
        events = [ev.thread_started("t1", "p"), ev.turn_started("t1", "turn-1")]
        for event, now in zip(events, times):
            out.append(encode(engine.apply(event, now)))
        return out

    assert build([0, 100]) == build([0, 100])   # 同时钟序列 → 相同
    assert build([0, 100]) != build([0, 200])   # 不同时钟序列 → 不同


def test_no_wall_clock_or_random_in_pure_modules():
    """红线自检：纯快照管线模块不允许墙钟/随机/环境。

    P3.1 注（A1）：bridge/sources/codex.py 与 bridge/codex_rpc.py 是 live IO
    模块（app-server 子进程、超时与退避需要单调钟/抖动），不属于纯快照管线，
    在此排除；其"事件→NormalizedEvent→快照"的确定性由
    tests/bridge/test_codex_adapter.py 的 mapper 与序列测试覆盖，
    engine 侧纯度仍由本测试与 bridge/state/ 全量扫描保证。

    P3.2 后 A0 裁决（2026-09-10）：bridge/transports/ 整目录同为 IO 层
    （网络收发、退避抖动），按同一理由排除纯度扫描；传输的确定性语义
    （seq 单调、同字节输出）由 tests/transport/ 的协议测试覆盖。

    R2 补记（2026-09-11，A1）：bridge/serve_codex.py 是持续服务编排层
    （任务队列时间戳、归档命名、线程锁），与 mock 服务脚本同性质，不属于
    纯快照管线；其"快照盖章只加 epoch/seq、不改内容"的语义由
    tests/bridge/test_serve_codex.py 覆盖。

    ZC1 补记（2026-09-12，A1）：bridge/sources/zcode.py 同为文件轮询 IO 模块
    （rollout/metadata/hook spool 增量读；lookback 新鲜度需要 time.time，
    [1308] 重置时刻按任务约定用 time.mktime 本地时区解析），按同一理由排除；
    其确定性由 test_zcode_mapper.py（纯映射逐条断言）与
    test_zcode_observer.py（固定单调钟序列 + 逐快照 schema/不变量）覆盖。
    """
    banned = ("import time", "import datetime", "import random", "import uuid",
              "time.time", "time.monotonic", "datetime.now", "datetime.utcnow",
              "random.", "uuid.", "os.environ", "getenv")
    excluded = {"__main__.py", "codex.py", "codex_rpc.py", "serve_codex.py",
                "zcode.py"}
    targets = [pathlib.Path("/Users/cui/Documents/Projects/codex-desk-terminal/bridge")]
    files = [p for p in targets[0].rglob("*.py")
             if p.name not in excluded
             and "transports" not in p.parts]
    assert files, "bridge package must exist"
    for path in files:
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} contains banned token {token!r}"
