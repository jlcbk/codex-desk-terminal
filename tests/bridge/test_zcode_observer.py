"""ZC1 离线集成单测：ZcodeObserver（bridge/sources/zcode.py）。

策略：tmpdir 假 rollout/agents/spool + 固定单调钟序列（poll_once 直注入 /
run_forever 注入 monotonic_fn），不触碰真实 ~/.zcode。断言全部走快照 JSON 字段
（state/attention/usage/selected_thread_id/end_reason/elapsed 等），逐快照过
冻结 schema 与跨字段不变量。覆盖：

- idle→working→needs_you→done 全流程；error 流；[1308] 用量窗口；
- 子代理 metadata 生命周期（mtime 变化才重读）与子代理 rollout 同池；
- 偏移增量（追加只产新事件，turn 不重置）；轮转/截断重读；
- rollout 目录消失→disconnected→恢复→reconnected；spool 历史不回放；
- max_threads 清池与协议 8 线程上限；坏行容错；冷启动尾巴上限；
- 停滞只产观测元数据、绝不伪造终态；lookback 旧文件不跟踪。

数据全部合成；fixtures/注入内容里的提示词/回复/工具输入是 SYNTHETIC 标记，
单测断言它们绝不进入任何快照（红线扫描）。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import time

import pytest

from bridge.sources import zcode as zc
from bridge.state.engine import SOURCE_ZCODE_OBSERVED, StateEngine
from conftest import assert_invariants

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "zcode"

SID = "sess_obs_main"
# mtime 基准取当前时间（lookback 新鲜度过滤按墙钟比较；+1s 递增保证变化可探测）
NS = {"tick": time.time_ns()}


# ---------------------------------------------------------------------------
# 构造辅助：tmpdir 三真源 + 注入 engine/observer
# ---------------------------------------------------------------------------

def make_world(tmp_path, *, with_spool=True, **kw):
    root = tmp_path / "zcode"
    (root / "rollout").mkdir(parents=True)
    (root / "agents").mkdir(parents=True)
    if with_spool:
        (root / "spool.jsonl").write_text("", encoding="utf-8")  # 预置空文件=首拍武装
    engine = StateEngine("zcode-test", source_kind=SOURCE_ZCODE_OBSERVED)
    observer = zc.ZcodeObserver(
        engine,
        rollout_dir=str(root / "rollout"),
        agents_dir=str(root / "agents"),
        spool_path=str(root / "spool.jsonl") if with_spool else None,
        **kw)
    return root, engine, observer


def rollout_path(root, sid=SID):
    return root / "rollout" / ("model-io-%s.jsonl" % sid)


def append_line(path, text):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def model_io_line(turn="t1", input_tokens=1500, cache_read=450, error=None):
    """合成 model_io 行（字段形状对齐 P3.6 §7；内容全部占位）。"""
    record = {
        "type": "model_io",
        "startedAt": "2026-09-12T10:00:00.000Z",
        "completedAt": "2026-09-12T10:00:00.100Z",
        "turnId": turn,
        "querySource": "main_turn",
        "attempt": 0,
        "durationMs": 100,
        "model": {"modelId": "model-under-test", "providerId": "fixture"},
        "request": {"messages": []},
        "response": {"text": "", "reasoningText": "", "toolCalls": [],
                     "usage": {"inputTokens": input_tokens,
                               "outputTokens": 10, "totalTokens": input_tokens + 10,
                               "cacheReadTokens": cache_read,
                               "cacheWriteTokens": 0},
                     "finishReason": "stop"},
    }
    if error is not None:
        record["error"] = error
    return json.dumps(record, ensure_ascii=False)


def spool_line(event, sid=SID, tool_name=None):
    return json.dumps({"event": event, "session_id": sid,
                       "tool_name": tool_name,
                       "received_at": "2026-09-12T10:00:00.000Z"},
                      ensure_ascii=False)


def bump_mtime(path):
    NS["tick"] += 1_000_000_000
    os.utime(path, ns=(NS["tick"], NS["tick"]))


def write_metadata(root, agent_dir, meta):
    path = root / "agents" / "sess_main" / agent_dir / "metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    bump_mtime(path)
    return path


def thread_of(snap, tid):
    for thread in snap["threads"]:
        if thread["id"] == tid:
            return thread
    return None


def thread_states(snaps, tid):
    """该线程在快照序列中的去重连续状态序列。"""
    out = []
    for snap in snaps:
        thread = thread_of(snap, tid)
        if thread is not None and (not out or out[-1] != thread["state"]):
            out.append(thread["state"])
    return out


def check_all(validator, snaps):
    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, errors[0].message
        assert_invariants(snap)
        assert snap["source"]["kind"] == "zcode_observed"


# ---------------------------------------------------------------------------
# 全流程：idle → working → needs_you → done
# ---------------------------------------------------------------------------

def test_full_flow_idle_working_needs_you_done(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    all_snaps = []

    assert obs.poll_once(1000) == []            # 首拍：武装 spool，无事件
    append_line(root / "spool.jsonl", spool_line("SessionStart"))
    snaps = obs.poll_once(2000)                 # 会话可见：idle
    all_snaps += snaps
    assert thread_states(all_snaps, SID) == ["idle"]

    append_line(rollout_path(root), model_io_line("t1", 1000, 200))
    snaps = obs.poll_once(3000)                 # rollout 新行：working
    all_snaps += snaps
    assert thread_states(all_snaps, SID) == ["idle", "working"]

    append_line(root / "spool.jsonl",
                spool_line("PermissionRequest", tool_name="bash"))
    snaps = obs.poll_once(4000)                 # 等审批：needs_you
    all_snaps += snaps
    assert thread_states(all_snaps, SID) == ["idle", "working", "needs_you"]
    attention = thread_of(all_snaps[-1], SID)["attention"]
    assert attention == {"pending_count": 1, "summary": "bash"}

    append_line(rollout_path(root), model_io_line("t1", 1500, 450))
    append_line(root / "spool.jsonl", spool_line("PreToolUse", tool_name="bash"))
    snaps = obs.poll_once(5000)                 # 放行：撤销等待恢复 working
    all_snaps += snaps
    assert thread_states(all_snaps, SID) == \
        ["idle", "working", "needs_you", "working"]
    recovered = thread_of(all_snaps[-1], SID)
    assert recovered["attention"] is None and recovered["waiting_ms"] == 0

    append_line(root / "spool.jsonl", spool_line("Stop"))
    snaps = obs.poll_once(6000)                 # 回合终结：done
    all_snaps += snaps
    assert thread_states(all_snaps, SID) == \
        ["idle", "working", "needs_you", "working", "done"]

    check_all(validator, all_snaps)
    assert len(all_snaps) == 2 + 4 + 2 + 5 + 3
    final = thread_of(all_snaps[-1], SID)
    assert final["state"] == "done" and final["end_reason"] == "completed"
    assert final["turn_id"] == "t1" and final["activity"] == ""
    assert all_snaps[-1]["selected_thread_id"] == SID  # select_thread=最近活动线程
    # token：最近一次调用 input+cacheRead；容量诚实未知
    assert final["context"] == {"used_tokens": 1950, "capacity_tokens": None,
                                "used_percent": None}
    # 未命中 1308 → 无额度窗口
    assert all_snaps[-1]["usage"]["available"] is False
    assert all_snaps[-1]["usage"]["windows"] == []


# ---------------------------------------------------------------------------
# error 流与 [1308] 窗口
# ---------------------------------------------------------------------------

def test_error_flow_with_scrubbed_summary(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    append_line(rollout_path(root), model_io_line(
        "t-err", error={"name": "TestError",
                        "message": "upstream failed for ops@example.com"}))
    snaps = obs.poll_once(1000)
    check_all(validator, snaps)
    thread = thread_of(snaps[-1], SID)
    assert thread["state"] == "error" and thread["end_reason"] == "failed"
    assert thread["activity"] == "upstream failed for <redacted>"
    blob = json.dumps(snaps, ensure_ascii=False)
    assert "ops@example.com" not in blob

    # 文件活动停滞不产生任何终态/状态事件（不伪造）
    assert obs.poll_once(2000) == []
    assert obs.poll_once(300000) == []


def test_1308_window_reaches_usage_snapshot(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    append_line(rollout_path(root), model_io_line("t-1308", error={
        "name": "UsageLimitError",
        "message": "[1308][已达到 5 小时的使用上限。您的限额将在 2026-09-12 23:01:32 重置。]"}))
    snaps = obs.poll_once(1000)
    check_all(validator, snaps)
    usage = snaps[-1]["usage"]
    assert usage["available"] is True and usage["windows_total"] == 1
    window = usage["windows"][0]
    expected_ms = int(time.mktime(
        time.strptime("2026-09-12 23:01:32", "%Y-%m-%d %H:%M:%S"))) * 1000
    assert window == {"id": "zcode-5h", "label": "ZCODE 5H WINDOW",
                      "used_percent": 100.0, "duration_mins": 300,
                      "resets_at_ms": expected_ms}
    assert thread_of(snaps[-1], SID)["state"] == "error"


def test_1308_with_unparseable_reset_time_is_honest(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    append_line(rollout_path(root), model_io_line("t-1308", error={
        "name": "UsageLimitError",
        "message": "[1308][已达到 5 小时的使用上限。重置时间待定。]"}))
    snaps = obs.poll_once(1000)
    window = snaps[-1]["usage"]["windows"][0]
    assert window["id"] == "zcode-5h" and window["resets_at_ms"] is None


# ---------------------------------------------------------------------------
# 子代理 metadata 生命周期（mtime 变化才重读）+ 子代理 rollout 同池
# ---------------------------------------------------------------------------

def test_subagent_lifecycle_and_shared_pool(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    child = "sess_subagent_agent_fixture01"
    base = json.loads(
        (FIXTURES / "metadata_sample.json").read_text(encoding="utf-8"))

    write_metadata(root, "agent_a1", base)       # running
    snaps = obs.poll_once(1000)
    check_all(validator, snaps)
    thread = thread_of(snaps[-1], child)
    assert thread["state"] == "working"          # thread_started + active
    assert thread["project"] == "zcode-fixture-project"

    write_metadata(root, "agent_a1", dict(      # completed（mtime/size 变化才重读）
        base, status="completed", completedAt="2026-09-12T10:05:00.000Z",
        updatedAt="2026-09-12T10:05:00.000Z"))
    snaps = obs.poll_once(2000)
    assert thread_states(snaps, child) == ["idle"]

    write_metadata(root, "agent_a1", dict(      # failed → error（脱敏摘要）
        base, status="failed", completedAt="2026-09-12T10:06:00.000Z",
        updatedAt="2026-09-12T10:06:00.000Z",
        error="synthetic agent failure for ops@example.com"))
    snaps = obs.poll_once(3000)
    thread = thread_of(snaps[-1], child)
    assert thread["state"] == "error" and thread["end_reason"] == "failed"
    assert thread["activity"] == "synthetic agent failure for <redacted>"

    # 子代理 rollout 文件与 metadata 同池：新 turn 让线程重回 working
    append_line(rollout_path(root, child), model_io_line("t-child", 100, 10))
    snaps = obs.poll_once(4000)
    check_all(validator, snaps)
    thread = thread_of(snaps[-1], child)
    assert thread["state"] == "working" and thread["turn_id"] == "t-child"
    # metadata 未变化的拍不重读：安静拍无事件
    assert obs.poll_once(5000) == []


# ---------------------------------------------------------------------------
# 偏移增量 / 轮转重读
# ---------------------------------------------------------------------------

def test_offset_increment_appends_only_new_events(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    append_line(rollout_path(root), model_io_line("t1", 1000, 200))
    snaps = obs.poll_once(1000)
    check_all(validator, snaps)
    assert len(snaps) == 4                       # turn_started+active+token+select
    assert thread_of(snaps[-1], SID)["elapsed_ms"] == 0

    append_line(rollout_path(root), model_io_line("t1", 1500, 450))
    snaps = obs.poll_once(2000)                  # 同 turn 追加：不重 mint turn_started
    assert len(snaps) == 3                       # active+token+select
    thread = thread_of(snaps[-1], SID)
    assert thread["turn_id"] == "t1"
    assert thread["elapsed_ms"] == 1000          # turn 未重置 → 增量读无重复
    assert thread["context"]["used_tokens"] == 1950

    append_line(rollout_path(root), model_io_line("t2", 10, 0))
    snaps = obs.poll_once(3000)                  # 新 turn：turn_started 重现
    assert len(snaps) == 4
    thread = thread_of(snaps[-1], SID)
    assert thread["turn_id"] == "t2" and thread["elapsed_ms"] == 0


def test_rotation_truncation_rereads_from_zero(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    append_line(rollout_path(root), model_io_line("t1", 1000, 200))
    append_line(rollout_path(root), model_io_line("t1", 1000, 200))
    snaps = obs.poll_once(1000)
    assert thread_of(snaps[-1], SID)["turn_id"] == "t1"

    # 轮转/截断：文件变小（offset>size）→ 从 0 重读，新 turn 正常接管
    with open(rollout_path(root), "w", encoding="utf-8") as fh:
        fh.write(model_io_line("t2", 500, 50) + "\n")
    snaps = obs.poll_once(2000)
    check_all(validator, snaps)
    thread = thread_of(snaps[-1], SID)
    assert thread["turn_id"] == "t2" and thread["state"] == "working"
    assert thread["context"]["used_tokens"] == 550


# ---------------------------------------------------------------------------
# 目录健康：disconnected → reconnected
# ---------------------------------------------------------------------------

def test_rollout_dir_gone_disconnected_then_recovered(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    append_line(rollout_path(root), model_io_line("t1", 1000, 200))
    snaps = obs.poll_once(1000)
    assert snaps[-1]["source"]["connected"] is True

    shutil.rmtree(root / "rollout")
    snaps = obs.poll_once(2000)
    check_all(validator, snaps)
    assert snaps[-1]["source"]["connected"] is False
    assert snaps[-1]["source"]["stale"] is True
    # 断连不改任务状态（不伪造 IDLE/DONE）
    assert thread_of(snaps[-1], SID)["state"] == "working"

    (root / "rollout").mkdir()
    with open(rollout_path(root), "w", encoding="utf-8") as fh:
        fh.write(model_io_line("t1", 1000, 200) + "\n")
    snaps = obs.poll_once(3000)
    check_all(validator, snaps)
    assert snaps[0]["source"]["connected"] is True    # 重连为拍内首个事件
    assert snaps[0]["source"]["stale"] is False
    # 偏移已清理→重读，但 turn 记账保留：不重复 mint turn_started（防复活）
    assert thread_of(snaps[-1], SID)["state"] == "working"
    assert thread_of(snaps[-1], SID)["turn_id"] == "t1"


# ---------------------------------------------------------------------------
# 上限：max_threads 清池 / 协议 8 线程
# ---------------------------------------------------------------------------

def test_max_threads_prunes_oldest_without_events(tmp_path):
    root, engine, obs = make_world(tmp_path, max_threads=2)
    append_line(rollout_path(root, "s1"), model_io_line("t-a", 10, 0))
    assert len(obs.poll_once(1000)) == 4
    append_line(rollout_path(root, "s2"), model_io_line("t-b", 10, 0))
    assert len(obs.poll_once(2000)) == 4
    append_line(rollout_path(root, "s3"), model_io_line("t-c", 10, 0))
    snaps = obs.poll_once(3000)                  # s1 最旧→清池（仅记账，不发事件）
    assert len(snaps) == 4                       # 只有 s3 的事件+select
    assert obs.describe()["tracked_sessions"] == 2
    assert engine.thread_ids() == ["s1", "s2", "s3"]  # 引擎不受清池影响
    assert snaps[-1]["selected_thread_id"] == "s3"

    assert obs.poll_once(4000) == []             # 安静拍：无事件（s1 重发现但不产事件）
    assert obs.describe()["tracked_sessions"] == 2
    assert engine.thread_ids() == ["s1", "s2", "s3"]


def test_protocol_cap_8_blocks_ninth_thread(tmp_path):
    root, engine, obs = make_world(tmp_path)
    for i in range(1, 9):
        append_line(rollout_path(root, "s%d" % i), model_io_line("t%d" % i, 10, 0))
    snaps = obs.poll_once(1000)
    assert len(engine.thread_ids()) == 8

    append_line(rollout_path(root, "s9"), model_io_line("t9", 10, 0))
    assert obs.poll_once(2000) == []             # 第 9 线程：不发任何事件
    assert obs.describe()["engine_threads"] == 8

    append_line(rollout_path(root, "s1"), model_io_line("t1b", 20, 0))
    snaps = obs.poll_once(3000)                  # 已在引擎的线程照常维护
    assert snaps and any(t["id"] == "s1" and t["turn_id"] == "t1b"
                         for t in snaps[-1]["threads"])
    for snap in snaps:
        assert snap["threads_total"] == 8


# ---------------------------------------------------------------------------
# spool：历史不回放 / 禁用时不伪造终态
# ---------------------------------------------------------------------------

def test_spool_history_is_not_replayed(tmp_path):
    root, engine, obs = make_world(tmp_path)
    append_line(root / "spool.jsonl", spool_line("Stop"))  # 观察前已存在的 Stop
    append_line(rollout_path(root), model_io_line("t1", 1000, 200))
    snaps = obs.poll_once(1000)                  # spool 首见=从尾部起读
    assert thread_of(snaps[-1], SID)["state"] == "working"  # 历史 Stop 未回放

    append_line(root / "spool.jsonl", spool_line("Stop"))  # 观察期内的 Stop 生效
    snaps = obs.poll_once(2000)
    assert thread_of(snaps[-1], SID)["state"] == "done"


def test_spool_disabled_never_fabricates_done(tmp_path):
    root, engine, obs = make_world(tmp_path, with_spool=False)
    assert obs.describe()["spool_enabled"] is False
    append_line(rollout_path(root), model_io_line("t1", 1000, 200))
    obs.poll_once(1000)
    # 无 hook 通道 → 无 Stop 可见：文件停滞绝不伪造 done/idle
    assert obs.poll_once(2000) == []
    assert obs.poll_once(300000) == []
    assert obs.poll_once(300001) == []


# ---------------------------------------------------------------------------
# 观测元数据：stale 只进报告，绝不映射状态事件
# ---------------------------------------------------------------------------

def test_stale_after_is_metadata_only(tmp_path):
    root, engine, obs = make_world(tmp_path, stale_after_s=100)
    append_line(rollout_path(root), model_io_line("t1", 1000, 200))
    obs.poll_once(1000)
    assert obs.stale_session_ids(101_000) == []      # 100s 内不算停滞
    assert obs.stale_session_ids(101_001) == [SID]   # 超 TTL→观测 stale
    # 停滞拍：无事件（不伪造终态），任务保持 working
    assert obs.poll_once(300_000) == []
    assert obs.describe()["tracked_sessions"] == 1


# ---------------------------------------------------------------------------
# 坏行容错
# ---------------------------------------------------------------------------

def test_bad_lines_skipped_and_counted(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    append_line(rollout_path(root), model_io_line("t1", 1000, 200))
    append_line(rollout_path(root), "not-json-at-all")
    append_line(rollout_path(root), "[1,2,3]")
    snaps = obs.poll_once(1000)
    check_all(validator, snaps)
    assert thread_of(snaps[-1], SID)["state"] == "working"
    assert obs.bad_lines == 2

    append_line(root / "spool.jsonl", spool_line("SessionStart"))
    append_line(root / "spool.jsonl", '{"event":"Stop"')          # 坏 JSON
    append_line(root / "spool.jsonl", '{"event":"Stop"}')         # 缺 session_id
    append_line(root / "spool.jsonl", spool_line("NotAHookEvent"))
    snaps = obs.poll_once(2000)
    assert obs.describe()["bad_lines"] == 5
    assert len(snaps) == 2                        # thread_started + select
    assert obs.poll_once(3000) == []


# ---------------------------------------------------------------------------
# 冷启动尾巴上限
# ---------------------------------------------------------------------------

def test_cold_start_replay_is_tail_capped(tmp_path):
    root, engine, obs = make_world(tmp_path)
    for i in range(1, 251):                       # 观察前已有 250 条历史
        append_line(rollout_path(root), model_io_line("t1", i, 0))
    snaps = obs.poll_once(1000)
    # 首拍回放 ≤ COLD_START_MAX_RECORDS 条：1 条 mint turn(3 事件)，
    # 其余各 2 事件，末尾 select。
    expected = 3 + 2 * (zc.COLD_START_MAX_RECORDS - 1) + 1
    assert len(snaps) == expected
    assert thread_of(snaps[-1], SID)["context"]["used_tokens"] == 250
    assert thread_of(snaps[-1], SID)["state"] == "working"


# ---------------------------------------------------------------------------
# run_forever：快照逐份回调；安静拍不回调
# ---------------------------------------------------------------------------

def test_run_forever_relays_snapshots_and_skips_quiet_ticks(tmp_path):
    class _ClockStop(Exception):
        pass

    root, engine, obs = make_world(tmp_path, poll_interval_s=0)
    assert obs.poll_once(1000) == []              # 先武装 spool（空文件）

    append_line(root / "spool.jsonl", spool_line("SessionStart"))
    ticks = iter([2000.0, 3000.0])
    received = []
    wrote = {"done": False}

    def sink(snap):
        received.append(snap)
        # 第二拍产出首批快照后写入 rollout，驱动第三拍
        if len(received) >= 2 and not wrote["done"]:
            wrote["done"] = True
            append_line(rollout_path(root), model_io_line("t1", 1000, 200))

    def monotonic_fn():
        try:
            return next(ticks)
        except StopIteration:
            raise _ClockStop()

    with pytest.raises(_ClockStop):
        obs.run_forever(sink, monotonic_fn)

    assert len(received) == 6                     # 2（idle 拍）+ 4（working 拍）
    assert [s["seq"] for s in received] == [1, 2, 3, 4, 5, 6]
    assert thread_states(received, SID) == ["idle", "working"]


# ---------------------------------------------------------------------------
# lookback：旧 rollout 文件不跟踪
# ---------------------------------------------------------------------------

def test_lookback_excludes_stale_files(tmp_path):
    root, engine, obs = make_world(tmp_path, lookback_s=86400.0)
    append_line(rollout_path(root, "old"), model_io_line("t-old", 10, 0))
    old_stamp = time.time() - 90000.0
    os.utime(rollout_path(root, "old"), (old_stamp, old_stamp))
    assert obs.poll_once(1000) == []
    assert obs.describe()["tracked_sessions"] == 0

    append_line(rollout_path(root, "new"), model_io_line("t-new", 10, 0))
    snaps = obs.poll_once(2000)
    assert any(t["id"] == "new" for t in snaps[-1]["threads"])


# ---------------------------------------------------------------------------
# 仓库 fixtures 冒烟：三份合成样例走通观察器 + 红线扫描
# ---------------------------------------------------------------------------

def test_repo_fixtures_smoke_with_redline_scan(validator, tmp_path):
    root, engine, obs = make_world(tmp_path)
    shutil.copy(FIXTURES / "rollout_sample.jsonl",
                rollout_path(root, "sess_fixture_main"))
    meta_dst = (root / "agents" / "sess_fixture_main" / "agent_fixture01"
                / "metadata.json")
    meta_dst.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "metadata_sample.json", meta_dst)

    snaps = obs.poll_once(1000)                   # rollout 回放（≤尾巴上限）+metadata
    check_all(validator, snaps)
    main = "sess_fixture_main"
    child = "sess_subagent_agent_fixture01"
    assert thread_of(snaps[-1], main)["state"] == "error"    # 1308 记录收尾
    assert thread_of(snaps[-1], child)["state"] == "working"

    append_line(root / "spool.jsonl",
                (FIXTURES / "spool_sample.jsonl").read_text(encoding="utf-8"))
    completed = json.loads((FIXTURES / "metadata_completed_sample.json")
                           .read_text(encoding="utf-8"))
    write_metadata(root, "agent_fixture01", completed)
    snaps = obs.poll_once(2000)
    all_snaps = snaps
    check_all(validator, all_snaps)

    assert obs.describe()["bad_lines"] == 3       # 坏 JSON + 未知事件 + 缺 session_id
    usage = all_snaps[-1]["usage"]
    assert usage["available"] is True
    assert usage["windows"][0]["id"] == "zcode-5h"
    ids = {t["id"] for t in all_snaps[-1]["threads"]}
    assert {main, child, "sess_fixture_other"} <= ids   # Stop 合成 done 线程
    assert thread_of(all_snaps[-1], child)["state"] == "idle"

    # 红线：提示词/回复/工具输入占位标记与真实感邮箱绝不进入任何快照
    blob = json.dumps(all_snaps, ensure_ascii=False)
    for marker in ("SYNTHETIC-PROMPT-MARKER", "SYNTHETIC-REPLY-MARKER",
                   "SYNTHETIC-TOOL-INPUT-MARKER", "ops@example.com"):
        assert marker not in blob
