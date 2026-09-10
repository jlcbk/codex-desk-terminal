"""NormalizedEvent 序列化与 reducer 基础行为。"""

import bridge.events as ev
from bridge.state.engine import StateEngine


def test_roundtrip_to_dict_from_dict():
    event = ev.plan_updated(
        "t1", "turn-1",
        [("a", ev.PLAN_COMPLETED), ("b", ev.PLAN_IN_PROGRESS), ("c", "bogus")],
        total=9, at_ms=42,
    )
    restored = ev.NormalizedEvent.from_dict(event.to_dict())
    assert restored == event


def test_from_dict_requires_type():
    import pytest

    with pytest.raises(ValueError):
        ev.NormalizedEvent.from_dict({"thread_id": "t1"})
    with pytest.raises(ValueError):
        ev.NormalizedEvent.from_dict("not-an-object")


def test_unknown_event_type_is_ignored():
    engine = StateEngine("epoch-x")
    snap = engine.apply(ev.NormalizedEvent("brand_new_future_event", thread_id="t1"), 5)
    assert snap["threads"] == []
    assert snap["seq"] == 1
    assert snap["generated_at_ms"] == 5  # anchor=0 + 单调 5


def test_approval_roundtrip_fields():
    event = ev.approval_requested("t1", 7, "运行命令需要批准", turn_id="turn-9", at_ms=10)
    d = event.to_dict()
    assert d["type"] == "approval_requested"
    assert d["request_id"] == 7
    restored = ev.NormalizedEvent.from_dict(d)
    assert restored == event
