"""mock 场景验收：lifecycle 四阶段状态流转、其余 P1.5/P2 复用场景。"""

from bridge.sources import mock


def state_seq(snaps):
    return [(s["seq"], (s["threads"][0]["state"] if s["threads"] else None)) for s in snaps]


def test_lifecycle_four_stages():
    snaps = mock.run("lifecycle")
    assert len(snaps) == 10
    flow = state_seq(snaps)
    # IDLE → WORKING → (PLAN UPDATE 仍 WORKING) → NEEDS YOU → DONE
    assert flow == [
        (1, "idle"),
        (2, "working"),   # 阶段1 WORKING
        (3, "working"),
        (4, "working"),   # 阶段2 PLAN UPDATE：plan 出现、状态保持 WORKING
        (5, "working"),
        (6, "needs_you"), # 阶段3 NEEDS YOU
        (7, "needs_you"),
        (8, "working"),
        (9, "working"),
        (10, "done"),     # 阶段4 DONE
    ]

    by_seq = {s["seq"]: s for s in snaps}
    plan_snap = by_seq[4]
    rec = plan_snap["threads"][0]
    assert rec["state"] == "working"                      # PLAN UPDATE 保持 WORKING
    assert rec["plan"]["total"] == 3
    assert [st["status"] for st in rec["plan"]["steps"]] == [
        "completed", "in_progress", "pending"]

    wait_snap = by_seq[6]
    rec = wait_snap["threads"][0]
    assert rec["attention"] == {"pending_count": 1, "summary": "运行命令需要批准"}
    assert rec["waiting_ms"] == 0                          # 请求发生在当前时刻
    assert by_seq[7]["threads"][0]["waiting_ms"] == 300    # 900 → 1200

    done_snap = by_seq[10]
    rec = done_snap["threads"][0]
    assert rec["state"] == "done" and rec["end_reason"] == "completed"
    assert rec["elapsed_ms"] == 2000                       # 100 → 2100
    assert rec["attention"] is None

    usage = done_snap["usage"]
    assert usage["available"] is True
    assert [w["used_percent"] for w in usage["windows"]] == [42.0, 22.0]
    assert [w["duration_mins"] for w in usage["windows"]] == [300, 10080]
    ctx = done_snap["threads"][0]["context"]
    assert ctx["used_tokens"] == 154000 and ctx["capacity_tokens"] == 258400


def test_cancelled_scenario():
    snaps = mock.run("cancelled")
    first, last = snaps[0], snaps[-1]
    assert first["threads"][0]["state"] == "idle"
    last_rec = last["threads"][0]
    assert last_rec["state"] == "idle"                     # cancelled 显示 idle
    assert last_rec["end_reason"] == "cancelled"
    assert last_rec["activity"] == "已取消"
    assert state_seq(snaps)[-2][1] == "needs_you"          # 取消前在等待


def test_error_scenario():
    snaps = mock.run("error")
    last = snaps[-1]["threads"][0]
    assert last["state"] == "error" and last["end_reason"] == "failed"
    assert last["activity"] == "编译错误：类型不匹配"


def test_multi_agents_sort_swap():
    snaps = mock.run("multi_agents")
    assert len({s["seq"] for s in snaps}) == 7
    orders = {s["seq"]: [t["id"] for t in s["threads"]] for s in snaps}

    # 三线程全部在场
    assert set(orders[3]) == {"thread-alpha", "thread-beta", "thread-gamma"}
    # alpha 先工作 → 首位
    assert orders[4][0] == "thread-alpha"
    # beta 更新（t200）反超 alpha（t100）：同级 working 按 updated 降序
    assert orders[5][0] == "thread-beta"
    # alpha 进入 needs_you → 抢回头位（跨级）
    assert orders[6][0] == "thread-alpha"
    states = {s["seq"]: {t["id"]: t["state"] for t in s["threads"]} for s in snaps}
    assert states[6]["thread-alpha"] == "needs_you"
    assert states[6]["thread-beta"] == "working"
    # beta 完成 → done 沉到 needs_you 之后、idle 之前
    assert orders[7] == ["thread-alpha", "thread-beta", "thread-gamma"]
    assert states[7]["thread-beta"] == "done"
    assert states[7]["thread-gamma"] == "idle"


def test_usage_scenarios():
    missing = mock.run("usage_missing")[-1]["usage"]
    assert missing["available"] is False and missing["windows"] == []

    zero = mock.run("usage_0")[-1]["usage"]
    assert zero["available"] is True
    assert [w["used_percent"] for w in zero["windows"]] == [0.0, 0.0]

    full = mock.run("usage_100")[-1]["usage"]
    assert [w["used_percent"] for w in full["windows"]] == [100.0, 100.0]


def test_unknown_scenario_raises():
    try:
        mock.run("nope")
    except ValueError as exc:
        assert "unknown scenario" in str(exc)
    else:
        raise AssertionError("expected ValueError")
