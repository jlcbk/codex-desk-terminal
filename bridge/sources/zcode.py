"""ZCode 会话观察器核心（ZC1）：桌面三真源 → NormalizedEvent → StateEngine。

真源结论：docs/P3.6_DESKTOP_OBSERVATION.md §7/§7.1（本机实测核验，勿在本模块
重新调研）。本模块零网络 IO、零写入、零用户配置修改，只读以下三个位置：

- 真源 1 rollout：``~/.zcode/cli/rollout/model-io-<sessionId>.jsonl``（主会话与
  子代理同目录、文件名区分），实时追加，每模型调用一条：
  ``{type:"model_io", startedAt/completedAt(ISO-8601), turnId, querySource,
  attempt, durationMs, model{...}, request{...}, response{text, reasoningText,
  toolCalls, usage, finishReason}, error{name,message,stack}?}``。
  只取状态/时间戳/计数/工具名/脱敏后的错误摘要；提示词、回复文本、工具输入
  内容绝不进入事件、日志或 fixtures（红线）。
- 真源 2 metadata：``~/.zcode/cli/agents/sess_<主会话>/agent_<id>/metadata.json``；
  status 观测枚举 {completed, failed, stopped}，运行中=无 completedAt 字段。
- 真源通道 3 hook spool（本模块只做读取端；写入端由 ZC2 安装器交付）：JSONL
  追加文件，每行一条 ``{"event","session_id","tool_name","received_at"}``；
  hook 事件名恰 7 种（SPOOL_EVENTS）。缺字段/多余字段/坏 JSON 的行跳过并计入
  ``observer.bad_lines``，绝不崩溃。

分层（对齐 bridge/sources/codex.py 的"纯映射器+适配器"）：

- ``ZcodeEventMapper``：纯内存映射（无 IO、可独立单测），持有 per-session 的
  turn 去重/最近 turn/合成审批记账；
- ``ZcodeObserver``：文件轮询适配器——按偏移增量读、目录健康、线程池与协议
  上限、选择线程。engine 由调用方构造好传入（StateEngine，
  source_kind=SOURCE_ZCODE_OBSERVED）。

映射规则（注释锚点 = P3.6 §7/§7.1）：

- 会话→线程：thread_id 直接用 sessionId；project=basename(metadata.cwd)（仅
  子代理有 metadata，主会话 project 留空）；主会话与子代理线程同池。
- working：rollout 出现新行 → thread_status(active)；同一 turnId 首次出现先
  turn_started（reducer 的 turn_started 会清 plan/pending/终态门）。
- done/idle：spool Stop → turn_completed(最近 turnId|"stop", completed) +
  thread_status(idle)。文件活动停滞不产生任何终态事件（不伪造；"停滞→stale"
  只以 stale_session_ids 观测元数据形式暴露给服务层，§7.1 约束 2）。
- needs_you：spool PermissionRequest → approval_requested（合成负数
  request_id，summary=工具名，绝不放 tool_input）；同会话后续
  PreToolUse/PostToolUse/Stop/新 turn → server_request_resolved 撤销。
- error：rollout 记录带 error → turn_completed(failed, summary=scrub(message,
  192))；message 匹配 "[1308]"（5 小时用量上限）→ 额外发 rate_limits
  （[{id:"zcode-5h", label:"ZCODE 5H WINDOW", used_percent:100,
  duration_mins:300, resets_at_ms:<中文重置时间按本地时区 time.mktime 解析>}]）；
  解析失败 resets_at_ms=None，绝不编造。此窗口只在真实命中限额时发。
- token：response.usage → token_usage(used=inputTokens+cacheReadTokens,
  capacity=None)。语义=当前 context 占用近似（最近一次调用的输入+缓存读部分），
  非累计消耗；ZCode 无 context 窗口事实，capacity 诚实给 None（注释见
  _usage_used_tokens）。
- 子代理：metadata 按 mtime/size 变化才重读；新 agent → thread_started；
  运行中 → active；completed/stopped → idle；failed → turn_completed(failed,
  turn_id 优先该线程最近 rollout turnId 以通过 reducer 终态门闸，否则 agentId；
  summary=scrub(error,192))。
- 子代理终态门闸（A0 ZC1-fix 裁决）：childSessionId 的最近 metadata status
  ∈ {completed, failed, stopped} 期间，该会话后续 rollout 行一律不映射（不
  working/turn_started/token/error，防止 working 无清除路径卡死屏幕状态），
  只计数进 ``gated_late_rollout_lines``（报告元数据，不产协议事件）。门闸只在
  metadata 仍为终态时生效：mtime 变化重读后状态翻回运行态（如 resume）即解除、
  后续行恢复映射。只对有 metadata 的子代理会话生效，主会话不受影响。
- 选择与上限：select_thread=最近活动线程（仅在有线程事件的拍发出）；会话池按
  最近活动排序，超出 max_threads 的最旧会话停止跟踪（记账移除，不发事件）；
  发事件前检查 engine 线程数 ≤ 协议上限 8（render.MAX_THREADS），超出上限的
  新线程不发事件（读偏移仍推进，避免积压）。
- 时钟纪律：monotonic_ms 一律由调用方传入（poll_once 参数 / monotonic_fn）；
  ISO 时间戳只用于 at_ms（上游事件时间语义）与 recency 记账，不经墙钟判断
  状态。唯一的墙钟用途是 lookback_s 的文件新鲜度 IO 过滤（不影响状态映射）。

与 ZC2 的接口契约（签名冻结，ZC2 按此接线）::

    ZcodeObserver(engine, rollout_dir=None, agents_dir=None, spool_path=None,
                  poll_interval_s=1.0, stale_after_s=120.0, lookback_s=86400.0,
                  max_threads=16)
    poll_once(monotonic_ms) -> list[AppState dict]   # 本拍全部快照
    run_forever(sink, monotonic_fn=time.monotonic)   # 每份快照回调 sink

spool_path=None 表示禁用 hook 通道（spool 在 ZC2 安装器部署前不存在，默认关
闭是安全默认）；启用时显式传路径，标准路径取 DEFAULT_SPOOL_PATH 常量。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from .. import events as ev
from ..redact import scrub_text
from ..state import render as state_render
from ..state.engine import SOURCE_ZCODE_OBSERVED

# 协议线程上限（INTERFACES §3：render 对 threads>8 截断，观察器在源头不越过）。
PROTOCOL_MAX_THREADS = state_render.MAX_THREADS

# ---- 三真源标准路径（使用前 expanduser）------------------------------------
DEFAULT_ROLLOUT_DIR = "~/.zcode/cli/rollout"
DEFAULT_AGENTS_DIR = "~/.zcode/cli/agents"
DEFAULT_SPOOL_PATH = "~/.zcode/cli/cdt-hook-spool.jsonl"

# ---- hook spool 事件名全集（Z1 核验：恰 7 种，无 SubagentStop/Notification）----
SPOOL_SESSION_START = "SessionStart"
SPOOL_USER_PROMPT_SUBMIT = "UserPromptSubmit"
SPOOL_PRE_TOOL_USE = "PreToolUse"
SPOOL_PERMISSION_REQUEST = "PermissionRequest"
SPOOL_POST_TOOL_USE = "PostToolUse"
SPOOL_POST_TOOL_USE_FAILURE = "PostToolUseFailure"
SPOOL_STOP = "Stop"
SPOOL_EVENTS = (
    SPOOL_SESSION_START,
    SPOOL_USER_PROMPT_SUBMIT,
    SPOOL_PRE_TOOL_USE,
    SPOOL_PERMISSION_REQUEST,
    SPOOL_POST_TOOL_USE,
    SPOOL_POST_TOOL_USE_FAILURE,
    SPOOL_STOP,
)

# ---- 5 小时用量上限（错误码 [1308]）----------------------------------------
RATE_WINDOW_ID_5H = "zcode-5h"
RATE_WINDOW_LABEL_5H = "ZCODE 5H WINDOW"
RATE_WINDOW_MINS_5H = 300
USAGE_LIMIT_5H_RE = re.compile(r"\[1308\]")
# 重置时刻形如 "2026-09-11 23:01:32"（本机实测中文文案内嵌本地裸时间）。
RESET_TIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")

# 占位 turn_id（诚实的最小占位，绝不编造上游不存在的 id 语义）。
STOP_TURN_PLACEHOLDER = "stop"    # Stop 时该会话从未见过 turnId
ERROR_TURN_PLACEHOLDER = "error"  # error 记录缺失 turnId 时

# 冷启动/重读尾巴上限：首次跟踪一个 rollout 文件最多回放这么多条记录。
# 状态从尾部收敛（当前 turn 必在尾部），同时防止长历史文件把首拍事件量撑爆。
COLD_START_MAX_RECORDS = 200


# ---------------------------------------------------------------------------
# 纯函数：时间解析（单测直接覆盖；除注释声明外不依赖本地时区）
# ---------------------------------------------------------------------------

def iso_to_epoch_ms(value) -> Optional[int]:
    """ISO-8601 → UTC epoch 毫秒（纯函数）；解析失败返回 None。

    支持 Z / ±HH:MM / ±HHMM 后缀；naive（无时区）按 UTC 处理（rollout 实测
    发 UTC 带 Z）。时钟纪律：结果只用于 at_ms 与 recency 记账，不做状态判断。
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # 整数化精确到毫秒，避免 float 秒 ×1000 的精度损失。
    return int(dt.timestamp()) * 1000 + dt.microsecond // 1000


def parse_reset_local_ms(message) -> Optional[int]:
    """从 1308 错误 message 提取"重置时刻"→ 本地时区 epoch 毫秒。

    实测文案为中文内嵌本地裸时间（"…您的限额将在 2026-09-11 23:01:32 重置。"），
    按任务约定用 time.mktime（本地时区）解析。解析失败/无时间 → None，绝不编造。
    """
    if not isinstance(message, str):
        return None
    m = RESET_TIME_RE.search(message)
    if m is None:
        return None
    raw = "%s-%s-%s %s:%s:%s" % m.groups()
    try:
        parsed = time.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    try:
        return int(time.mktime(parsed)) * 1000
    except (OverflowError, OSError, ValueError):
        return None


def usage_limit_window_5h(message) -> dict:
    """1308 命中 → UsageWindow。used_percent 恒 100（已到上限）；重置时刻来自
    message 内嵌时间（解析失败 → resets_at_ms=None）。"""
    return {
        "id": RATE_WINDOW_ID_5H,
        "label": RATE_WINDOW_LABEL_5H,
        "used_percent": 100,
        "duration_mins": RATE_WINDOW_MINS_5H,
        "resets_at_ms": parse_reset_local_ms(message),
    }


def _usage_used_tokens(usage) -> Optional[int]:
    """response.usage → context 占用近似：inputTokens + cacheReadTokens。

    取舍说明：ZCode 没有可观察的 context-window 总量事实，capacity 诚实给
    None（render 呈现 used_percent=null），不编造容量。used 取"最近一次调用的
    输入 token + 其中缓存读部分"，即当前上下文规模的近似，而非累计消耗
    （累计值对设备 UI 无意义且会随会话无限增长）。inputTokens 缺失/非整数
    → None（部分数据不硬凑）。
    """
    if not isinstance(usage, dict):
        return None
    used = usage.get("inputTokens")
    if not isinstance(used, int) or isinstance(used, bool):
        return None
    extra = usage.get("cacheReadTokens")
    if isinstance(extra, int) and not isinstance(extra, bool):
        used += extra
    return used


def _meta_is_terminal(meta) -> bool:
    """metadata 是否处于观测终态（Z5 实测枚举 {completed, failed, stopped}）。

    status 缺失但 completedAt 存在也视为终态（实测约定：运行中=无
    completedAt）。mapper 的生命周期映射与观察器的终态门闸共用本判定，
    保证两处对"终态"的口径完全一致。
    """
    status = meta.get("status")
    return status in ("completed", "failed", "stopped") or (
        status is None and meta.get("completedAt") is not None)


def _agent_error_summary(error) -> str:
    """metadata.error → 脱敏单行摘要（实测为字符串；容忍 {message} 形态）。"""
    if isinstance(error, dict):
        error = error.get("message")
    if isinstance(error, str) and error.strip():
        return scrub_text(error, 192)
    return "子代理失败"


class _SessionMapState:
    """per-session 映射记账（纯内存；仅 cap 清池时按会话整体清除）。"""

    __slots__ = ("turns_seen", "last_turn", "started_emitted", "pending_approvals")

    def __init__(self) -> None:
        self.turns_seen = set()              # 已发过 turn_started 的 turnId
        self.last_turn: Optional[str] = None  # 最近见过的 turnId（Stop 归结用）
        self.started_emitted = False          # SessionStart → thread_started 去重
        self.pending_approvals: List[int] = []  # 未撤销的合成 request_id（插入序）


class ZcodeEventMapper:
    """ZCode 原始记录 → [NormalizedEvent]（纯内存、无 IO、可独立单测）。

    - 三个来源（rollout 记录 / spool 行 / metadata）分别经 rollout_record /
      spool_event / agent_update 进入；只产出状态/时间戳/计数/工具名/脱敏摘要；
    - 合成 pending 用负数 request_id（不与上游 id 空间冲突，同 codex.py 策略）；
      同会话后续 PreToolUse/PostToolUse/Stop/新 turn → server_request_resolved；
    - 未知记录类型 / 未知 hook 事件名：忽略（前向兼容），只计数。
    """

    def __init__(self) -> None:
        self._sessions = {}
        self._agents_started = set()   # 已发过 thread_started 的子代理线程
        self._synthetic_next = -1      # 合成审批 id 计数器（负数递减）
        # 计数器（报告用；只有计数，无内容）。
        self.records_seen = 0
        self.non_model_io = 0
        self.spool_seen = 0
        self.agents_seen = 0

    # ---- 记账 ---------------------------------------------------------------
    def _state(self, session_id: str) -> _SessionMapState:
        st = self._sessions.get(session_id)
        if st is None:
            st = _SessionMapState()
            self._sessions[session_id] = st
        return st

    def forget_session(self, session_id: str) -> None:
        """会话映射记账清除（仅观察器 max_threads 清池时调用）。

        rollout 文件消失时【不】调用：保留 turns_seen 可防止文件重读/轮转后
        重新 mint turn_started——reducer 对"已终态线程的同 turn turn_started"
        会清掉 turn_finished 门并复活线程（防复活）。
        """
        self._sessions.pop(session_id, None)

    def _resolve_pending(self, session_id: str, at_ms) -> list:
        st = self._state(session_id)
        if not st.pending_approvals:
            return []
        out = [ev.server_request_resolved(session_id, rid, at_ms=at_ms)
               for rid in st.pending_approvals]
        st.pending_approvals = []
        return out

    # ---- 真源 1：rollout（model_io 记录）------------------------------------
    def rollout_record(self, session_id: str, record) -> list:
        """一条 model_io 记录 → 事件列表。

        映射（§7.1）：新行 → active；turnId 首次出现 → 先（撤销旧合成等待再）
        turn_started；usage → token_usage；带 error → turn_completed(failed,
        summary=scrub)；[1308] → 额外发 5H 用量窗口（仅真实命中时）。
        """
        if not isinstance(record, dict):
            return []
        if record.get("type") != "model_io":
            self.non_model_io += 1
            return []
        self.records_seen += 1
        st = self._state(session_id)
        at_ms = iso_to_epoch_ms(record.get("startedAt"))
        if at_ms is None:
            at_ms = iso_to_epoch_ms(record.get("completedAt"))
        turn_id = record.get("turnId")
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else None

        events = []
        if turn_id is not None and turn_id not in st.turns_seen:
            st.turns_seen.add(turn_id)
            st.last_turn = turn_id
            # 新 turn：先撤销同会话旧合成等待（§7.1：新 turn → resolved；
            # reducer 的 turn_started 本也会清 pending，这里保证 mapper 记账一致），
            # 再 mint turn_started。
            events.extend(self._resolve_pending(session_id, at_ms))
            events.append(ev.turn_started(session_id, turn_id, at_ms=at_ms))
        # 新行 → 活动心跳（文件活动停滞不产生事件，不伪造终态）。
        events.append(ev.thread_status(session_id, ev.THREAD_STATUS_ACTIVE,
                                       at_ms=at_ms))
        response = record.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        used = _usage_used_tokens(usage)
        if used is not None:
            events.append(ev.token_usage(session_id, used, None,
                                         turn_id=turn_id, at_ms=at_ms))
        error = record.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str) \
                and error["message"].strip():
            # 摘要只保留 scrub 后的错误信息（邮箱/凭证/home 路径已被 redact 清洗）。
            summary = scrub_text(error["message"], 192)
            events.append(ev.turn_completed(
                session_id,
                turn_id if turn_id is not None else ERROR_TURN_PLACEHOLDER,
                ev.TURN_STATUS_FAILED, summary=summary, at_ms=at_ms))
            # 5 小时用量上限：仅 message 真实含 [1308] 时发窗口。
            if USAGE_LIMIT_5H_RE.search(error["message"]):
                events.append(ev.rate_limits(
                    [usage_limit_window_5h(error["message"])], at_ms=at_ms))
        return events

    # ---- 真源通道 3：hook spool（只当触发器，不信任载荷完整性，§7.1 约束 1）----
    def spool_event(self, event_name: str, session_id: str, tool_name,
                    at_ms=None) -> list:
        """hook 事件名 → 事件列表。

        PermissionRequest → needs_you（summary=工具名，绝不放 tool_input）；
        PreToolUse/PostToolUse → 撤销合成等待 + active；Stop → turn 归结 +
        idle；SessionStart → thread_started（去重，主会话无 cwd 事实 project
        留空）；UserPromptSubmit/PostToolUseFailure 只作活动记账（无状态事件，
        理由见分支注释）。未知事件名忽略并计数。
        """
        st = self._state(session_id)
        if event_name == SPOOL_SESSION_START:
            if st.started_emitted:
                return []
            st.started_emitted = True
            return [ev.thread_started(session_id, "", at_ms=at_ms)]
        if event_name in (SPOOL_USER_PROMPT_SUBMIT, SPOOL_POST_TOOL_USE_FAILURE):
            # UserPromptSubmit：turn 真源是 rollout 的 turn_started，此处提前发
            # active 会被 reducer 的 finished 门闸吞掉（不伪造"已开始"）；
            # PostToolUseFailure：工具失败≠整轮失败（§7.1：需与 rollout error
            # 合流才构成 error，error 由 rollout_record 负责）。
            return []
        if event_name == SPOOL_PERMISSION_REQUEST:
            request_id = self._synthetic_next
            self._synthetic_next -= 1
            st.pending_approvals.append(request_id)
            # 红线：只取 tool_name 字段；工具输入内容不进入 summary。
            if isinstance(tool_name, str) and tool_name.strip():
                summary = scrub_text(tool_name, 192)
            else:
                summary = "等待审批"
            return [ev.approval_requested(session_id, request_id, summary,
                                          at_ms=at_ms)]
        if event_name in (SPOOL_PRE_TOOL_USE, SPOOL_POST_TOOL_USE):
            # 审批已放行/工具已执行：撤销该会话全部合成等待，再报活动。
            events = self._resolve_pending(session_id, at_ms)
            events.append(ev.thread_status(session_id, ev.THREAD_STATUS_ACTIVE,
                                           at_ms=at_ms))
            return events
        if event_name == SPOOL_STOP:
            events = self._resolve_pending(session_id, at_ms)
            # turn 归结：取该会话最近见过的 turnId，没有则用占位 "stop"。
            turn_id = st.last_turn if st.last_turn else STOP_TURN_PLACEHOLDER
            events.append(ev.turn_completed(session_id, turn_id,
                                            ev.TURN_STATUS_COMPLETED,
                                            at_ms=at_ms))
            # idle 置于 turn 归结之后：reducer 的终态门闸丢弃 turn_completed 时
            # （如跨通道乱序的 turn_id 不一致），idle 兜底，避免 working 死挂。
            events.append(ev.thread_status(session_id, ev.THREAD_STATUS_IDLE,
                                           at_ms=at_ms))
            return events
        return []  # 未知事件名：忽略（前向兼容；观察器侧已计数）

    # ---- 真源 2：子代理 metadata ---------------------------------------------
    def agent_update(self, child_session_id: str, meta, at_ms=None) -> list:
        """一份（有变化的）metadata → 事件列表。

        首见 → thread_started(project=basename(cwd) 脱敏)；运行中（无
        completedAt）→ active；completed/stopped → idle；failed →
        turn_completed(failed)：turn_id 优先该线程最近 rollout turnId（通过
        reducer 终态门闸，避免跨源 id 不一致被丢弃），否则用 agentId。
        """
        if not isinstance(meta, dict):
            return []
        st = self._state(child_session_id)
        events = []
        if child_session_id not in self._agents_started:
            self._agents_started.add(child_session_id)
            self.agents_seen += 1
            cwd = meta.get("cwd")
            project = ""
            if isinstance(cwd, str) and cwd.strip():
                project = scrub_text(os.path.basename(cwd.rstrip("/")) or cwd, 96)
            events.append(ev.thread_started(child_session_id, project,
                                            at_ms=at_ms))
        status = meta.get("status")
        terminal = _meta_is_terminal(meta)
        if not terminal:
            # 运行中：活动心跳（观察器仅在 mtime 变化时重读，不刷屏）。
            events.append(ev.thread_status(child_session_id,
                                           ev.THREAD_STATUS_ACTIVE, at_ms=at_ms))
            return events
        # 终态：先撤销该线程的合成等待（子代理线程同池、同规则）。
        events.extend(self._resolve_pending(child_session_id, at_ms))
        if status == "failed":
            turn_id = st.last_turn or str(meta.get("agentId") or "agent")
            events.append(ev.turn_completed(
                child_session_id, turn_id, ev.TURN_STATUS_FAILED,
                summary=_agent_error_summary(meta.get("error")), at_ms=at_ms))
        else:
            # completed / stopped（以及罕见"有 completedAt 但 status 未知"）→ idle。
            events.append(ev.thread_status(child_session_id,
                                           ev.THREAD_STATUS_IDLE, at_ms=at_ms))
        return events


# ---------------------------------------------------------------------------
# 观察器（唯一 IO 层：只读文件，零网络、零写入）
# ---------------------------------------------------------------------------

class ZcodeObserver:
    """ZCode 桌面会话观察器：轮询三真源 → mapper → engine.apply → 快照序列。

    engine 由调用方构造好传入（StateEngine，source_kind=zcode_observed）；
    monotonic_ms 一律由调用方注入。单拍无事件 → poll_once 返回 []，
    run_forever 不回调 sink（安静拍不产快照）。
    """

    def __init__(self, engine, *, rollout_dir=None, agents_dir=None,
                 spool_path=None, poll_interval_s: float = 1.0,
                 stale_after_s: float = 120.0, lookback_s: float = 86400.0,
                 max_threads: int = 16) -> None:
        self.engine = engine
        # rollout_dir/agents_dir 缺省 = ZCode 标准路径；spool_path=None = 禁用
        # hook 通道（标准路径见 DEFAULT_SPOOL_PATH，由调用方显式传入启用）。
        self.rollout_dir = os.path.expanduser(
            rollout_dir if rollout_dir is not None else DEFAULT_ROLLOUT_DIR)
        self.agents_dir = os.path.expanduser(
            agents_dir if agents_dir is not None else DEFAULT_AGENTS_DIR)
        self.spool_path = os.path.expanduser(spool_path) if spool_path else None
        self.poll_interval_s = max(0.0, float(poll_interval_s))
        self.stale_after_s = max(0.0, float(stale_after_s))
        self.lookback_s = max(0.0, float(lookback_s))   # ≤0 = 关闭新鲜度过滤
        self.max_threads = max(1, int(max_threads))
        self.mapper = ZcodeEventMapper()
        # ---- 记账（会话粒度）----
        self._sessions = {}         # session_id -> 最近活动 monotonic ms（0=休眠）
        self._ever_seen = set()     # 曾注册过的会话（清池后再发现按休眠态入池）
        self._rollout_offsets = {}  # session_id -> 已读字节偏移
        self._agent_mtimes = {}     # metadata 路径 -> (mtime_ns, size)
        # 子代理终态门闸（ZC1-fix）：最近一次观测为终态的 childSessionId 集合。
        # 只在 metadata 仍为终态时生效——mtime 变化重读翻回运行态即移除（解除）。
        self._terminal_children = set()
        self._spool_offset = None   # None = 未武装（首次见到文件=从尾部起读）
        self._rollout_ok = None     # None=未探测；rollout 根目录健康与否
        self._apply_seq = 0         # 拍内事件序号（select_thread 并列决胜）
        # ---- 运行计数（报告用；只有计数，无内容）----
        self.polls = 0
        self.bad_lines = 0
        self.read_errors = 0
        # 终态子代理迟到的 rollout 行计数（被门闸吞掉的行数；只进报告元数据）
        self.gated_late_rollout_lines = 0

    # ---- 主入口 --------------------------------------------------------------
    def poll_once(self, monotonic_ms: int) -> list:
        """扫一遍三真源 → 逐事件 engine.apply → 返回本拍产出的全部快照列表。

        monotonic_ms：调用方注入的单调毫秒（跨调用须非递减）。唯一的墙钟用途
        是 lookback_s 文件新鲜度过滤（time.time()，IO 层），不经墙钟判断状态。
        """
        mono = int(monotonic_ms)
        self.polls += 1
        snapshots = []
        touched = {}  # session_id -> (mono, 拍内序)；本拍有线程活动的会话

        def apply(events, session_id=None):
            for event in events:
                snapshots.append(self.engine.apply(event, mono))
                if session_id is not None:
                    self._apply_seq += 1
                    touched[session_id] = (mono, self._apply_seq)

        # 1) rollout 根目录健康 → source connected/disconnected（源级 stale 的
        #    唯一信号；agents/spool 缺席属正常，不算断连）。
        discovered = self._discover_rollouts(time.time())
        rollout_ok = discovered is not None
        if rollout_ok and self._rollout_ok is False:
            apply([ev.source_reconnected()])
        elif not rollout_ok and self._rollout_ok is not False:
            apply([ev.source_disconnected()])
        self._rollout_ok = rollout_ok
        if not rollout_ok:
            discovered = {}
        # rollout 文件消失 → 清该会话读偏移（记账清理；turns 记账保留，
        # 见 mapper.forget_session 的防复活说明）。
        for session_id in list(self._rollout_offsets):
            if session_id not in discovered:
                del self._rollout_offsets[session_id]
        # 新文件 → 注册会话（确定性：按 sid 排序发现）。
        for session_id in sorted(discovered):
            self._register_session(session_id, mono)

        # 2) 先读完 spool/metadata 增量并注册其会话，再做池上限——保证
        #    "最近活动"池收纳的是最新会话而非发现顺序在前的会话。
        spool_entries = self._drain_spool()
        for _name, session_id, _tool, _at in spool_entries:
            self._note_activity(session_id, mono)   # hook 触发=真实活动
        agent_entries = self._scan_agents(time.time())
        for child_id, meta, _at in agent_entries:
            self._note_activity(child_id, mono)     # metadata 变化=真实活动
            # 终态门闸集合维护：本步先于 rollout 映射（step 4），故 metadata
            # 终态化/翻回运行态都在同一拍内对 rollout 行生效。翻回运行态
            # （resume，completedAt 消失/status 变化）必然伴随 mtime 变化触发
            # 重读（_scan_agents 只返回有变化的 metadata）→ 门闸在此解除。
            if _meta_is_terminal(meta):
                self._terminal_children.add(child_id)
            else:
                self._terminal_children.discard(child_id)

        # 3) 线程池上限：超出 max_threads 的最旧会话停止跟踪（仅移除记账，
        #    不发事件）。
        self._prune_sessions()

        # 4) rollout 增量映射（按最近活动新→旧处理，上限与选择都偏向最新）。
        for session_id in self._sessions_by_recency():
            path = discovered.get(session_id)
            if path is None:
                continue  # 纯 spool/metadata 会话：无 rollout 文件
            first_read = session_id not in self._rollout_offsets
            new_offset, lines = self._read_file_increment(
                path, self._rollout_offsets.get(session_id, 0))
            self._rollout_offsets[session_id] = new_offset
            if not lines:
                continue
            self._note_activity(session_id, mono)  # 观察到新字节=活动（可重新入池）
            if first_read:
                # 冷启动/重读：只回放尾部（状态从尾部收敛，事件量有界）。
                lines = lines[-COLD_START_MAX_RECORDS:]
            if session_id in self._terminal_children:
                # 子代理终态门闸（A0 ZC1-fix 裁决）：metadata 终态期间迟到的
                # rollout 行一律吞掉——不 working/turn_started/token/error，
                # 防止 working 无清除路径卡死屏幕状态；只计数进报告元数据，
                # 不产协议事件。recency 仍刷新（文件确有新字节），不进 bad_lines。
                self.gated_late_rollout_lines += len(lines)
                continue
            records = []
            for raw in lines:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    self.bad_lines += 1
                    continue
                if isinstance(record, dict):
                    records.append(record)
                else:
                    self.bad_lines += 1
            if not self._event_allowed(session_id):
                continue  # 超协议上限的线程：不发事件（偏移已推进防积压）
            for record in records:
                events = self.mapper.rollout_record(session_id, record)
                if events:
                    apply(events, session_id)

        # 5) spool 映射（文件固有行序=发生序）。
        for name, session_id, tool_name, at_ms in spool_entries:
            if session_id not in self._sessions:
                continue  # 已被池上限清退：不跟踪、不发事件
            self._note_activity(session_id, mono)  # hook 触发=活动
            if not self._event_allowed(session_id):
                continue
            events = self.mapper.spool_event(name, session_id, tool_name, at_ms)
            if events:
                apply(events, session_id)

        # 6) 子代理 metadata 映射（仅 mtime/size 变化者进入 agent_entries）。
        for child_id, meta, at_ms in agent_entries:
            if child_id not in self._sessions:
                continue
            self._note_activity(child_id, mono)
            if not self._event_allowed(child_id):
                continue
            events = self.mapper.agent_update(child_id, meta, at_ms)
            if events:
                apply(events, child_id)

        # 7) 选择：select_thread = 最近活动线程（仅本拍有线程事件时发出；
        #    安静拍不产事件，避免每拍空转快照）。
        if touched:
            best = max(touched.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]
            if best in self.engine.thread_ids():
                apply([ev.select_thread(best)])
        return snapshots

    def run_forever(self, sink: Callable[[dict], None],
                    monotonic_fn: Callable[[], float] = time.monotonic) -> None:
        """长驻循环：poll_once → 每份快照回调 sink；引擎不产快照的拍不回调。

        monotonic_fn 返回单调秒（默认 time.monotonic；测试注入固定序列）。
        KeyboardInterrupt 干净退出；其他异常向上传播（投递策略由调用方定）。
        """
        try:
            while True:
                for snapshot in self.poll_once(int(monotonic_fn() * 1000)):
                    sink(snapshot)
                if self.poll_interval_s > 0:
                    time.sleep(self.poll_interval_s)
        except KeyboardInterrupt:
            return None

    # ---- 观测元数据（服务层/报告；不映射任何状态事件）--------------------------
    def describe(self) -> dict:
        """运行报告：只有计数与健康位，无提示词/回复/工具输入内容。"""
        return {
            "source_kind": SOURCE_ZCODE_OBSERVED,
            "polls": self.polls,
            "bad_lines": self.bad_lines,
            "read_errors": self.read_errors,
            "gated_late_rollout_lines": self.gated_late_rollout_lines,
            "tracked_sessions": len(self._sessions),
            "engine_threads": len(self.engine.thread_ids()),
            "rollout_ok": self._rollout_ok,
            "spool_enabled": self.spool_path is not None,
            "records_seen": self.mapper.records_seen,
            "non_model_io": self.mapper.non_model_io,
            "spool_seen": self.mapper.spool_seen,
            "agents_seen": self.mapper.agents_seen,
        }

    def stale_session_ids(self, monotonic_ms: int) -> List[str]:
        """超过 stale_after_s 无任何源活动的会话 id（观测元数据，拍时钟）。

        §7.1 约束 2 的"文件停滞超 TTL→stale"：协议无 per-thread stale 事件，
        且停滞绝不产生终态（不伪造）——本集合只供服务层健康报告/诊断，
        绝不映射为状态事件。
        """
        cutoff = int(monotonic_ms) - int(self.stale_after_s * 1000)
        return sorted(sid for sid, last in self._sessions.items() if last < cutoff)

    # ---- 文件读取（增量 / 轮转 / 容错）----------------------------------------
    @staticmethod
    def _split_lines(chunk: bytes):
        """bytes 块 → (完整行列表, 消耗字节数)；尾部不完整行留待下次增量。"""
        lines = []
        start = 0
        while True:
            nl = chunk.find(b"\n", start)
            if nl < 0:
                break
            lines.append(chunk[start:nl])
            start = nl + 1
        return lines, start

    def _read_file_increment(self, path: str, offset: int):
        """按偏移增量读一个 JSONL 文件 → (新偏移, 完整行列表)。

        offset > 文件大小（轮转/截断）→ 从 0 重读；文件此刻不可见 → 原偏移
        空结果（下一拍 discovery 会做会话记账清理）；其他 IO 错误计数不抛。
        """
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size < offset:
                    offset = 0  # 轮转/截断：从头重读（状态由 mapper 去重保证幂等）
                fh.seek(offset)
                chunk = fh.read()
        except FileNotFoundError:
            return offset, []
        except OSError:
            self.read_errors += 1
            return offset, []
        lines, consumed = self._split_lines(chunk)
        return offset + consumed, lines

    def _drain_spool(self) -> List[Tuple[str, str, Optional[str], Optional[int]]]:
        """hook spool 增量 → [(event, session_id, tool_name, at_ms)]。

        首次见到 spool 文件时从文件末尾起读（不回放历史）：spool 是触发通道，
        历史事件脱离当时 rollout 上下文会把旧 turn 重放成新状态。行格式按
        ZC2 已冻结的 4 字段解析；多余字段容忍，坏行跳过并计数，绝不崩溃。
        """
        if self.spool_path is None:
            return []
        try:
            with open(self.spool_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                base = self._spool_offset
                if base is None:
                    self._spool_offset = size  # 武装：只观察此后追加的行
                    return []
                if size < base:
                    base = 0  # 轮转/截断：重读
                fh.seek(base)
                chunk = fh.read()
        except FileNotFoundError:
            return []  # hook 未安装/文件尚未创建：常态，不算错误
        except OSError:
            self.read_errors += 1
            return []
        lines, consumed = self._split_lines(chunk)
        self._spool_offset = base + consumed
        entries = []
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                self.bad_lines += 1
                continue
            if not isinstance(obj, dict):
                self.bad_lines += 1
                continue
            name = obj.get("event")
            session_id = obj.get("session_id")
            if not isinstance(name, str) or not isinstance(session_id, str) \
                    or not session_id:
                self.bad_lines += 1  # 缺 event/session_id：坏行
                continue
            if name not in SPOOL_EVENTS:
                self.bad_lines += 1  # 事件名超出 7 种实测枚举：坏行（前向容忍）
                continue
            tool_name = obj.get("tool_name")
            if not isinstance(tool_name, str):
                tool_name = None
            self.mapper.spool_seen += 1
            entries.append((name, session_id, tool_name,
                            iso_to_epoch_ms(obj.get("received_at"))))
        return entries

    def _scan_agents(self, now_wall: float):
        """扫描 agents/sess_*/agent_*/metadata.json → [(child_sid, meta, at_ms)]。

        mtime/size 变化才重读（运行中 metadata 不停更新，但观察器只在变化拍
        重映射）；超 lookback_s 的旧 metadata 不跟踪（与 rollout 同一新鲜度
        判据——否则历史子代理会占满协议 8 线程槽位）。目录不存在=从未有过
        子代理，属常态。
        """
        if not self.agents_dir:
            return []
        try:
            sess_dirs = os.listdir(self.agents_dir)
        except OSError:
            return []
        entries = []
        for sess_name in sorted(sess_dirs):
            if not sess_name.startswith("sess_"):
                continue
            sess_path = os.path.join(self.agents_dir, sess_name)
            try:
                agent_names = os.listdir(sess_path)
            except OSError:
                continue
            for agent_name in sorted(agent_names):
                if not agent_name.startswith("agent_"):
                    continue
                meta_path = os.path.join(sess_path, agent_name, "metadata.json")
                try:
                    st = os.stat(meta_path)
                except OSError:
                    continue
                if self.lookback_s > 0 and (now_wall - st.st_mtime) > self.lookback_s:
                    continue
                key = (st.st_mtime_ns, st.st_size)
                if self._agent_mtimes.get(meta_path) == key:
                    continue
                self._agent_mtimes[meta_path] = key
                try:
                    with open(meta_path, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                except (OSError, ValueError):
                    self.bad_lines += 1
                    continue
                if not isinstance(meta, dict):
                    self.bad_lines += 1
                    continue
                child = meta.get("childSessionId")
                if not isinstance(child, str) or not child:
                    self.bad_lines += 1
                    continue
                entries.append((child, meta, iso_to_epoch_ms(meta.get("updatedAt"))))
        return entries

    def _discover_rollouts(self, now_wall: float):
        """rollout 目录盘点 → {session_id: path}；根目录不可读 → None（断连）。

        文件名 model-io-<sessionId>.jsonl；超 lookback_s 的旧文件不跟踪。
        """
        try:
            names = os.listdir(self.rollout_dir)
        except OSError:
            return None
        prefix, suffix = "model-io-", ".jsonl"
        discovered = {}
        for name in sorted(names):
            if not (name.startswith(prefix) and name.endswith(suffix)):
                continue
            session_id = name[len(prefix):len(name) - len(suffix)]
            if not session_id:
                continue
            path = os.path.join(self.rollout_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if self.lookback_s > 0 and (now_wall - st.st_mtime) > self.lookback_s:
                continue
            discovered[session_id] = path
        return discovered

    # ---- 上限与排序 -----------------------------------------------------------
    def _register_session(self, session_id: str, mono: int) -> None:
        """rollout 发现注册：全新会话记为最新（避免首次处理前被池上限清退）；
        清池后再发现的会话以休眠态（0）入池——只有新活动（新行/hook/metadata
        变化）才把它重新变"新"，防止清池被发现循环悄悄撤销。"""
        if session_id in self._sessions:
            return
        if session_id in self._ever_seen:
            self._sessions[session_id] = 0
        else:
            self._sessions[session_id] = mono
            self._ever_seen.add(session_id)

    def _note_activity(self, session_id: str, mono: int) -> None:
        """会话出现真实活动（新行/hook 事件/metadata 变化）→ 刷新最近活动。"""
        self._sessions[session_id] = mono
        self._ever_seen.add(session_id)

    def _prune_sessions(self) -> None:
        """超出 max_threads 的最旧会话停止跟踪：仅移除映射记账与 recency
        （§7.1 上限语义：记账移除、不发事件；重新出现活动可重新入池）。

        读偏移有意保留：清池会话的文件仍在磁盘上，若连偏移一并清除，下一拍
        发现-冷回放会把旧记录误判成"新活动"，上限形同虚设；保留偏移后，
        休眠会话零开销（一次 stat + 偏移比对），新行到达才重新激活。
        """
        excess = len(self._sessions) - self.max_threads
        if excess <= 0:
            return
        order = sorted(self._sessions.items(), key=lambda kv: (-kv[1], kv[0]))
        for session_id, _mono in order[len(order) - excess:]:
            del self._sessions[session_id]
            # 终态门闸事实（_terminal_children）有意随清池保留：门闸的解除
            # 途径只有"metadata 重读翻回运行态"（与池成员无关），否则清池后
            # 重新入池的终态子代理会丢门闸、缺陷复活。
            self.mapper.forget_session(session_id)

    def _sessions_by_recency(self) -> List[str]:
        """会话 id 按最近活动新→旧（并列按 sid 字典序，确定性）。"""
        return [sid for sid, _mono in
                sorted(self._sessions.items(), key=lambda kv: (-kv[1], kv[0]))]

    def _event_allowed(self, session_id: str) -> bool:
        """协议线程上限：已在 engine 的线程恒可维护；新线程仅当 engine <8。

        超上限线程不发事件，但读偏移照常推进（避免恢复后积压重放）。8 取自
        render.MAX_THREADS（INTERFACES §3 渲染截断阈值），源头不越过。
        """
        engine_ids = self.engine.thread_ids()
        if session_id in engine_ids:
            return True
        return len(engine_ids) < PROTOCOL_MAX_THREADS
