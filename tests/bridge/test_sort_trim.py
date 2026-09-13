"""排序、上限裁剪与字节级截断测试（INTERFACES §3 排序/裁剪顺序）。"""

import bridge.events as ev
from bridge.state.engine import StateEngine
from bridge.state.render import clamp_utf8

from cdt_bridge_shared import encode


def run(engine, *events):
    last = 0
    for e in events:
        mono = e.at_ms if e.at_ms is not None else last
        last = mono
        engine.apply(e, mono)


def ids(snap):
    return [t["id"] for t in snap["threads"]]


# ---- 排序：needs_you > error > working/thinking > done > idle ----

def make_thread(engine, tid, at_ms):
    run(engine, ev.thread_started(tid, "p-" + tid, at_ms=at_ms))


def test_priority_ordering():
    e = StateEngine("e")
    make_thread(e, "t-done", 0)
    make_thread(e, "t-working", 10)
    make_thread(e, "t-error", 20)
    make_thread(e, "t-idle", 30)
    make_thread(e, "t-needs", 40)
    run(e,
        ev.turn_started("t-done", "d1", at_ms=100),
        ev.turn_started("t-working", "w1", at_ms=110),
        ev.turn_started("t-error", "e1", at_ms=120),
        ev.turn_completed("t-done", "d1", ev.TURN_STATUS_COMPLETED, at_ms=130),
        ev.turn_completed("t-error", "e1", ev.TURN_STATUS_FAILED, at_ms=140),
        ev.turn_started("t-needs", "n1", at_ms=150),
        ev.approval_requested("t-needs", 0, "等待", turn_id="n1", at_ms=160))
    snap = e.apply(ev.select_thread(None, at_ms=170), 170)
    assert ids(snap) == ["t-needs", "t-error", "t-working", "t-done", "t-idle"]


def test_same_rank_updated_desc_then_id_asc():
    e = StateEngine("e")
    for tid in ("a", "b", "c", "d"):
        make_thread(e, tid, 0)
    # 同为 idle 且 updated 相同 → id 升序；c 被 status 事件更新到 50 → 降序跳到最前
    run(e, ev.thread_status("c", ev.THREAD_STATUS_IDLE, at_ms=50))
    snap = e.apply(ev.select_thread(None, at_ms=60), 60)
    assert ids(snap) == ["c", "a", "b", "d"]


def test_thinking_and_working_share_rank():
    e = StateEngine("e")
    make_thread(e, "t-think", 0)
    make_thread(e, "t-work", 0)
    run(e,
        ev.turn_started("t-think", "k1", at_ms=10),
        ev.item_started("t-think", "k1", "reasoning", at_ms=20),
        ev.turn_started("t-work", "w1", at_ms=15))
    snap = e.apply(ev.select_thread(None, at_ms=30), 30)
    # 同级（working/thinking）按 updated 降序：t-work(15) 在 t-think(20) 之前？否：20>15
    assert ids(snap) == ["t-think", "t-work"]


# ---- 线程数裁剪：9 → 8 + truncated；选中与 needs_you 保留 ----

def test_nine_threads_trimmed_with_selected_kept():
    e = StateEngine("e")
    for i in range(9):
        make_thread(e, f"t-{i:02d}", i)
    # 选中优先级最低的线程（最早 updated、idle）
    snap = e.apply(ev.select_thread("t-00", at_ms=100), 100)
    got = ids(snap)
    assert len(got) == 8
    assert snap["threads_total"] == 9
    assert snap["threads_truncated"] is True
    assert "t-00" in got          # 选中线程占一个名额
    assert got[0] == "t-08"       # 默认显示顺序仍是最高优先级在前


def test_needs_you_survives_trim_when_others_dropped():
    e = StateEngine("e")
    for i in range(9):
        make_thread(e, f"t-{i:02d}", i)
    run(e, ev.turn_started("t-00", "n1", at_ms=100),
        ev.approval_requested("t-00", 0, "等待", turn_id="n1", at_ms=110))
    snap = e.apply(ev.select_thread(None, at_ms=120), 120)
    got = ids(snap)
    assert got[0] == "t-00"                      # needs_you 排最前且保留
    assert len(got) == 8 and snap["threads_total"] == 9


# ---- 字符串按 UTF-8 完整码点截断 ----

def test_utf8_clamp_on_codepoint_boundary():
    # 191 个 ASCII + 1 个 3 字节汉字 = 194 字节 → 截到 192 字节 = 191 个 a（半个码点被丢弃）
    text = "a" * 191 + "汉"
    out = clamp_utf8(text, 192)
    assert out == "a" * 191
    assert len(out.encode("utf-8")) <= 192

    # 64 个汉字恰好 192 字节 → 不截断
    assert clamp_utf8("汉" * 64, 192) == "汉" * 64
    # 65 个汉字 → 64 个
    assert clamp_utf8("汉" * 65, 192) == "汉" * 64


def test_overlong_chinese_activity_truncated_in_snapshot():
    e = StateEngine("e")
    run(e, ev.thread_started("t1", "p", at_ms=0),
        ev.turn_started("t1", "turn-1", at_ms=10),
        ev.item_started("t1", "turn-1", "agentMessage",
                        summary="汉" * 300, at_ms=20))
    snap = e.apply(ev.select_thread(None, at_ms=30), 30)
    activity = snap["threads"][0]["activity"]
    assert len(activity.encode("utf-8")) <= 192
    assert activity == "汉" * 64


def test_all_string_fields_clamped():
    e = StateEngine("e")
    run(e,
        ev.thread_started("t" * 300, "项" * 200, at_ms=0),
        ev.turn_started("t" * 300, "u" * 300, at_ms=10),
        ev.plan_updated("t" * 300, "u" * 300, [("步" * 200, "pending")], at_ms=20),
        ev.rate_limits(({"id": "w" * 300, "label": "标" * 100, "used_percent": 5.0,
                         "duration_mins": 60, "resets_at_ms": None},), at_ms=30),
    )
    snap = e.apply(ev.select_thread(None, at_ms=40), 40)
    rec = snap["threads"][0]
    assert len(rec["id"].encode("utf-8")) <= 128
    assert len(rec["project"].encode("utf-8")) <= 96
    assert len(rec["turn_id"].encode("utf-8")) <= 128
    assert len(rec["plan"]["steps"][0]["text"].encode("utf-8")) <= 128
    w = snap["usage"]["windows"][0]
    assert len(w["id"].encode("utf-8")) <= 128
    assert len(w["label"].encode("utf-8")) <= 48


# ---- plan steps / usage windows 数量裁剪 ----

def test_plan_steps_trimmed_to_eight_with_total():
    e = StateEngine("e")
    steps = [(f"步骤 {i:02d}", "pending") for i in range(12)]
    run(e, ev.thread_started("t1", "p", at_ms=0),
        ev.turn_started("t1", "turn-1", at_ms=10),
        ev.plan_updated("t1", "turn-1", steps, at_ms=20))
    snap = e.apply(ev.select_thread(None, at_ms=30), 30)
    plan = snap["threads"][0]["plan"]
    assert len(plan["steps"]) == 8
    assert plan["total"] == 12
    assert plan["truncated"] is True


def test_usage_windows_trimmed_to_four_with_total():
    e = StateEngine("e")
    windows = tuple({"id": f"w{i}", "label": f"W{i}", "used_percent": float(i),
                     "duration_mins": 60, "resets_at_ms": None} for i in range(6))
    run(e, ev.thread_started("t1", "p", at_ms=0), ev.rate_limits(windows, at_ms=10))
    snap = e.apply(ev.select_thread(None, at_ms=20), 20)
    usage = snap["usage"]
    assert len(usage["windows"]) == 4
    assert usage["windows_total"] == 6
    assert usage["windows_truncated"] is True


# ---- 超预算场景：字段全满 → 仍 ≤16KiB，选中线程保留 ----

def test_full_snapshot_fits_16kib_and_keeps_selected():
    e = StateEngine("e", source_kind="mock", utc_anchor_ms=1789002000000)
    steps = [("检" * 42, "in_progress")] * 8  # 每步 126 字节
    for i in range(8):
        tid = f"thread-{i:02d}"
        run(e,
            ev.thread_started(tid, "项" * 32, at_ms=i),                # project 满 96 字节
            ev.turn_started(tid, "u" * 128, summary="活" * 64, at_ms=100 + i),
            ev.plan_updated(tid, "u" * 128, steps, at_ms=200 + i),
            ev.approval_requested(tid, i, "等" * 64, turn_id="u" * 128, at_ms=250 + i),
        )
    windows = tuple({"id": f"win-{i}", "label": "标" * 16, "used_percent": 42.0,
                     "duration_mins": 300, "resets_at_ms": 1789012800000}
                    for i in range(4))
    run(e, ev.rate_limits(windows, at_ms=300))
    snap = e.apply(ev.select_thread("thread-07", at_ms=310), 310)
    assert len(encode(snap)) <= 16384
    assert "thread-07" in ids(snap)              # 选中线程被保留
    assert snap["threads_total"] == 8
    assert snap["threads_truncated"] is True     # 为塞进 16KiB 而丢弃了线程
    assert len(snap["threads"]) < 8
