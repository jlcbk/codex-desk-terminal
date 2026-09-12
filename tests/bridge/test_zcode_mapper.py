"""ZC1 离线单测：ZCode 事件映射器（bridge/sources/zcode.py::ZcodeEventMapper + 纯函数）。

策略：纯内存、零 IO、零真实 ~/.zcode 访问。规则真源 =
docs/P3.6_DESKTOP_OBSERVATION.md §7/§7.1 + ZC1 任务映射表。覆盖：

- working：新行 → active；turnId 首次出现先 turn_started；usage → token
  （used=inputTokens+cacheReadTokens，capacity=None）；
- Stop → turn_completed(last|"stop", completed) + idle；
- needs_you：PermissionRequest → 合成负数审批（summary=工具名，绝无 tool_input）；
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
        ev.EVENT_TURN_STARTED, ev.EVENT_THREAD_STATUS, ev.EVENT_TOKEN_USAGE]
    assert events[0].thread_id == SID and events[0].turn_id == "t1"
    assert events[1].status == ev.THREAD_STATUS_ACTIVE
    # token：used=inputTokens+cacheReadTokens；ZCode 无容量事实 → capacity=None
    assert events[2].used_tokens == 100 + 20
    assert events[2].capacity_tokens is None
    # at_ms：startedAt 的 UTC 毫秒（纯函数换算）
    assert events[0].at_ms == (calendar.timegm((2026, 9, 12, 10, 0, 0)) * 1000)


def test_second_record_same_turn_has_no_turn_started(mapper):
    mapper.rollout_record(SID, model_io(turn_id="t1"))
    events = mapper.rollout_record(SID, model_io(turn_id="t1"))
    assert [e.type for e in events] == [ev.EVENT_THREAD_STATUS, ev.EVENT_TOKEN_USAGE]


def test_usage_missing_or_partial_does_not_mint_token_event(mapper):
    no_usage = model_io()
    no_usage["response"]["usage"] = None
    assert [e.type for e in mapper.rollout_record(SID, no_usage)] == [
        ev.EVENT_TURN_STARTED, ev.EVENT_THREAD_STATUS]
    bad = model_io(turn_id="t1")  # 同 turn：只差 token 事件的有无
    bad["response"]["usage"] = {"outputTokens": 5}  # inputTokens 缺失不硬凑
    assert [e.type for e in mapper.rollout_record(SID, bad)] == [
        ev.EVENT_THREAD_STATUS]


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


def test_activity_only_spool_events_mint_no_state_events(mapper):
    # UserPromptSubmit / PostToolUseFailure：turn 真源在 rollout；工具失败≠整轮失败
    assert mapper.spool_event(zc.SPOOL_USER_PROMPT_SUBMIT, SID, None) == []
    assert mapper.spool_event(zc.SPOOL_POST_TOOL_USE_FAILURE, SID, "bash") == []


def test_session_start_mints_thread_started_once(mapper):
    events = mapper.spool_event(zc.SPOOL_SESSION_START, SID, None)
    assert [e.type for e in events] == [ev.EVENT_THREAD_STARTED]
    assert events[0].thread_id == SID
    assert events[0].project == ""  # 主会话无 metadata.cwd 事实，诚实留空
    assert mapper.spool_event(zc.SPOOL_SESSION_START, SID, None) == []


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
    # 再次 PreToolUse：无 pending 可撤，只剩 active
    events2 = mapper.spool_event(zc.SPOOL_POST_TOOL_USE, SID, "bash")
    assert [e.type for e in events2] == [ev.EVENT_THREAD_STATUS]


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
    message = "[1308][已达到 5 小时的使用上限。您的限额将在 2026-09-12 23:01:32 重置。]"
    events = mapper.rollout_record(SID, model_io(
        turn_id="t-1308", error={"name": "UsageLimitError", "message": message}))
    windows = [e for e in events if e.type == ev.EVENT_RATE_LIMITS]
    assert len(windows) == 1  # 只在真实命中 [1308] 时发，且只发一次
    window = windows[0].windows[0]
    expected_ms = int(time.mktime(
        time.strptime("2026-09-12 23:01:32", "%Y-%m-%d %H:%M:%S"))) * 1000
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

def test_fixture_rollout_records_map_end_to_end(mapper):
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
