"""ZC1 离线单测：ZCode 事件映射器（bridge/sources/zcode.py::ZcodeEventMapper + 纯函数）。

策略：纯内存、零 IO、零真实 ~/.zcode 访问。规则真源 =
docs/P3.6_DESKTOP_OBSERVATION.md §7/§7.1 + ZC1 任务映射表。覆盖：

- working：新行 → active；turnId 首次出现先 turn_started；usage → token
  （used=inputTokens+cacheReadTokens，capacity=None）；
- Stop → turn_completed(last|"stop", completed) + idle；
- needs_you：PermissionRequest → 合成负数审批（summary 优先 spool 审批内容
  摘要 [ZC3]，scrub ≤192 字节；无摘要回退工具名，绝无 tool_input 原文）；
  PreToolUse/PostToolUse/新 turn → server_request_resolved 撤销；
- error → turn_completed(failed, 脱敏摘要)；[1308] → 5H 窗口（重置时刻按
  本地时区解析；解析失败 → resets_at_ms=None）；
- 子代理 metadata 生命周期；ISO 解析（含时区）；fixtures 文件本体可解析。

数据全部合成；fixtures 中的提示词/回复/工具输入是占位标记
（SYNTHETIC-*-MARKER），单测同时断言它们绝不进入任何事件。
"""

from __future__ import annotations

import calendar
import json
import pathlib
import time

import pytest

from bridge import events as ev
from bridge.sources import zcode as zc
from conftest import FrozenZcodeWallClock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "zcode"

SID = "sess_mapper_main"


def load_fixture_records():
    records = []
    for line in (FIXTURES / "rollout_sample.jsonl").read_text(
            encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def model_io(turn_id="t1", started="2026-09-12T10:00:00.000Z", error=None,
             usage=None):
    record = {
        "type": "model_io",
        "startedAt": started,
        "completedAt": "2026-09-12T10:00:00.100Z",
        "turnId": turn_id,
        "querySource": "main_turn",
        "attempt": 0,
        "durationMs": 100,
        "model": {"modelId": "model-under-test", "providerId": "fixture"},
        "request": {"messages": []},
        "response": {"text": "", "reasoningText": "", "toolCalls": [],
                     "usage": usage if usage is not None
                     else {"inputTokens": 100, "outputTokens": 10,
                           "totalTokens": 110, "cacheReadTokens": 20,
                           "cacheWriteTokens": 0},
                     "finishReason": "stop"},
    }
    if error is not None:
        record["error"] = error
    return record


@pytest.fixture()
def mapper():
    return zc.ZcodeEventMapper()


# ---------------------------------------------------------------------------
# working / token
# ---------------------------------------------------------------------------

def test_first_record_mints_turn_started_active_token(mapper):
    events = mapper.rollout_record(SID, model_io(turn_id="t1"))
    assert [e.type for e in events] == [
        ev.EVENT_TURN_STARTED, ev.EVENT_THREAD_STATUS, ev.EVENT_TOKEN_USAGE,
        ev.EVENT_MODEL_INFO, ev.EVENT_TOKEN_TOTALS]
    assert events[0].thread_id == SID and events[0].turn_id == "t1"
    assert events[1].status == ev.THREAD_STATUS_ACTIVE
    # token：used=inputTokens+cacheReadTokens；ZCode 无容量事实 → capacity=None
    assert events[2].used_tokens == 100 + 20
    assert events[2].capacity_tokens is None
    # v1.2 DETAILS 数据源：模型标签 + 会话累计（首条即累计值）
    assert events[3].model == "model-under-test"
    assert (events[4].input_tokens, events[4].output_tokens,
            events[4].cached_tokens) == (100, 10, 20)
    # at_ms：startedAt 的 UTC 毫秒（纯函数换算）
    assert events[0].at_ms == (calendar.timegm((2026, 9, 12, 10, 0, 0)) * 1000)


def test_second_record_same_turn_has_no_turn_started(mapper):
    mapper.rollout_record(SID, model_io(turn_id="t1"))
    events = mapper.rollout_record(SID, model_io(turn_id="t1"))
    # model 不变不再重发；token_totals 持续累计（100+100 / 10+10 / 20+20）
    assert [e.type for e in events] == [ev.EVENT_THREAD_STATUS, ev.EVENT_TOKEN_USAGE,
                                        ev.EVENT_TOKEN_TOTALS]
    totals = events[-1]
    assert (totals.input_tokens, totals.output_tokens,
            totals.cached_tokens) == (200, 20, 40)


def test_usage_missing_or_partial_does_not_mint_token_event(mapper):
    no_usage = model_io()
    no_usage["response"]["usage"] = None
    assert [e.type for e in mapper.rollout_record(SID, no_usage)] == [
        ev.EVENT_TURN_STARTED, ev.EVENT_THREAD_STATUS, ev.EVENT_MODEL_INFO]
    bad = model_io(turn_id="t1")  # 同 turn：只差 token 事件的有无
    bad["response"]["usage"] = {"outputTokens": 5}  # inputTokens 缺失不硬凑
    # 部分字段缺失 → 累计只记有值项（in/cached 诚实为 None，out=5）
    events = mapper.rollout_record(SID, bad)
    assert [e.type for e in events] == [ev.EVENT_THREAD_STATUS, ev.EVENT_TOKEN_TOTALS]
    assert (events[-1].input_tokens, events[-1].output_tokens,
            events[-1].cached_tokens) == (None, 5, None)


def test_model_change_reemits_and_variant_appended(mapper):
    """同会话换模型 → model_info 重发；variant 追加为 thought 标注。"""
    events = mapper.rollout_record(SID, model_io(turn_id="t1"))
    assert events[-2].model == "model-under-test"
    changed = model_io(turn_id="t1")
    changed["model"] = {"modelId": "GLM-5.3", "providerId": "fixture",
                        "variant": "max"}
    events = mapper.rollout_record(SID, changed)
    assert ev.EVENT_MODEL_INFO in [e.type for e in events]
    info = [e for e in events if e.type == ev.EVENT_MODEL_INFO][0]
    assert info.model == "GLM-5.3 (max)"


def test_non_model_io_record_ignored_and_counted(mapper):
    assert mapper.rollout_record(SID, {"type": "other"}) == []
    assert mapper.rollout_record(SID, ["not", "a", "dict"]) == []
    # 非 dict 行在读到 type 字段前即被丢弃，只计入 type 不符的 dict 记录
    assert mapper.non_model_io == 1


# ---------------------------------------------------------------------------
# done/idle：Stop
# ---------------------------------------------------------------------------

def test_stop_completes_last_turn_and_goes_idle(mapper):
    mapper.rollout_record(SID, model_io(turn_id="t1"))
    events = mapper.spool_event(zc.SPOOL_STOP, SID, None)
    assert [e.type for e in events] == [
        ev.EVENT_TURN_COMPLETED, ev.EVENT_THREAD_STATUS]
    assert events[0].turn_id == "t1"
    assert events[0].status == ev.TURN_STATUS_COMPLETED
    assert events[1].status == ev.THREAD_STATUS_IDLE


def test_stop_without_any_turn_uses_placeholder_stop(mapper):
    events = mapper.spool_event(zc.SPOOL_STOP, SID, None)
    assert events[0].type == ev.EVENT_TURN_COMPLETED
    assert events[0].turn_id == zc.STOP_TURN_PLACEHOLDER
    assert events[0].status == ev.TURN_STATUS_COMPLETED
    assert events[1].status == ev.THREAD_STATUS_IDLE


def test_post_tool_use_failure_mints_no_state_events(mapper):
    # 工具失败≠整轮失败（error 由 rollout_record 合流），只作活动记账。
    assert mapper.spool_event(zc.SPOOL_POST_TOOL_USE_FAILURE, SID, "bash") == []


def test_user_prompt_submit_mints_synthetic_turn_start(mapper):
    """真机裁决 2026-09-12：rollout 的 model_io 行要到调用完成才落盘，
    "提交后思考"阶段文件静默——提交即合成新 turn 起点 + THINKING 相位，
    屏幕才能立刻从上一轮 done 翻回（思考中）而不是停在 done。"""
    e1 = mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None)
    e2 = mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None)
    assert [e.type for e in e1] == [ev.EVENT_TURN_STARTED, ev.EVENT_ITEM_STARTED]
    assert e1[0].turn_id == "prompt-1"
    assert e1[1].turn_id == "prompt-1"
    assert e1[1].item_kind == "reasoning"  # reducer 唯一放行 THINKING 的路径
    assert e2[0].turn_id == "prompt-2"  # 递增保证与 reducer 当前 turn 必然不同


def test_done_then_user_prompt_flips_to_thinking_then_working(mapper):
    """done → 用户提交 → thinking（思考相位）→ 真实 turnId 首见 → working；
    Stop 以真实 id 归结回 done。"""
    from bridge.state.engine import StateEngine, SOURCE_ZCODE_OBSERVED
    eng = StateEngine("t-prompt", source_kind=SOURCE_ZCODE_OBSERVED)
    ticks = iter(range(1000, 1000000, 10))

    def flow(events):
        snap = None
        for event in events:
            snap = eng.apply(event, next(ticks))
        return snap

    snap = flow(mapper.spool_event(zc.SPOOL_SESSION_START, SID, None))
    snap = flow(mapper.rollout_record(SID, model_io(turn_id="t-real")))
    snap = flow(mapper.spool_event(zc.SPOOL_STOP, SID, None))
    assert snap["threads"][0]["state"] == "done"

    snap = flow(mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None))
    assert snap["threads"][0]["state"] == "thinking"

    # 真实 turn 首见（新 turnId）→ 模型调用已完成、工作可观察 → working；
    # Stop 以真实 id 归结 → done。
    snap = flow(mapper.rollout_record(SID, model_io(turn_id="t-real-2")))
    assert snap["threads"][0]["state"] == "working"
    snap = flow(mapper.spool_event(zc.SPOOL_STOP, SID, None))
    assert snap["threads"][0]["state"] == "done"
    assert snap["threads"][0]["turn_id"] == "t-real-2"


def test_post_tool_use_maps_thinking_pretool_maps_working(mapper):
    """工具结果返回后模型进入下一轮处理 → THINKING；工具开始执行 → WORKING。"""
    flow = mapper.spool_event
    flow(zc.SPOOL_SESSION_START, SID, None)
    flow(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None)
    events = flow(zc.SPOOL_POST_TOOL_USE, SID, "bash")
    assert [e.type for e in events] == [ev.EVENT_ITEM_STARTED]
    assert events[0].item_kind == "reasoning"
    events = flow(zc.SPOOL_PRE_TOOL_USE, SID, "bash")
    assert [e.type for e in events] == [ev.EVENT_THREAD_STATUS]
    assert events[0].status == ev.THREAD_STATUS_ACTIVE


def test_session_start_mints_thread_started_once(mapper):
    events = mapper.spool_event(zc.SPOOL_SESSION_START, SID, None)
    assert [e.type for e in events] == [ev.EVENT_THREAD_STARTED]
    assert events[0].thread_id == SID
    assert events[0].project == ""  # 主会话无 cwd 事实（旧四字段行），诚实留空
    assert mapper.spool_event(zc.SPOOL_SESSION_START, SID, None) == []


# ---------------------------------------------------------------------------
# ZC3：SessionStart cwd → project（放行裁决 2026-09-12）
# ---------------------------------------------------------------------------

def test_session_start_with_cwd_sets_project_basename(mapper):
    events = mapper.spool_event(zc.SPOOL_SESSION_START, SID, None,
                                cwd="/Users/me/dev/my proj/")
    assert [e.type for e in events] == [ev.EVENT_THREAD_STARTED]
    assert events[0].project == "my proj"  # basename（尾斜杠容忍）+ 脱敏


def test_session_start_cwd_long_basename_clamped_codepoint_safe(mapper):
    """>96 字节的 basename 按 scrub_text 字节上限截断，且不劈开多字节字符。"""
    long_name = "项" * 80  # 240 UTF-8 字节
    events = mapper.spool_event(zc.SPOOL_SESSION_START, SID, None,
                                cwd="/x/" + long_name)
    project = events[0].project
    assert len(project.encode("utf-8")) <= 96
    project.encode("utf-8").decode("utf-8")  # 码点安全（可完整解码）


def test_session_start_cwd_stored_per_session(mapper):
    """cwd 记入 per-session 记账（供后续取用）；坏类型不覆盖。"""
    mapper.spool_event(zc.SPOOL_SESSION_START, SID, None, cwd="/a/b")
    assert mapper._state(SID).cwd == "/a/b"
    mapper.spool_event(zc.SPOOL_SESSION_START, SID, None, cwd=123)  # 去重+坏类型
    assert mapper._state(SID).cwd == "/a/b"


@pytest.mark.parametrize("bad_cwd", [None, "", "   ", 42])
def test_session_start_bad_cwd_project_empty(mapper, bad_cwd):
    events = mapper.spool_event(zc.SPOOL_SESSION_START, SID, None, cwd=bad_cwd)
    assert events[0].project == ""  # 无 cwd 事实不编造


# ---------------------------------------------------------------------------
# ZC3：TodoWrite plan → plan_updated（唯一放行 tool_input 的工具）
# ---------------------------------------------------------------------------

PLAN = {"total": 3, "steps": [
    {"text": "step one", "status": "completed"},
    {"text": "step two", "status": "in_progress"},
    {"text": "step three", "status": "pending"},
], "truncated": False}


def test_todowrite_plan_maps_plan_updated_on_current_turn(mapper):
    """提交（合成 turn）→ TodoWrite：plan_updated 挂在最近 turn 上；
    PreToolUse 本体的 active 语义不变。"""
    mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None)  # prompt-1
    events = mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "TodoWrite",
                                plan=PLAN)
    assert [e.type for e in events] == [
        ev.EVENT_THREAD_STATUS, ev.EVENT_PLAN_UPDATED]
    assert events[0].status == ev.THREAD_STATUS_ACTIVE
    pe = events[1]
    assert pe.turn_id == "prompt-1"
    assert pe.plan_steps == (("step one", "completed"),
                             ("step two", "in_progress"),
                             ("step three", "pending"))
    assert pe.plan_total == 3


def test_todowrite_plan_with_real_rollout_turn(mapper):
    mapper.rollout_record(SID, model_io(turn_id="t9"))
    events = mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "TodoWrite",
                                plan=PLAN)
    assert events[-1].turn_id == "t9"


def test_plan_steps_scrubbed_and_status_defensed(mapper):
    """观察器侧再过一次 scrub（邮箱/凭证/home 路径）；status 防御性归一化；
    坏 step（非 dict/空文本/坏类型）跳过。"""
    plan = {"total": 5, "steps": [
        {"text": "fix ops@example.com login", "status": "pending"},
        {"text": "cache from /Users/cui/secrets dir", "status": "weird"},
        {"text": 42, "status": "pending"},   # 坏类型：跳过
        {"text": "   ", "status": "pending"},  # 空文本：跳过
        "not-a-dict",                          # 坏 step：跳过
    ]}
    mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None)
    events = mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "TodoWrite",
                                plan=plan)
    pe = events[1]
    assert pe.plan_steps == (("fix <redacted> login", "pending"),
                             ("cache from ~/secrets dir", "pending"))
    blob = json.dumps([e.to_dict() for e in events], ensure_ascii=False)
    assert "ops@example.com" not in blob and "/Users/cui" not in blob


def test_plan_bad_total_falls_back_to_none(mapper):
    mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None)
    for bad_total in ("x", None, True, -3, 2.5):
        events = mapper.spool_event(
            zc.SPOOL_PRE_TOOL_USE, SID, "TodoWrite",
            plan={"total": bad_total, "steps": PLAN["steps"]})
        assert events[-1].plan_total is None  # 坏 total 不硬凑（reducer 回退）


def test_plan_without_turn_dropped_and_counted(mapper):
    """st.last_turn=None（未见过任何 turn）：plan 丢弃（reducer 会拒无 turn
    事件），计数进 plan_dropped_no_turn；active 语义不受影响；有 turn 后恢复。"""
    events = mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "TodoWrite",
                                plan=PLAN)
    assert [e.type for e in events] == [ev.EVENT_THREAD_STATUS]
    assert mapper.plan_dropped_no_turn == 1
    mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None)
    events = mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "TodoWrite",
                                plan=PLAN)
    assert any(e.type == ev.EVENT_PLAN_UPDATED for e in events)
    assert mapper.plan_dropped_no_turn == 1  # 不重复计数


@pytest.mark.parametrize("bad_plan", [None, "x", 42, {}, {"steps": "nope"},
                                      {"steps": []}, {"steps": [{"text": ""}]}])
def test_plan_bad_shapes_ignored_without_counting(mapper, bad_plan):
    mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None)
    events = mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "TodoWrite",
                                plan=bad_plan)
    assert all(e.type != ev.EVENT_PLAN_UPDATED for e in events)
    assert mapper.plan_dropped_no_turn == 0  # 坏数据≠"有 plan 被丢"


def test_plan_does_not_change_state_or_needs_you(mapper):
    """plan 是内容事件：不改 working/needs_you 状态（reducer 语义；此处在
    mapper+engine 全流上验证 TodoWrite 行为与其他 PreToolUse 完全一致）。"""
    from bridge.state.engine import StateEngine, SOURCE_ZCODE_OBSERVED
    eng = StateEngine("t-plan", source_kind=SOURCE_ZCODE_OBSERVED)
    ticks = iter(range(1000, 1000000, 10))

    def flow(events):
        snap = None
        for event in events:
            snap = eng.apply(event, next(ticks))
        return snap

    # needs_you 期间：TodoWrite 的 plan_updated 不抢状态（等待优先）。
    flow(mapper.spool_event(zc.SPOOL_SESSION_START, SID, None,
                            cwd="/home/me/cdt"))
    flow(mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None))
    events = mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "TodoWrite",
                                plan=PLAN)
    approval = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, "bash")[0]
    snap = flow([approval])
    assert snap["threads"][0]["state"] == "needs_you"
    # 只应用 plan_updated（跳过 PreToolUse 的 resolved+active——真实流里它们
    # 会撤销等待，这里隔离验证 plan 本身无状态副作用）。
    snap = flow([events[-1]])
    assert snap["threads"][0]["state"] == "needs_you"
    assert snap["threads"][0]["plan"]["total"] == 3
    assert snap["threads"][0]["plan"]["steps"][0]["text"] == "step one"
    # 等待撤销后（真实 PreToolUse 流）→ working，PLAN 页仍在。
    snap = flow(mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "bash"))
    assert snap["threads"][0]["state"] == "working"
    assert snap["threads"][0]["plan"]["total"] == 3


def test_plan_full_flow_snapshot_project_and_page(mapper):
    """真机目标场景端到端：主会话 project（旧版为空）+ PLAN 页有内容。"""
    from bridge.state.engine import StateEngine, SOURCE_ZCODE_OBSERVED
    eng = StateEngine("t-plan2", source_kind=SOURCE_ZCODE_OBSERVED)
    ticks = iter(range(1000, 1000000, 10))

    def flow(events):
        snap = None
        for event in events:
            snap = eng.apply(event, next(ticks))
        return snap

    snap = flow(mapper.spool_event(zc.SPOOL_SESSION_START, SID, None,
                                   cwd="/Users/me/dev/codex-desk-terminal"))
    assert snap["threads"][0]["project"] == "codex-desk-terminal"
    flow(mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None))
    big_plan = {"total": 12,
                "steps": [{"text": "s%d" % i, "status": "pending"}
                          for i in range(9)],   # >8：reducer/render 侧截断
                "truncated": True}
    snap = flow(mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "TodoWrite",
                                   plan=big_plan))
    page = snap["threads"][0]["plan"]
    assert page["total"] == 12
    assert page["truncated"] is True  # render：steps 条数 > MAX_PLAN_STEPS(8)
    assert len(page["steps"]) == 8
    snap = flow(mapper.spool_event(zc.SPOOL_STOP, SID, None))
    assert snap["threads"][0]["state"] == "done"  # 状态流不变


# ---------------------------------------------------------------------------
# needs_you：置起与撤销
# ---------------------------------------------------------------------------

def test_permission_request_synthesizes_negative_approval_with_tool_name(mapper):
    events = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, "bash")
    assert [e.type for e in events] == [ev.EVENT_APPROVAL_REQUESTED]
    assert events[0].request_id < 0
    assert events[0].summary == "bash"  # 只有工具名，绝无 tool_input 内容


def test_synthetic_request_ids_are_distinct_and_negative(mapper):
    first = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, "bash")[0]
    second = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, "edit")[0]
    assert first.request_id < 0 and second.request_id < 0
    assert first.request_id != second.request_id


def test_pretooluse_resolves_pending_then_active(mapper):
    approval = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, "bash")[0]
    events = mapper.spool_event(zc.SPOOL_PRE_TOOL_USE, SID, "bash")
    assert [e.type for e in events] == [
        ev.EVENT_SERVER_REQUEST_RESOLVED, ev.EVENT_THREAD_STATUS]
    assert events[0].request_id == approval.request_id
    assert events[1].status == ev.THREAD_STATUS_ACTIVE
    # 再次 PostToolUse：无 pending 可撤，只剩合成 reasoning（THINKING 相位）
    events2 = mapper.spool_event(zc.SPOOL_POST_TOOL_USE, SID, "bash")
    assert [e.type for e in events2] == [ev.EVENT_ITEM_STARTED]
    assert events2[0].item_kind == "reasoning"


def test_stop_resolves_pending_approval(mapper):
    approval = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, "bash")[0]
    events = mapper.spool_event(zc.SPOOL_STOP, SID, None)
    assert events[0].type == ev.EVENT_SERVER_REQUEST_RESOLVED
    assert events[0].request_id == approval.request_id
    assert [e.type for e in events][1:] == [
        ev.EVENT_TURN_COMPLETED, ev.EVENT_THREAD_STATUS]


def test_new_turn_resolves_pending_before_turn_started(mapper):
    approval = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, "bash")[0]
    events = mapper.rollout_record(SID, model_io(turn_id="t-new"))
    assert events[0].type == ev.EVENT_SERVER_REQUEST_RESOLVED
    assert events[0].request_id == approval.request_id
    assert events[1].type == ev.EVENT_TURN_STARTED
    assert events[1].turn_id == "t-new"


# ---------------------------------------------------------------------------
# ZC3：PermissionRequest 审批内容摘要 → needs_you（放行裁决 2026-09-12）
# ---------------------------------------------------------------------------

def test_permission_request_prefers_spool_summary_over_tool_name(mapper):
    """对照效果图：NEEDS YOU 页显示真实审批内容（命令）而非裸工具名。"""
    events = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, "Bash",
                                summary="git push origin main")
    assert [e.type for e in events] == [ev.EVENT_APPROVAL_REQUESTED]
    assert events[0].request_id < 0
    assert events[0].summary == "git push origin main"


def test_permission_request_summary_scrubbed_and_clamped_codepoint_safe(mapper):
    """>192 字节摘要按 scrub_text 字节上限截断，且经脱敏、码点安全。"""
    events = mapper.spool_event(
        zc.SPOOL_PERMISSION_REQUEST, SID, "Bash",
        summary="git commit -m 'contact me me@example.com ' " + "汉" * 100)
    summary = events[0].summary
    assert len(summary.encode("utf-8")) <= 192
    assert "me@example.com" not in summary  # scrub_text 清洗邮箱
    summary.encode("utf-8").decode("utf-8")  # 码点安全（可完整解码）


@pytest.mark.parametrize("bad_summary", [None, "", "   ", 42, ["git"]])
def test_permission_request_missing_summary_falls_back_to_tool_name(
        mapper, bad_summary):
    """旧格式行/坏类型/空白摘要 → 回退工具名（现状行为零回归）。"""
    events = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, "bash",
                                summary=bad_summary)
    assert events[0].summary == "bash"


def test_permission_request_summary_whitespace_fallback_no_tool_name(mapper):
    """无摘要且无工具名 → 占位文案（现状兜底）。"""
    events = mapper.spool_event(zc.SPOOL_PERMISSION_REQUEST, SID, None,
                                summary=None)
    assert events[0].summary == "等待审批"


# ---------------------------------------------------------------------------
# error / [1308] 窗口
# ---------------------------------------------------------------------------

def test_error_record_maps_failed_with_scrubbed_summary(mapper):
    events = mapper.rollout_record(SID, model_io(
        turn_id="t-err",
        error={"name": "TestError", "message": "failed for ops@example.com",
               "stack": "x"}))
    completed = [e for e in events if e.type == ev.EVENT_TURN_COMPLETED][0]
    assert completed.status == ev.TURN_STATUS_FAILED
    assert completed.turn_id == "t-err"
    assert completed.summary == "failed for <redacted>"  # scrub_text 清洗邮箱
    assert "ops@example.com" not in json.dumps([e.to_dict() for e in events])


def test_error_without_1308_mints_no_rate_window(mapper):
    events = mapper.rollout_record(SID, model_io(
        turn_id="t-err", error={"name": "E", "message": "plain failure"}))
    assert all(e.type != ev.EVENT_RATE_LIMITS for e in events)


def test_1308_record_mints_5h_window_with_local_reset_time(mapper):
    # 重置时刻动态取"未来 1 小时"（ZC3 时间炸弹修复：原用例硬编码
    # "2026-09-12 23:01:32"，当天 23:01 后过期窗口抑制生效 → 永远假红）。
    reset = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 3600))
    message = f"[1308][已达到 5 小时的使用上限。您的限额将在 {reset} 重置。]"
    events = mapper.rollout_record(SID, model_io(
        turn_id="t-1308", error={"name": "UsageLimitError", "message": message}))
    windows = [e for e in events if e.type == ev.EVENT_RATE_LIMITS]
    assert len(windows) == 1  # 只在真实命中 [1308] 时发，且只发一次
    window = windows[0].windows[0]
    expected_ms = int(time.mktime(time.strptime(reset, "%Y-%m-%d %H:%M:%S"))) * 1000
    assert window == {"id": "zcode-5h", "label": "ZCODE 5H WINDOW",
                      "used_percent": 100, "duration_mins": 300,
                      "resets_at_ms": expected_ms}
    # error 本体仍归结为 failed turn
    completed = [e for e in events if e.type == ev.EVENT_TURN_COMPLETED][0]
    assert completed.status == ev.TURN_STATUS_FAILED


def test_1308_without_parseable_reset_time_yields_none(mapper):
    events = mapper.rollout_record(SID, model_io(
        turn_id="t-1308",
        error={"name": "UsageLimitError",
               "message": "[1308][已达到 5 小时的使用上限。重置时间待定。]"}))
    window = [e for e in events if e.type == ev.EVENT_RATE_LIMITS][0].windows[0]
    assert window["resets_at_ms"] is None  # 解析失败 → None，绝不编造


def test_1308_expired_reset_time_suppresses_window(mapper):
    """A0 裁决（真机首跑发现）：重置时刻已过的 1308 是历史事实，不是当前
    占用——冷启动回放尾巴里的旧限额错误不得让 USAGE 页永远显示 100%。"""
    past = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
    events = mapper.rollout_record(SID, model_io(
        turn_id="t-1308-old",
        error={"name": "UsageLimitError",
               "message": f"[1308][已达到 5 小时的使用上限。您的限额将在 {past} 重置。]"}))
    assert all(e.type != ev.EVENT_RATE_LIMITS for e in events)
    # failed turn 本体仍如实归结（历史错误不失真）
    completed = [e for e in events if e.type == ev.EVENT_TURN_COMPLETED][0]
    assert completed.status == ev.TURN_STATUS_FAILED


# ---------------------------------------------------------------------------
# 子代理 metadata
# ---------------------------------------------------------------------------

def test_agent_running_maps_thread_started_then_active(mapper):
    meta = json.loads((FIXTURES / "metadata_sample.json").read_text(
        encoding="utf-8"))
    events = mapper.agent_update(meta["childSessionId"], meta)
    assert [e.type for e in events] == [
        ev.EVENT_THREAD_STARTED, ev.EVENT_THREAD_STATUS]
    assert events[0].thread_id == "sess_subagent_agent_fixture01"
    assert events[0].project == "zcode-fixture-project"  # basename(cwd)
    assert events[1].status == ev.THREAD_STATUS_ACTIVE


def test_agent_completed_and_stopped_go_idle(mapper):
    child = "sess_subagent_agent_fixture01"
    running = json.loads((FIXTURES / "metadata_sample.json").read_text(
        encoding="utf-8"))
    mapper.agent_update(child, running)
    completed = json.loads((FIXTURES / "metadata_completed_sample.json")
                           .read_text(encoding="utf-8"))
    events = mapper.agent_update(child, completed)
    assert [e.type for e in events] == [ev.EVENT_THREAD_STATUS]
    assert events[0].status == ev.THREAD_STATUS_IDLE
    stopped = dict(completed, status="stopped")
    events = mapper.agent_update(child, stopped)
    assert events[0].status == ev.THREAD_STATUS_IDLE


def test_agent_failed_maps_failed_turn_with_scrubbed_summary(mapper):
    child = "sess_subagent_agent_fixture02"
    failed = json.loads((FIXTURES / "metadata_failed_sample.json").read_text(
        encoding="utf-8"))
    events = mapper.agent_update(child, failed)
    completed = [e for e in events if e.type == ev.EVENT_TURN_COMPLETED][0]
    assert completed.status == ev.TURN_STATUS_FAILED
    assert completed.thread_id == child
    assert completed.summary == "synthetic agent failure for <redacted>"
    assert "ops@example.com" not in json.dumps([e.to_dict() for e in events])


def test_agent_failed_prefers_last_rollout_turn_for_gate(mapper):
    # 子代理 rollout 已见过 turn → failed 的 turn_id 用它，保证通过 reducer 门闸
    child = "sess_subagent_agent_fixture01"
    mapper.rollout_record(child, model_io(turn_id="t-child"))
    failed = json.loads((FIXTURES / "metadata_failed_sample.json").read_text(
        encoding="utf-8"))
    failed = dict(failed, childSessionId=child, error="boom")
    events = mapper.agent_update(child, failed)
    completed = [e for e in events if e.type == ev.EVENT_TURN_COMPLETED][0]
    assert completed.turn_id == "t-child"


def test_meta_is_terminal_pure_predicate():
    """终态判定（mapper 生命周期与观察器门闸共用，口径一致）。"""
    terminal = {"status": "completed", "completedAt": "2026-09-12T10:00:00Z"}
    assert zc._meta_is_terminal(terminal)
    for status in ("failed", "stopped"):
        assert zc._meta_is_terminal({"status": status, "completedAt": None})
    # 实测约定：运行中 = 无 completedAt（status 缺失但有 completedAt 仍算终态）
    assert not zc._meta_is_terminal({"status": "running", "completedAt": None})
    assert not zc._meta_is_terminal({"status": None, "completedAt": None})
    assert zc._meta_is_terminal({"status": None, "completedAt": "2026-09-12T10:00:00Z"})


# ---------------------------------------------------------------------------
# ISO 解析（纯函数，时区敏感）
# ---------------------------------------------------------------------------

def test_iso_to_epoch_ms_utc_z():
    assert zc.iso_to_epoch_ms("2026-09-11T12:42:09.549Z") == \
        calendar.timegm((2026, 9, 11, 12, 42, 9)) * 1000 + 549


def test_iso_to_epoch_ms_offset_and_naive():
    utc = calendar.timegm((2026, 9, 11, 12, 42, 9)) * 1000
    assert zc.iso_to_epoch_ms("2026-09-11T20:42:09.549+08:00") == utc + 549
    assert zc.iso_to_epoch_ms("2026-09-11T20:42:09+0800") == utc
    assert zc.iso_to_epoch_ms("2026-09-11T12:42:09") == utc  # naive 按 UTC


def test_iso_to_epoch_ms_invalid_returns_none():
    assert zc.iso_to_epoch_ms("not-a-date") is None
    assert zc.iso_to_epoch_ms("") is None
    assert zc.iso_to_epoch_ms(None) is None
    assert zc.iso_to_epoch_ms(12345) is None


def test_parse_reset_local_ms():
    expected = int(time.mktime(
        time.strptime("2026-09-11 23:01:32", "%Y-%m-%d %H:%M:%S"))) * 1000
    text = "您的限额将在 2026-09-11 23:01:32 重置。"
    assert zc.parse_reset_local_ms(text) == expected
    assert zc.parse_reset_local_ms("2026-09-11T23:01:32") == expected  # T 分隔
    assert zc.parse_reset_local_ms("没有时间") is None
    assert zc.parse_reset_local_ms("2026-13-99 99:99:99") is None  # 非法时刻
    assert zc.parse_reset_local_ms(None) is None


# ---------------------------------------------------------------------------
# fixtures 本体：合成 rollout 样例逐条可映射，且内容标记绝不进入事件
# ---------------------------------------------------------------------------

def test_fixture_rollout_records_map_end_to_end(mapper, monkeypatch):
    # fixture 的 1308 重置时刻是固定历史字符串；冻结墙钟到重置前 1s，
    # 使"过期窗口抑制"不随真实日期翻转（ZC3 时间炸弹修复，conftest 替身）。
    frozen = time.mktime(time.strptime(
        "2026-09-12 23:01:32", "%Y-%m-%d %H:%M:%S")) - 1
    monkeypatch.setattr(zc, "time", FrozenZcodeWallClock(frozen))
    records = load_fixture_records()
    assert len(records) == 4
    seen_types = []
    for record in records:
        seen_types.extend(e.type for e in mapper.rollout_record("sess_x", record))
    assert seen_types.count(ev.EVENT_TURN_STARTED) == 3      # t1/t-err/t-1308
    assert seen_types.count(ev.EVENT_TURN_COMPLETED) == 2    # err + 1308
    assert seen_types.count(ev.EVENT_RATE_LIMITS) == 1       # 仅 [1308] 记录


def test_fixture_content_markers_never_enter_events(mapper):
    markers = ["SYNTHETIC-PROMPT-MARKER", "SYNTHETIC-REPLY-MARKER",
               "SYNTHETIC-TOOL-INPUT-MARKER"]
    dumped = []
    for record in load_fixture_records():
        for e in mapper.rollout_record("sess_x", record):
            dumped.append(json.dumps(e.to_dict(), ensure_ascii=False))
    blob = "\n".join(dumped)
    for marker in markers:
        assert marker not in blob
