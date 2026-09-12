"""契约 v1.2 增补（ZC4）：model_info / token_totals 事件与 threads[].model/tokens。

覆盖（docs/INTERFACES.md §3 v1.2 两行 + §8 兼容矩阵）：
- reducer：新事件建档与更新；turn_started 不清会话累计字段；
- render：快照恒输出 model/tokens（缺失 → None），负值诚实置 None；
- schema：v1.2 快照过 protocol/state.schema.json（可选字段向后兼容）；
- events：to_dict/from_dict 往返。
"""

import pytest

import bridge.events as ev
from bridge.state.engine import StateEngine


def build(*events, epoch="epoch-v12", anchor=1000):
    engine = StateEngine(epoch, source_kind="mock", utc_anchor_ms=anchor)
    snaps = [engine.apply(e, e.at_ms if e.at_ms is not None else 0) for e in events]
    return engine, snaps


# ---- model_info：建档 + 更新 ----

def test_model_info_creates_and_updates():
    engine, snaps = build(
        ev.thread_started("t1", "proj", at_ms=0),
        ev.model_info("t1", "GLM-5.3", at_ms=10),
    )
    rec = snaps[-1]["threads"][0]
    assert rec["model"] == "GLM-5.3"
    # model_info 先于 thread_started 到达（codex adapter 建线程后立即发送）也须生效。
    engine2 = StateEngine("e2")
    snap = engine2.apply(ev.model_info("t9", "m9", at_ms=1), 1)
    assert snap["threads"][0]["model"] == "m9"


# ---- token_totals：会话累计，turn_started 不清零 ----

def test_token_totals_survive_turn_restart():
    engine = StateEngine("e")
    engine.apply(ev.thread_started("t1", "proj", at_ms=0), 0)
    engine.apply(ev.token_totals("t1", 1200, 3456, 789, at_ms=10), 10)
    s1 = engine.apply(ev.turn_started("t1", "turn-1", at_ms=20), 20)
    rec = s1["threads"][0]
    assert rec["tokens"] == {"input_tokens": 1200, "output_tokens": 3456,
                             "cached_tokens": 789}
    # 跨 turn 不清零（会话累计语义）：新 turn 开始后仍保留。
    engine.apply(ev.turn_completed("t1", "turn-1", ev.TURN_STATUS_COMPLETED, at_ms=90), 90)
    s2 = engine.apply(ev.turn_started("t1", "turn-2", at_ms=100), 100)
    assert s2["threads"][0]["tokens"]["input_tokens"] == 1200
    assert s2["threads"][0]["model"] is None  # 未发过 model_info：诚实 None


def test_token_totals_update_overwrites():
    engine, snaps = build(
        ev.thread_started("t1", "p", at_ms=0),
        ev.token_totals("t1", 1, 2, 3, at_ms=10),
        ev.token_totals("t1", 10, None, 30, at_ms=20),
    )
    assert snaps[-1]["threads"][0]["tokens"] == {
        "input_tokens": 10, "output_tokens": None, "cached_tokens": 30}


# ---- render：恒输出 v1.2 键；缺失/脏值诚实 null ----

def test_render_always_emits_model_and_tokens():
    engine, snaps = build(ev.thread_started("t1", "p", at_ms=0),
                          ev.turn_started("t1", "turn-1", at_ms=10))
    rec = snaps[-1]["threads"][0]
    assert rec["model"] is None
    assert rec["tokens"] == {"input_tokens": None, "output_tokens": None,
                             "cached_tokens": None}


def test_render_clamps_negative_token_totals_to_none():
    engine, snaps = build(
        ev.thread_started("t1", "p", at_ms=0),
        # 直接构造脏事件（正常 adapter 不产负值）：render 层诚实置 None。
        ev.NormalizedEvent(ev.EVENT_TOKEN_TOTALS, thread_id="t1",
                           input_tokens=-5, output_tokens=7, cached_tokens=None),
    )
    assert snaps[-1]["threads"][0]["tokens"] == {
        "input_tokens": None, "output_tokens": 7, "cached_tokens": None}


def test_render_clamps_long_model_name():
    engine, snaps = build(
        ev.thread_started("t1", "p", at_ms=0),
        ev.model_info("t1", "m" * 100, at_ms=10),
    )
    assert snaps[-1]["threads"][0]["model"] == "m" * 48  # ≤48 UTF-8 字节


# ---- 未知线程 / 兼容矩阵 ----

def test_v12_fields_absent_in_v1_snapshot_are_displayable():
    """旧桥+新固件=字段缺失渲染 "--"：render 恒输出键，缺值即 None（协议侧）。"""
    engine, snaps = build(ev.thread_started("t1", "p", at_ms=0))
    rec = snaps[-1]["threads"][0]
    assert "model" in rec and "tokens" in rec
    assert rec["model"] is None and rec["tokens"]["input_tokens"] is None


# ---- events 往返 ----

def test_v12_events_roundtrip():
    m = ev.model_info("t1", "GLM-5.3", at_ms=5)
    m2 = ev.NormalizedEvent.from_dict(m.to_dict())
    assert m2.type == ev.EVENT_MODEL_INFO and m2.thread_id == "t1"
    assert m2.model == "GLM-5.3" and m2.at_ms == 5

    t = ev.token_totals("t1", 1, None, 3, at_ms=6)
    t2 = ev.NormalizedEvent.from_dict(t.to_dict())
    assert t2.type == ev.EVENT_TOKEN_TOTALS
    assert (t2.input_tokens, t2.output_tokens, t2.cached_tokens) == (1, None, 3)

    # model=None 不写键（与既有省略缺省约定一致）。
    assert "model" not in ev.model_info("t1", None).to_dict()


# ---- schema 校验 ----

def test_v12_snapshot_validates_against_schema(validator):
    engine = StateEngine("e", source_kind="mock", utc_anchor_ms=1000)
    snap = engine.apply(ev.thread_started("t1", "p", at_ms=0), 0)
    engine.apply(ev.model_info("t1", "GLM-5.3", at_ms=10), 10)
    snap = engine.apply(
        ev.token_totals("t1", 9007199254740991, 0, None, at_ms=20), 20)
    errs = list(validator.iter_errors(snap))
    assert errs == []
    assert snap["threads"][0]["model"] == "GLM-5.3"
    assert snap["threads"][0]["tokens"]["input_tokens"] == 9007199254740991


def test_v1_snapshot_still_validates_without_v12_fields(validator):
    engine, snaps = build(ev.thread_started("t1", "p", at_ms=0))
    assert list(validator.iter_errors(snaps[-1])) == []
