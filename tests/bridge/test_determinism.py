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
    """红线自检：reducer/render/engine/events/sources 不允许墙钟/随机/环境。"""
    banned = ("import time", "import datetime", "import random", "import uuid",
              "time.time", "time.monotonic", "datetime.now", "datetime.utcnow",
              "random.", "uuid.", "os.environ", "getenv")
    targets = [pathlib.Path("/Users/cui/Documents/Projects/codex-desk-terminal/bridge")]
    files = [p for p in targets[0].rglob("*.py") if p.name != "__main__.py"]
    assert files, "bridge package must exist"
    for path in files:
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} contains banned token {token!r}"
