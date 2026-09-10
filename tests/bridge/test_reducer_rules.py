"""reducer 规则单元测试（DEVELOPMENT_PLAN §8 State/reducer 行必测案例）。

覆盖：新 turn、重复 completion、迟到事件、多 pending、错误/取消、
无计划/额度、THINKING 判定、PLAN UPDATE 保 WORKING、断连不转 IDLE、
pending 与静音边界、token 上下文、epoch/source 约束。
"""

import pytest

import bridge.events as ev
from bridge.state.engine import StateEngine


def build(*events, epoch="epoch-rules", anchor=1000):
    engine = StateEngine(epoch, source_kind="mock", utc_anchor_ms=anchor)
    snaps = [engine.apply(e, e.at_ms if e.at_ms is not None else 0) for e in events]
    return engine, snaps


# ---- 新 turn：清旧 plan/pending/计时，不继承 DONE ----

def test_new_turn_clears_plan_pending_and_does_not_inherit_done():
    t = "t1"
    engine = StateEngine("e")
    events = [
        ev.thread_started(t, "p", at_ms=0),
        ev.turn_started(t, "turn-1", summary="第一件事", at_ms=10),
        ev.plan_updated(t, "turn-1", [("旧步骤", "completed")], at_ms=20),
        ev.approval_requested(t, 0, "等待批准", turn_id="turn-1", at_ms=30),
        ev.turn_completed(t, "turn-1", ev.TURN_STATUS_COMPLETED, at_ms=40),
    ]
    for e in events:
        snap = engine.apply(e, e.at_ms)
    assert snap["threads"][0]["state"] == "done"

    snap = engine.apply(ev.turn_started(t, "turn-2", summary="第二件事", at_ms=100), 100)
    rec = snap["threads"][0]
    assert rec["turn_id"] == "turn-2"
    assert rec["state"] == "working"          # 不继承 DONE
    assert rec["plan"]["total"] == 0 and rec["plan"]["steps"] == []
    assert rec["attention"] is None            # 旧 pending 已清
    assert rec["end_reason"] is None
    assert rec["elapsed_ms"] == 0              # 计时清零
    assert rec["waiting_ms"] == 0


# ---- 重复 completion：幂等，不重置计时 ----

def test_duplicate_completion_is_idempotent():
    t = "t1"
    engine = StateEngine("e")
    engine.apply(ev.thread_started(t, "p", at_ms=0), 0)
    engine.apply(ev.turn_started(t, "turn-1", summary="任务", at_ms=10), 10)
    s1 = engine.apply(ev.turn_completed(t, "turn-1", ev.TURN_STATUS_COMPLETED, at_ms=50), 50)
    s2 = engine.apply(ev.turn_completed(t, "turn-1", ev.TURN_STATUS_COMPLETED, at_ms=90), 90)
    a, b = s1["threads"][0], s2["threads"][0]
    assert b["state"] == "done" and b["end_reason"] == "completed"
    assert b["updated_at_ms"] == a["updated_at_ms"]  # 迟到的重复完成不改时间
    assert b["elapsed_ms"] == a["elapsed_ms"]        # 计时冻结在首次终态


# ---- 迟到事件：终态后禁止复活该 turn ----

def test_late_item_cannot_revive_finished_turn():
    t = "t1"
    engine = StateEngine("e")
    engine.apply(ev.thread_started(t, "p", at_ms=0), 0)
    engine.apply(ev.turn_started(t, "turn-1", summary="任务", at_ms=10), 10)
    done_snap = engine.apply(ev.turn_completed(t, "turn-1", ev.TURN_STATUS_COMPLETED, at_ms=50), 50)
    late1 = engine.apply(
        ev.item_started(t, "turn-1", "commandExecution", summary="迟到的命令", at_ms=60), 60)
    late2 = engine.apply(
        ev.plan_updated(t, "turn-1", [("迟到计划", "in_progress")], at_ms=70), 70)
    late3 = engine.apply(ev.thread_status(t, "active", at_ms=80), 80)
    for snap in (late1, late2, late3):
        rec = snap["threads"][0]
        assert rec["state"] == "done"
        assert rec["updated_at_ms"] == done_snap["threads"][0]["updated_at_ms"]
    assert late2["threads"][0]["plan"]["total"] == 0  # 迟到计划也被丢弃


def test_late_event_from_older_turn_is_dropped():
    t = "t1"
    engine = StateEngine("e")
    engine.apply(ev.thread_started(t, "p", at_ms=0), 0)
    engine.apply(ev.turn_started(t, "turn-1", at_ms=10), 10)
    engine.apply(ev.turn_completed(t, "turn-1", ev.TURN_STATUS_COMPLETED, at_ms=20), 20)
    engine.apply(ev.turn_started(t, "turn-2", summary="新任务", at_ms=30), 30)
    snap = engine.apply(
        ev.item_started(t, "turn-1", "commandExecution", summary="旧 turn 的迟到事件", at_ms=40), 40)
    rec = snap["threads"][0]
    assert rec["state"] == "working"
    assert rec["activity"] == "新任务"  # 旧 turn 事件没有覆盖 activity


# ---- 多 pending：计数保留，逐一解除 ----

def test_multiple_pending_keeps_count_until_each_resolved():
    t = "t1"
    engine = StateEngine("e")
    engine.apply(ev.thread_started(t, "p", at_ms=0), 0)
    engine.apply(ev.turn_started(t, "turn-1", at_ms=10), 10)
    s1 = engine.apply(ev.approval_requested(t, 0, "运行命令需要批准", turn_id="turn-1", at_ms=20), 20)
    s2 = engine.apply(ev.approval_requested(t, 1, "等待用户输入", turn_id="turn-1", at_ms=30), 30)
    assert s1["threads"][0]["attention"]["pending_count"] == 1
    assert s2["threads"][0]["state"] == "needs_you"
    assert s2["threads"][0]["attention"]["pending_count"] == 2
    s3 = engine.apply(ev.server_request_resolved(t, 0, at_ms=40), 40)
    assert s3["threads"][0]["state"] == "needs_you"       # 还剩一个 pending
    assert s3["threads"][0]["attention"]["pending_count"] == 1
    s4 = engine.apply(ev.server_request_resolved(t, 1, at_ms=50), 50)
    assert s4["threads"][0]["state"] == "working"
    assert s4["threads"][0]["attention"] is None
    assert s4["threads"][0]["waiting_ms"] == 0


# ---- 错误 / 取消 ----

def test_failed_turn_maps_to_error():
    _, snaps = build(
        ev.thread_started("t1", "p", at_ms=0),
        ev.turn_started("t1", "turn-1", at_ms=10),
        ev.turn_completed("t1", "turn-1", ev.TURN_STATUS_FAILED, summary="编译错误", at_ms=20),
    )
    rec = snaps[-1]["threads"][0]
    assert rec["state"] == "error" and rec["end_reason"] == "failed"
    assert rec["activity"] == "编译错误"


def test_cancelled_turn_maps_to_idle_with_reason():
    _, snaps = build(
        ev.thread_started("t1", "p", at_ms=0),
        ev.turn_started("t1", "turn-1", at_ms=10),
        ev.turn_completed("t1", "turn-1", ev.TURN_STATUS_CANCELLED, at_ms=20),
    )
    rec = snaps[-1]["threads"][0]
    assert rec["state"] == "idle" and rec["end_reason"] == "cancelled"
    assert rec["activity"] == "已取消"  # 默认取消说明


def test_failed_wins_over_pending_then_clears_pending():
    t = "t1"
    engine = StateEngine("e")
    engine.apply(ev.thread_started(t, "p", at_ms=0), 0)
    engine.apply(ev.turn_started(t, "turn-1", at_ms=10), 10)
    engine.apply(ev.approval_requested(t, 0, "等待", turn_id="turn-1", at_ms=20), 20)
    snap = engine.apply(ev.turn_completed(t, "turn-1", ev.TURN_STATUS_FAILED, at_ms=30), 30)
    rec = snap["threads"][0]
    assert rec["state"] == "error"          # 可靠失败终态优先于等待展示
    assert rec["attention"] is None          # 终态清理该 turn pending


# ---- 无计划 / 无额度 ----

def test_no_plan_and_no_usage_defaults():
    _, snaps = build(ev.thread_started("t1", "p", at_ms=0))
    snap = snaps[-1]
    rec = snap["threads"][0]
    assert rec["plan"] == {"total": 0, "truncated": False, "steps": []}
    assert snap["usage"]["available"] is False
    assert snap["usage"]["windows"] == []
    assert snap["usage"]["windows_total"] == 0
    assert snap["usage"]["updated_at_ms"] is None


# ---- THINKING 判定 ----

def test_thinking_only_for_explicit_reasoning_item():
    engine = StateEngine("e")
    engine.apply(ev.thread_started("t1", "p", at_ms=0), 0)
    s = engine.apply(ev.turn_started("t1", "turn-1", at_ms=10), 10)
    assert s["threads"][0]["state"] == "working"  # 未知阶段不猜"思考"
    s = engine.apply(ev.item_started("t1", "turn-1", "reasoning", at_ms=20), 20)
    assert s["threads"][0]["state"] == "thinking"
    s = engine.apply(ev.item_started("t1", "turn-1", "commandExecution", summary="ls", at_ms=30), 30)
    assert s["threads"][0]["state"] == "working"


# ---- PLAN UPDATE：内容事件，不改状态 ----

def test_plan_update_keeps_state_and_upstream_inprogress_normalized():
    _, snaps = build(
        ev.thread_started("t1", "p", at_ms=0),
        ev.turn_started("t1", "turn-1", summary="任务", at_ms=10),
        ev.plan_updated("t1", "turn-1", [
            ("a", "completed"), ("b", "inProgress"), ("c", "unknown-status"),
        ], total=7, at_ms=20),
    )
    rec = snaps[-1]["threads"][0]
    assert rec["state"] == "working"  # PLAN UPDATE 保持 WORKING
    assert rec["plan"]["total"] == 7
    assert [s["status"] for s in rec["plan"]["steps"]] == ["completed", "in_progress", "pending"]


# ---- pending 与 thread/status 边界 ----

def test_thread_idle_does_not_clear_pending():
    _, snaps = build(
        ev.thread_started("t1", "p", at_ms=0),
        ev.turn_started("t1", "turn-1", at_ms=10),
        ev.approval_requested("t1", 0, "等待", turn_id="turn-1", at_ms=20),
        ev.thread_status("t1", ev.THREAD_STATUS_IDLE, at_ms=30),
    )
    rec = snaps[-1]["threads"][0]
    assert rec["state"] == "needs_you"  # pending 未解除前不被 idle 抢掉
    assert rec["attention"]["pending_count"] == 1


def test_waiting_ms_measured_from_first_pending():
    _, snaps = build(
        ev.thread_started("t1", "p", at_ms=0),
        ev.turn_started("t1", "turn-1", at_ms=10),
        ev.approval_requested("t1", 0, "等待", turn_id="turn-1", at_ms=100),
        ev.token_usage("t1", 10, 100, turn_id="turn-1", at_ms=250),
    )
    rec = snaps[-1]["threads"][0]
    assert rec["waiting_ms"] == 150  # 250 - 100
    assert rec["elapsed_ms"] == 240  # 250 - 10


# ---- 断连：不全部转 IDLE，stale 标记 ----

def test_disconnect_keeps_states_and_marks_stale():
    engine = StateEngine("e")
    engine.apply(ev.thread_started("t1", "p", at_ms=0), 0)
    engine.apply(ev.turn_started("t1", "turn-1", at_ms=10), 10)
    snap = engine.apply(ev.source_disconnected(at_ms=20), 20)
    assert snap["source"]["connected"] is False
    assert snap["source"]["stale"] is True
    assert snap["threads"][0]["state"] == "working"  # 不伪造 IDLE/DONE
    snap = engine.apply(ev.source_reconnected(at_ms=30), 30)
    assert snap["source"]["connected"] is True
    assert snap["source"]["stale"] is False


# ---- token 上下文与额度 ----

def test_token_usage_context_and_percent():
    _, snaps = build(
        ev.thread_started("t1", "p", at_ms=0),
        ev.token_usage("t1", 258400, 258400, turn_id="turn-1", at_ms=10),
        ev.token_usage("t1", 100, 0, at_ms=20),   # capacity 无效 → 全 null
    )
    ctx = snaps[1]["threads"][0]["context"]
    assert ctx == {"used_tokens": 258400, "capacity_tokens": 258400, "used_percent": 100.0}
    ctx = snaps[2]["threads"][0]["context"]
    assert ctx == {"used_tokens": 100, "capacity_tokens": None, "used_percent": None}


def test_rate_limits_unavailable_clears_windows():
    _, snaps = build(
        ev.rate_limits(({"id": "w", "label": "L", "used_percent": 1.0,
                         "duration_mins": 60, "resets_at_ms": None},), at_ms=0),
        ev.rate_limits_unavailable(at_ms=10),
    )
    usage = snaps[-1]["usage"]
    assert usage["available"] is False and usage["windows"] == []


# ---- 选择：本地选中 / 非法 id 忽略 ----

def test_select_thread_defaults_and_unknown_ignored():
    engine = StateEngine("e")
    engine.apply(ev.thread_started("a", "pa", at_ms=0), 0)
    snap = engine.apply(ev.select_thread("missing", at_ms=10), 10)
    assert snap["selected_thread_id"] == "a"  # 未知 id 忽略 → 默认首项
    snap = engine.apply(ev.select_thread("a", at_ms=20), 20)
    assert snap["selected_thread_id"] == "a"
    snap = engine.apply(ev.select_thread(None, at_ms=30), 30)
    assert snap["selected_thread_id"] == "a"  # 清除后回默认首项


# ---- epoch / source 约束 ----

def test_epoch_and_source_kind_validation():
    with pytest.raises(ValueError):
        StateEngine("x" * 65)
    with pytest.raises(ValueError):
        StateEngine("")
    with pytest.raises(ValueError):
        StateEngine("ok", source_kind="bogus")


# ---- P3.2 钉死：等待只在明确 resolve/completion/取消才消除（INTERFACES §3）----
# 「pending request 集合非空即 NEEDS YOU；明确的 resolve/completion/取消才清理；
#   其他 item 事件不能抢掉等待状态。」以下断言冻结该语义，回归时不得放松。

def _enter_waiting(engine, thread="t1", turn="turn-1"):
    """走到 needs_you（turn 进行中 + 1 个 pending）并返回最新快照。"""
    engine.apply(ev.thread_started(thread, "p", at_ms=0), 0)
    engine.apply(ev.turn_started(thread, turn, summary="任务", at_ms=10), 10)
    return engine.apply(
        ev.approval_requested(thread, 0, "运行命令需要批准", turn_id=turn, at_ms=100), 100)


def test_waiting_survives_other_item_events():
    """迟到/普通 item 事件、plan、usage、thread 状态都不得抢掉 NEEDS YOU。"""
    engine = StateEngine("e")
    s = _enter_waiting(engine)
    assert s["threads"][0]["state"] == "needs_you"

    steps = [
        ev.item_started("t1", "turn-1", "commandExecution",
                        item_id="i1", summary="迟到的命令", at_ms=120),
        ev.item_completed("t1", "turn-1", "commandExecution", item_id="i1", at_ms=130),
        ev.plan_updated("t1", "turn-1", [("步骤", "in_progress")], at_ms=140),
        ev.token_usage("t1", 100, 1000, turn_id="turn-1", at_ms=150),
        ev.thread_status("t1", ev.THREAD_STATUS_ACTIVE, at_ms=160),
        ev.thread_status("t1", ev.THREAD_STATUS_IDLE, at_ms=170),
        ev.item_started("t1", "turn-1", "reasoning", at_ms=180),
    ]
    for e in steps:
        s = engine.apply(e, e.at_ms)
        rec = s["threads"][0]
        assert rec["state"] == "needs_you", f"{e.type} 不得抢掉等待状态"
        assert rec["attention"]["pending_count"] == 1
        # waiting_ms 从首个 pending 起算，中间事件不清零也不重新起算
        assert rec["waiting_ms"] == e.at_ms - 100
    # plan 是内容事件：等待期间仍允许更新（PLAN UPDATE 不改变业务状态）
    assert s["threads"][0]["plan"]["steps"] == [{"text": "步骤", "status": "in_progress"}]


def test_duplicate_resolved_is_idempotent_and_unknown_resolved_ignored():
    engine = StateEngine("e")
    _enter_waiting(engine)
    s1 = engine.apply(ev.server_request_resolved("t1", 0, at_ms=150), 150)
    assert s1["threads"][0]["state"] == "working"
    assert s1["threads"][0]["attention"] is None

    # 迟到的重复 resolved：不复活等待、不崩、不改业务状态
    s2 = engine.apply(ev.server_request_resolved("t1", 0, at_ms=200), 200)
    assert s2["threads"][0]["state"] == "working"
    assert s2["threads"][0]["attention"] is None
    assert s2["threads"][0]["waiting_ms"] == 0
    # 从未挂起的 request id：同样只走"无等待恢复"路径
    s3 = engine.apply(ev.server_request_resolved("t1", 99, at_ms=250), 250)
    assert s3["threads"][0]["state"] == "working"
    assert s3["threads"][0]["attention"] is None


def test_out_of_order_resolved_keeps_remaining_pending():
    """多 pending 乱序解除：只清匹配 id，剩量保持 needs_you。"""
    engine = StateEngine("e")
    engine.apply(ev.thread_started("t1", "p", at_ms=0), 0)
    engine.apply(ev.turn_started("t1", "turn-1", at_ms=10), 10)
    engine.apply(ev.approval_requested("t1", 7, "命令批准", turn_id="turn-1", at_ms=20), 20)
    s2 = engine.apply(ev.approval_requested("t1", 9, "用户输入", turn_id="turn-1", at_ms=30), 30)
    assert s2["threads"][0]["attention"]["pending_count"] == 2

    s3 = engine.apply(ev.server_request_resolved("t1", 9, at_ms=40), 40)  # 先解除后者
    assert s3["threads"][0]["state"] == "needs_you"
    assert s3["threads"][0]["attention"]["pending_count"] == 1
    assert s3["threads"][0]["attention"]["summary"] == "命令批准"  # 剩下的是 id=7
    s4 = engine.apply(ev.server_request_resolved("t1", 7, at_ms=50), 50)
    assert s4["threads"][0]["state"] == "working"
    assert s4["threads"][0]["attention"] is None


def test_cancelled_turn_clears_waiting_and_late_requests_cannot_revive():
    """取消是明确解除：清 pending → idle+cancelled；终态后迟到的审批请求被门闸丢弃。"""
    engine = StateEngine("e")
    _enter_waiting(engine)
    s = engine.apply(ev.turn_completed("t1", "turn-1", ev.TURN_STATUS_CANCELLED, at_ms=200), 200)
    rec = s["threads"][0]
    assert rec["state"] == "idle" and rec["end_reason"] == "cancelled"
    assert rec["attention"] is None and rec["waiting_ms"] == 0

    late = [
        ev.approval_requested("t1", 1, "迟到的审批", turn_id="turn-1", at_ms=210),
        ev.item_started("t1", "turn-1", "commandExecution", summary="迟到的命令", at_ms=220),
        ev.thread_status("t1", ev.THREAD_STATUS_ACTIVE, at_ms=230),
    ]
    for e in late:
        s = engine.apply(e, e.at_ms)
        rec = s["threads"][0]
        assert rec["state"] == "idle"
        assert rec["end_reason"] == "cancelled"
        assert rec["attention"] is None  # 不复活等待


def test_failed_terminal_error_not_done_and_late_activity_dropped():
    """失败终态 → ERROR（绝不显示 DONE）；迟到 item 不改写终态。"""
    engine = StateEngine("e")
    engine.apply(ev.thread_started("t1", "p", at_ms=0), 0)
    engine.apply(ev.turn_started("t1", "turn-1", at_ms=10), 10)
    engine.apply(ev.approval_requested("t1", 0, "等待", turn_id="turn-1", at_ms=20), 20)
    s = engine.apply(
        ev.turn_completed("t1", "turn-1", ev.TURN_STATUS_FAILED,
                          summary="模型拒绝请求", at_ms=30), 30)
    rec = s["threads"][0]
    assert rec["state"] == "error" and rec["end_reason"] == "failed"
    assert rec["activity"] == "模型拒绝请求"
    assert rec["attention"] is None  # 失败终态清理该 turn pending

    late = engine.apply(
        ev.item_started("t1", "turn-1", "agentMessage", summary="迟到的回复", at_ms=40), 40)
    rec = late["threads"][0]
    assert rec["state"] == "error"
    assert rec["end_reason"] == "failed"
    assert rec["activity"] == "模型拒绝请求"  # 迟到活动不改写终态摘要


def test_thread_system_error_maps_error_not_done():
    """thread/status systemError（thread 错误状态）→ ERROR。"""
    engine = StateEngine("e")
    engine.apply(ev.thread_started("t1", "p", at_ms=0), 0)
    engine.apply(ev.turn_started("t1", "turn-1", at_ms=10), 10)
    s = engine.apply(ev.thread_status("t1", ev.THREAD_STATUS_SYSTEM_ERROR, at_ms=20), 20)
    rec = s["threads"][0]
    assert rec["state"] == "error"
    assert rec["end_reason"] is None  # thread 级错误：非 turn 终态，end_reason 保持 null
