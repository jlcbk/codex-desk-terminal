"""锁定版 Codex app-server adapter（P3.1，角色 A1：Bridge/State）。

把本机 codex CLI 0.152.0 的 app-server（stdio JSON-RPC）实时事件转成
NormalizedEvent 喂 StateEngine，产出真实 AppState 快照序列。

能力结论真源：docs/CODEX_CAPABILITIES.md（P0.2）。本 adapter 的关键事实：

- 桌面运行时任务不可旁听 → 本 adapter 只做 bridge-owned 受控会话，
  StateEngine 的 source.kind 固定为 ``codex_bridge_owned``（INTERFACES §3）。
- 账户默认模型 gpt-6-astra 被 0.152.0 拒（400）→ turn 必须显式固定模型
  （默认 gpt-5.6-sol，见 CODEX_CAPABILITIES 矩阵第 7 行）。
- 启动即做能力检测：initialize（clientInfo=codex-desk-terminal-bridge +
  capabilities.experimentalApi）、account/rateLimits/read 实探、
  thread/start / turn/start 等对照冻结 schema 方法清单核验；
  结果写入运行报告（快照内的体现是 source.kind=codex_bridge_owned）。

安全红线（与 P0.2 相同，违反即失败）：
- 绝不 resume / 触碰已存在 thread；会话线程一律 ephemeral=true，隔离在
  mkdtemp 临时 cwd，结束后 stop 进程并删除临时目录。
- 绝不 approve/reject：server→client 审批请求只接收不回应（客户端层
  结构性保证，见 bridge/codex_rpc.py）；我们自己的受控 turn 停滞时只允许
  turn/interrupt 自己的 turn（探针 6b 先例，不解锁任何审批）。
- 每次交互带超时；原始 IO 日志落盘前经 bridge/redact.py 清洗，
  凭证/邮箱/home 路径不入 fixture 或日志。

事件映射（app-server method → NormalizedEvent，见 bridge/events.py docstring）：
thread/started、thread/status/changed（activeFlags 确认 needs_you；缺请求
载荷时按 STATUS.md P1.1 结论合成 pending，等真实请求到达后替换）、
turn/started/completed（interrupted→cancelled）、item/started/completed、
turn/plan/updated（inProgress→in_progress）、thread/tokenUsage/updated
（used=totalTokens，capacity=modelContextWindow；totalTokens 含系统开销）、
account/rateLimits/read+updated（双窗口；updated 为稀疏推送，按窗口 id 合并）、
审批请求族 → approval_requested、serverRequest/resolved → server_request_resolved。

CLI（经 python -m bridge 分发）::

    python -m bridge --source codex --prompt "只回复 ok" --out DIR --live
    python -m bridge --source codex --prompt "..." --out DIR   # 无 --live 为 dry-run

断连/重连：app-server 进程退出 → 指数退避重启（1/2/4/8/16/30s 上限，
INTERFACES §6；+抖动）；重连后经 StateEngine 产生 source_disconnected /
source_reconnected 语义事件（source.connected 翻转、stale 来回）。
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

from .. import events as ev
from ..codex_rpc import (
    KIND_NOTIFICATION,
    KIND_SERVER_REQUEST,
    AppServerClient,
    RpcError,
    RpcTimeout,
)
from ..redact import scrub, scrub_text
from ..state.engine import SOURCE_BRIDGE_OWNED, StateEngine

# ---- initialize 参数（对齐 docs/proto-samples/requests/initialize.request.json）----
CLIENT_INFO = {
    "name": "codex-desk-terminal-bridge",
    "title": "Codex Desk Terminal bridge-owned adapter",
    "version": "0.1.0",
}
INITIALIZE_PARAMS = {
    "clientInfo": CLIENT_INFO,
    "capabilities": {"experimentalApi": True},
}

DEFAULT_MODEL = "gpt-5.6-sol"       # 账户默认 gpt-6-astra 被 0.152.0 拒 400，必须显式固定
DEFAULT_PROMPT = "只回复 ok"
DEFAULT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)  # INTERFACES §6，秒；上限 30s

# 审批/用户输入等待标志（schema ThreadActiveFlag 枚举）。
FLAG_WAITING_APPROVAL = "waitingOnApproval"
FLAG_WAITING_USER_INPUT = "waitingOnUserInput"
WAITING_FLAGS = (FLAG_WAITING_APPROVAL, FLAG_WAITING_USER_INPUT)

# server→client 请求族的审批类方法（0.152.0 schema ServerRequest 面，已实测子集）。
APPROVAL_REQUEST_METHODS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
)

# 能力检测针对的方法（INTERFACES §3 adapter 核对清单 + CODEX_CAPABILITIES 矩阵）。
CAPABILITY_METHODS = (
    "thread/start",
    "turn/start",
    "account/rateLimits/read",
    "account/rateLimits/updated",
    "thread/status/changed",
    "thread/tokenUsage/updated",
    "turn/plan/updated",
    "item/commandExecution/requestApproval",
    "serverRequest/resolved",
)

# 退出码（adapter 层；CLI 直接透传）：
#   0 = 真实 turn 以 completed 终态结束；3 = turn failed；4 = turn interrupted
#   （含停滞时我们 interrupt 自己 turn 后收到上游终态）；5 = turn 超时无终态；
#   6 = 能力缺失（initialize/thread/start 不可用）；7 = app-server 反复退出。
EXIT_TURN_COMPLETED = 0
EXIT_TURN_FAILED = 3
EXIT_TURN_INTERRUPTED = 4
EXIT_TURN_TIMEOUT = 5
EXIT_CAPABILITY_MISSING = 6
EXIT_PROCESS_EXITED = 7

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_METHODS_FILE = os.path.join(
    _REPO_ROOT, "docs", "proto-samples", "schema-codex-0.152.0", "methods-codex-0.152.0.json"
)
_SERVER_REQUEST_SCHEMA_FILE = os.path.join(
    _REPO_ROOT, "docs", "proto-samples", "schema-codex-0.152.0",
    "codex_app_server_protocol.v1.schemas.json"
)


def _declared_methods():
    """冻结的 0.152.0 方法名清单（docs/proto-samples）；读不到则返回 (None, False)。

    方法存档（methods-codex-0.152.0.json）只有 client 请求与 server 通知两个桶；
    server→client 请求（审批族等 11 个）取自 v1 schema bundle 的
    definitions.ServerRequest.oneOf 分支。返回 (方法名集合, server请求桶是否已并入)。
    """
    merged = set()
    try:
        with open(_METHODS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for value in data.values():
            if isinstance(value, list):
                merged.update(m for m in value if isinstance(m, str))
    except (OSError, ValueError):
        return None, False
    have_server_requests = False
    try:
        with open(_SERVER_REQUEST_SCHEMA_FILE, "r", encoding="utf-8") as fh:
            bundle = json.load(fh)
        for branch in bundle["definitions"]["ServerRequest"].get("oneOf", []):
            enum = ((((branch or {}).get("properties") or {})
                     .get("method") or {}).get("enum"))
            if isinstance(enum, list):
                merged.update(m for m in enum if isinstance(m, str))
        have_server_requests = True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return (merged or None), have_server_requests


def windows_from_rate_limits(rate_limits):
    """RateLimitSnapshot → UsageWindow 列表（primary/secondary 双窗口）。

    resetsAt 是 UTC 秒（0.152.0 实测），×1000 转 ms；usedPercent 缺失的窗口跳过。
    """
    out = []
    if not isinstance(rate_limits, dict):
        return out
    for key, window_id in (("primary", "codex-primary"), ("secondary", "codex-secondary")):
        window = rate_limits.get(key)
        if not isinstance(window, dict) or window.get("usedPercent") is None:
            continue
        mins = window.get("windowDurationMins")
        resets = window.get("resetsAt")
        label = ("%d MIN WINDOW" % mins) if isinstance(mins, int) else "CODEX WINDOW"
        out.append({
            "id": window_id,
            "label": label,
            "used_percent": window.get("usedPercent"),
            "duration_mins": mins if isinstance(mins, int) else None,
            "resets_at_ms": int(resets) * 1000 if isinstance(resets, int) else None,
        })
    return out


def _item_summary(item):
    """item/started 的单行摘要（已脱敏；无可用字段时 None，不编造）。"""
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    raw = None
    if kind == "commandExecution":
        raw = item.get("command")
    elif kind == "fileChange":
        raw = item.get("path") or item.get("cwd")
    elif kind == "mcpToolCall":
        raw = item.get("tool") or item.get("server")
    elif kind == "webSearch":
        raw = item.get("query")
    if isinstance(raw, str) and raw.strip():
        return scrub_text(raw, 192)
    return None


def _plan_step_status(status):
    """上游 TurnPlanStepStatus → 规范化 status（adapter 负责归一化）。"""
    if status == ev.PLAN_IN_PROGRESS or status == "inProgress":
        return ev.PLAN_IN_PROGRESS
    if status == ev.PLAN_COMPLETED:
        return ev.PLAN_COMPLETED
    return ev.PLAN_PENDING


def _turn_status_normalize(status):
    """上游 TurnStatus → turn/completed 事件 status；非终态返回 None（忽略）。"""
    if status == "completed":
        return ev.TURN_STATUS_COMPLETED
    if status == "failed":
        return ev.TURN_STATUS_FAILED
    if status == "interrupted":
        # 0.152.0 无 "cancelled" 终态；turn/interrupt 后上游报 interrupted，
        # 语义对齐 INTERFACES §3 的 cancelled（IDLE + 取消说明）。
        return ev.TURN_STATUS_CANCELLED
    return None


class CodexEventMapper:
    """app-server 原始消息 → [NormalizedEvent]（纯内存、无 IO、可独立单测）。

    - 未知 method：忽略（前向兼容），只计数；
    - 审批等待合成的 pending 使用负数 request_id（不与上游 id 空间冲突），
      真实审批请求到达时先以 server_request_resolved 撤掉合成项再挂真实项；
    - account/rateLimits/updated 是稀疏推送：null 窗口不清除旧值（schema 描述），
      按窗口 id 合并到最近已知值后再发完整窗口集。
    """

    def __init__(self):
        self._synthetic_next = -1
        self._pending_real = {}   # thread_id -> set(request_id)（本 adapter 观察到的未决审批）
        self._synthetic = {}      # thread_id -> 合成 pending 的 request_id
        self._last_windows = {}   # window_id -> UsageWindow（稀疏合并）
        self.notifications_seen = {}
        self.server_requests_seen = []

    # ---- notifications ---------------------------------------------------
    def notification(self, method, params, at_ms=None):
        self.notifications_seen[method] = self.notifications_seen.get(method, 0) + 1
        params = params if isinstance(params, dict) else {}
        handler = {
            "thread/started": self._thread_started,
            "thread/status/changed": self._thread_status,
            "turn/started": self._turn_started,
            "turn/completed": self._turn_completed,
            "item/started": self._item_started,
            "item/completed": self._item_completed,
            "turn/plan/updated": self._plan_updated,
            "thread/tokenUsage/updated": self._token_usage,
            "account/rateLimits/updated": self._rate_limits_updated,
            "serverRequest/resolved": self._server_request_resolved,
        }.get(method)
        if handler is None:
            return []  # 未知事件：前向兼容，忽略（reducer 同样兜底）
        return handler(params, at_ms)

    def _thread_started(self, params, at_ms):
        thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
        thread_id = thread.get("id")
        if not thread_id:
            return []
        cwd = thread.get("cwd")
        project = os.path.basename(str(cwd).rstrip("/")) if isinstance(cwd, str) else ""
        return [ev.thread_started(thread_id, scrub_text(project, 96) if project else "",
                                  at_ms=at_ms)]

    def _thread_status(self, params, at_ms):
        thread_id = params.get("threadId")
        status = params.get("status") if isinstance(params.get("status"), dict) else {}
        stype = status.get("type")
        if not thread_id:
            return []
        if stype == ev.THREAD_STATUS_ACTIVE:
            flags = tuple(f for f in (status.get("activeFlags") or ())
                          if isinstance(f, str))
            events = [ev.thread_status(thread_id, ev.THREAD_STATUS_ACTIVE, flags, at_ms=at_ms)]
            # activeFlags 确认 needs_you：reducer 在 pending 非空时保持 NEEDS YOU。
            # 若等待标志在、但没有（或还没收到）请求载荷（如 adapter 重启错过请求），
            # 合成一个只读 pending，保证"等待可观察"（STATUS.md P1.1 遗留项）。
            waiting = [f for f in flags if f in WAITING_FLAGS]
            if waiting and not self._pending_real.get(thread_id) \
                    and thread_id not in self._synthetic:
                synthetic_id = self._synthetic_next
                self._synthetic_next -= 1
                self._synthetic[thread_id] = synthetic_id
                label = ("等待用户输入" if waiting[0] == FLAG_WAITING_USER_INPUT else "等待审批")
                events.append(ev.approval_requested(
                    thread_id, synthetic_id,
                    "上游标记%s（未收到请求载荷，只读等待）" % label, at_ms=at_ms))
            return events
        if stype == ev.THREAD_STATUS_IDLE:
            return [ev.thread_status(thread_id, ev.THREAD_STATUS_IDLE, at_ms=at_ms)]
        if stype == ev.THREAD_STATUS_SYSTEM_ERROR:
            return [ev.thread_status(thread_id, ev.THREAD_STATUS_SYSTEM_ERROR, at_ms=at_ms)]
        return []  # notLoaded 等：不产生事件

    def _turn_started(self, params, at_ms):
        thread_id = params.get("threadId")
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = turn.get("id")
        if not thread_id or not turn_id:
            return []
        # 新 turn：清 adapter 侧 pending 记账（reducer 侧由 turn_started 清）。
        self._pending_real[thread_id] = set()
        self._synthetic.pop(thread_id, None)
        return [ev.turn_started(thread_id, turn_id, at_ms=at_ms)]

    def _turn_completed(self, params, at_ms):
        thread_id = params.get("threadId")
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = turn.get("id")
        status = _turn_status_normalize(turn.get("status"))
        if not thread_id or not turn_id or status is None:
            return []
        summary = None
        if status == ev.TURN_STATUS_FAILED and isinstance(turn.get("error"), dict):
            message = turn["error"].get("message")
            if isinstance(message, str):
                summary = scrub_text(message, 192)
        self._pending_real.pop(thread_id, None)
        self._synthetic.pop(thread_id, None)
        return [ev.turn_completed(thread_id, turn_id, status, summary=summary, at_ms=at_ms)]

    def _item_started(self, params, at_ms):
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        if not thread_id or not item.get("type"):
            return []
        return [ev.item_started(thread_id, turn_id, str(item.get("type")),
                                item_id=item.get("id"),
                                summary=_item_summary(item), at_ms=at_ms)]

    def _item_completed(self, params, at_ms):
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        if not thread_id or not item.get("type"):
            return []
        # item/completed 不携带任何正文（完整工具输出/回复文本不入快照）。
        return [ev.item_completed(thread_id, turn_id, str(item.get("type")),
                                  item_id=item.get("id"), at_ms=at_ms)]

    def _plan_updated(self, params, at_ms):
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        plan = params.get("plan")
        if not thread_id or not isinstance(plan, list):
            return []
        steps = []
        for step in plan:
            if not isinstance(step, dict):
                continue
            text = step.get("step")
            if not isinstance(text, str):
                continue
            steps.append((scrub_text(text, 128), _plan_step_status(step.get("status"))))
        if not steps:
            return []
        return [ev.plan_updated(thread_id, turn_id, tuple(steps),
                                total=len(steps), at_ms=at_ms)]

    def _token_usage(self, params, at_ms):
        thread_id = params.get("threadId")
        usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
        total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
        used = total.get("totalTokens")
        capacity = usage.get("modelContextWindow")
        if not thread_id or not isinstance(used, int):
            return []
        if capacity is not None and not isinstance(capacity, int):
            capacity = None
        # 注意：totalTokens 含系统指令开销（CODEX_CAPABILITIES 矩阵第 5 行），
        # reducer/render 只按"当前 context 占用"呈现，不做累计消耗换算。
        return [ev.token_usage(thread_id, used, capacity,
                               turn_id=params.get("turnId"), at_ms=at_ms)]

    def _rate_limits_updated(self, params, at_ms):
        rate_limits = params.get("rateLimits")
        windows = windows_from_rate_limits(rate_limits)
        if not windows:
            return []  # 稀疏推送且无窗口数据：保留旧值，不清除
        for window in windows:
            self._last_windows[window["id"]] = dict(window)
        merged = [dict(self._last_windows[k]) for k in
                  ("codex-primary", "codex-secondary") if k in self._last_windows]
        return [ev.rate_limits(merged, at_ms=at_ms)]

    def _server_request_resolved(self, params, at_ms):
        thread_id = params.get("threadId")
        request_id = params.get("requestId")
        if not thread_id or not isinstance(request_id, int):
            return []
        self._pending_real.get(thread_id, set()).discard(request_id)
        if self._synthetic.get(thread_id) == request_id:
            self._synthetic.pop(thread_id, None)
        return [ev.server_request_resolved(thread_id, request_id, at_ms=at_ms)]

    # ---- server->client requests（审批族；只接收不回应）--------------------
    def server_request(self, msg):
        method = msg.get("method")
        request_id = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        self.server_requests_seen.append({"method": method, "id": request_id})
        if method not in APPROVAL_REQUEST_METHODS:
            return []
        if not isinstance(request_id, int):
            return []
        thread_id = params.get("threadId")
        if not thread_id:
            return []  # 无线程归属（如部分 elicitation）：无法映射，只记录
        summary = self._approval_summary(method, params)
        turn_id = params.get("turnId")
        events = []
        # 合成 pending 换成真实请求：先撤合成项，避免重复计数。
        synthetic_id = self._synthetic.pop(thread_id, None)
        if synthetic_id is not None:
            events.append(ev.server_request_resolved(thread_id, synthetic_id))
        self._pending_real.setdefault(thread_id, set()).add(request_id)
        events.append(ev.approval_requested(thread_id, request_id, summary,
                                            turn_id=turn_id))
        return events

    @staticmethod
    def _approval_summary(method, params):
        for key in ("command", "path", "query", "summary", "message", "reason", "prompt"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return scrub_text(value, 192)
        return scrub_text("审批请求：%s" % method, 192)


# ---------------------------------------------------------------------------

@dataclass
class CodexRunResult:
    snapshots: list
    report: dict
    raw_log: list
    exit_code: int
    exit_reason: str


@dataclass
class _Attempt:
    n: int
    ended_by: str = "process_exit"
    turn_status: Optional[str] = None
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    events_seen: int = 0
    error: Optional[str] = None

    def as_dict(self):
        return {
            "attempt": self.n,
            "ended_by": self.ended_by,
            "turn_status": self.turn_status,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "events_seen": self.events_seen,
            "error": self.error,
        }


class CodexAdapter:
    """SourceAdapter：起/管 app-server 子进程，事件映射并喂 StateEngine。

    使用：构造 → run() → CodexRunResult（快照序列、报告、脱敏原始日志、退出码）。
    可注入 server_argv（测试用假 app-server）；真实运行默认 [codex_bin, "app-server"]。
    """

    def __init__(self, prompt: str = DEFAULT_PROMPT, *, model: str = DEFAULT_MODEL,
                 codex_bin: str = "codex", server_argv=None,
                 backoff=DEFAULT_BACKOFF, jitter: float = 0.15,
                 connect_timeout: float = 15.0, start_timeout: float = 30.0,
                 turn_timeout: float = 120.0, interrupt_grace: float = 10.0,
                 epoch: Optional[str] = None, rng=None) -> None:
        if not prompt or not isinstance(prompt, str):
            raise ValueError("prompt must be a non-empty string")
        if not model:
            raise ValueError("model must be pinned explicitly (0.152.0 rejects the "
                             "account default gpt-6-astra)")
        self.prompt = prompt
        self.model = model
        self.codex_bin = codex_bin
        self.server_argv = list(server_argv) if server_argv is not None else None
        self.backoff = tuple(float(x) for x in backoff)
        self.jitter = float(jitter)
        self.connect_timeout = float(connect_timeout)
        self.start_timeout = float(start_timeout)
        self.turn_timeout = float(turn_timeout)
        self.interrupt_grace = float(interrupt_grace)
        self.epoch = epoch
        self._rng = rng or random.Random()
        self.snapshots: list = []
        self.raw_log: list = []
        self.capabilities: dict = {}
        self.attempts: list = []
        self.engine: Optional[StateEngine] = None
        self.mapper: Optional[CodexEventMapper] = None
        self._ran = False

    # ---- 时钟（live 用真实单调钟 + 固定锚点；engine 保持无墙钟约定）---------
    @staticmethod
    def _mono_ms() -> int:
        return int(time.monotonic() * 1000)

    @staticmethod
    def _wall_ms() -> int:
        return int(time.time() * 1000)

    def _sleep_backoff(self, seconds: float) -> None:
        delay = seconds
        if self.jitter > 0 and seconds > 0:
            delay += self._rng.uniform(0.0, self.jitter * seconds)
        time.sleep(delay)

    # ---- 主流程 ------------------------------------------------------------
    def run(self) -> CodexRunResult:
        if self._ran:
            raise RuntimeError("adapter already ran; construct a new instance")
        self._ran = True
        wall0 = self._wall_ms()
        anchor = wall0 - self._mono_ms()
        if self.epoch is None:
            self.epoch = "codex-%d" % wall0
        self.engine = StateEngine(self.epoch, source_kind=SOURCE_BRIDGE_OWNED,
                                  utc_anchor_ms=anchor)
        self.mapper = CodexEventMapper()

        exit_code = EXIT_PROCESS_EXITED
        exit_reason = "process_exited_repeatedly"
        schedule = (0.0,) + self.backoff
        for attempt_no, delay in enumerate(schedule):
            if attempt_no > 0:
                self._sleep_backoff(delay)
            attempt = self._run_session(attempt_no)
            self.attempts.append(attempt.as_dict())
            if attempt.ended_by == "turn_completed":
                exit_reason = "turn_completed"
                exit_code = {
                    ev.TURN_STATUS_COMPLETED: EXIT_TURN_COMPLETED,
                    ev.TURN_STATUS_FAILED: EXIT_TURN_FAILED,
                    ev.TURN_STATUS_CANCELLED: EXIT_TURN_INTERRUPTED,
                }.get(attempt.turn_status, EXIT_TURN_TIMEOUT)
                break
            if attempt.ended_by == "process_exit" or attempt.ended_by == "session_error":
                continue  # 指数退避重启
            if attempt.ended_by == "capability_missing":
                exit_code, exit_reason = EXIT_CAPABILITY_MISSING, "capability_missing"
                break
            # turn_timeout：无可靠终态，不伪造 DONE/ERROR，直接报告超时
            exit_code, exit_reason = EXIT_TURN_TIMEOUT, "turn_timeout"
            break

        report = self._build_report(exit_code, exit_reason)
        return CodexRunResult(snapshots=list(self.snapshots), report=report,
                              raw_log=list(self.raw_log), exit_code=exit_code,
                              exit_reason=exit_reason)

    # ---- 单次会话（一个 app-server 生命周期）--------------------------------
    def _run_session(self, attempt_no: int) -> _Attempt:
        attempt = _Attempt(n=attempt_no)
        tmpdir = tempfile.mkdtemp(prefix="codex-bridge-")
        argv = self.server_argv if self.server_argv is not None \
            else [self.codex_bin, "app-server"]
        client = AppServerClient(argv, cwd=tmpdir)
        try:
            client.start()
        except OSError as exc:
            attempt.ended_by = "session_error"
            attempt.error = scrub_text(str(exc), 200)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return attempt
        try:
            self._session_inner(client, tmpdir, attempt)
        except RuntimeError as exc:  # app-server 进程中途退出（请求得不到应答）
            attempt.ended_by = "process_exit"
            attempt.error = scrub_text(str(exc), 200)
            if self.engine is not None:
                self._apply([ev.source_disconnected()],
                            {"dir": "bridge", "note": "app-server process exited"})
        finally:
            client.stop()
            shutil.rmtree(tmpdir, ignore_errors=True)
        return attempt

    def _session_inner(self, client: AppServerClient, tmpdir: str, attempt: _Attempt) -> None:
        caps = self.capabilities
        try:
            init = client.request("initialize", INITIALIZE_PARAMS,
                                  timeout=self.connect_timeout)
        except RpcTimeout as exc:
            attempt.ended_by, attempt.error = "session_error", scrub_text(str(exc), 200)
            return
        except RpcError as exc:
            attempt.ended_by, attempt.error = "capability_missing", scrub_text(str(exc), 200)
            return
        caps["initialize"] = True
        user_agent = init.get("userAgent") if isinstance(init, dict) else None
        caps["user_agent"] = scrub_text(user_agent or "", 200)
        version = re.search(r"(\d+\.\d+(?:\.\d+)?)", caps.get("user_agent") or "")
        caps["cli_version"] = version.group(1) if version else None

        # ---- 能力检测：冻结 schema 方法清单核对（启动即做）----
        declared, have_server_requests = _declared_methods()
        caps["methods_baseline"] = "docs/proto-samples schema 0.152.0" \
            if declared is not None else "unknown"
        caps["declared"] = {m: (m in declared if declared is not None else None)
                            for m in CAPABILITY_METHODS}
        if declared is not None and not have_server_requests:
            # 存档缺 server→client 桶时标记未知，不误报审批能力缺失
            # （审批族的存在性已由 P0.2 探针 6b/7 实测）。
            for meth in APPROVAL_REQUEST_METHODS:
                if meth in caps["declared"]:
                    caps["declared"][meth] = None
        if declared is not None and "thread/start" not in declared:
            attempt.ended_by = "capability_missing"
            attempt.error = "thread/start missing from frozen 0.152.0 method list"
            return

        # （重）连成功：source_reconnected → 快照体现 connected 翻转 / stale 清除。
        self._apply([ev.source_reconnected()],
                    {"dir": "bridge", "note": "connected"})

        # ---- 启动额度读取（account/rateLimits/read 实探；失败→unavailable 降级）----
        self._apply(self._startup_rate_limits(client),
                    {"dir": "bridge", "note": "startup account/rateLimits/read"})

        # ---- ephemeral thread（探针 7 安全参数）----
        thread_params = {
            "cwd": tmpdir,
            "ephemeral": True,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "model": self.model,
        }
        self._log_raw({"dir": "out", "method": "thread/start",
                       "payload": scrub({"params": thread_params})})
        try:
            started = client.request("thread/start", thread_params,
                                     timeout=self.start_timeout)
        except RpcError as exc:
            if exc.code == -32601:
                attempt.ended_by = "capability_missing"
            else:
                attempt.ended_by = "session_error"
            attempt.error = scrub_text(str(exc), 200)
            return
        except RpcTimeout as exc:
            attempt.ended_by, attempt.error = "session_error", scrub_text(str(exc), 200)
            return
        caps["thread_start_executed"] = True
        started = started if isinstance(started, dict) else {}
        thread = started.get("thread") if isinstance(started.get("thread"), dict) else {}
        thread_id = thread.get("id")
        caps["model_confirmed"] = started.get("model")
        attempt.thread_id = thread_id

        self._log_raw({"dir": "out", "method": "turn/start",
                       "payload": scrub({"params": {"threadId": thread_id,
                                                    "input": [{"type": "text",
                                                               "text": self.prompt}]}})})
        try:
            turn = client.request(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": self.prompt}]},
                timeout=self.start_timeout)
        except RpcError as exc:
            if exc.code == -32601:
                attempt.ended_by = "capability_missing"
            else:
                attempt.ended_by = "session_error"
            attempt.error = scrub_text(str(exc), 200)
            return
        except RpcTimeout as exc:
            attempt.ended_by, attempt.error = "session_error", scrub_text(str(exc), 200)
            return
        caps["turn_start_executed"] = True
        turn = turn if isinstance(turn, dict) else {}
        turn_obj = turn.get("turn") if isinstance(turn.get("turn"), dict) else {}
        turn_id = turn_obj.get("id")
        attempt.turn_id = turn_id

        # ---- 事件泵：直到本 turn 终态 / 进程退出 / 超时 ----
        deadline = self._mono_ms() + int(self.turn_timeout * 1000)
        while True:
            if not client.alive():
                attempt.ended_by = "process_exit"
                self._apply([ev.source_disconnected()],
                            {"dir": "bridge", "note": "app-server process exited"})
                return
            now = self._mono_ms()
            if now >= deadline:
                attempt.ended_by = "turn_timeout"
                break
            got = client.wait_any(timeout=min(0.5, max(0.05, (deadline - now) / 1000.0)))
            if got is None:
                continue
            kind, msg = got
            self._handle_inbound(kind, msg, attempt)
            if kind == KIND_NOTIFICATION:
                method = msg.get("method")
                if method == "turn/completed":
                    params = msg.get("params") or {}
                    turn_obj2 = params.get("turn") or {}
                    if params.get("threadId") == thread_id and turn_obj2.get("id") == turn_id:
                        attempt.ended_by = "turn_completed"
                        attempt.turn_status = turn_obj2.get("status")
                        return
                elif method == "error":
                    attempt.ended_by = "error_notification"
                    break

        # ---- 停滞兜底：只 interrupt 我们自己的受控 turn（不碰审批请求本身），
        # 等上游给出真实终态（探针 6b 先例：interrupt → interrupted → serverRequest/resolved）。
        if attempt.ended_by in ("error_notification", "turn_timeout") \
                and client.alive() and thread_id and turn_id:
            self._log_raw({"dir": "out", "method": "turn/interrupt",
                           "payload": scrub({"params": {"threadId": thread_id,
                                                        "turnId": turn_id}})})
            try:
                client.request("turn/interrupt",
                               {"threadId": thread_id, "turnId": turn_id}, timeout=10.0)
            except (RpcError, RpcTimeout, RuntimeError):
                pass
            grace_end = self._mono_ms() + int(self.interrupt_grace * 1000)
            while client.alive() and self._mono_ms() < grace_end:
                got = client.wait_any(timeout=0.3)
                if got is None:
                    continue
                kind, msg = got
                self._handle_inbound(kind, msg, attempt)
                if kind == KIND_NOTIFICATION and msg.get("method") == "turn/completed":
                    params = msg.get("params") or {}
                    turn_obj2 = params.get("turn") or {}
                    if params.get("threadId") == thread_id and turn_obj2.get("id") == turn_id:
                        attempt.ended_by = "turn_completed"
                        attempt.turn_status = turn_obj2.get("status")
                        return
            if not client.alive():
                # 宽限期内进程又死了：按进程退出处理，交给外层退避重启。
                attempt.ended_by = "process_exit"
                self._apply([ev.source_disconnected()],
                            {"dir": "bridge", "note": "app-server process exited"})
            # 否则保持 error_notification / turn_timeout（无可靠终态，不伪造）。

    def _handle_inbound(self, kind: str, msg: dict, attempt: _Attempt) -> None:
        self._log_raw({"dir": "in", "kind": kind, "payload": scrub(msg)})
        if kind == KIND_SERVER_REQUEST:
            events = self.mapper.server_request(msg)
        else:
            events = self.mapper.notification(msg.get("method"), msg.get("params"),
                                              at_ms=msg.get("emittedAtMs"))
        attempt.events_seen += 1
        self._apply(events, None)

    def _startup_rate_limits(self, client: AppServerClient) -> list:
        try:
            resp = client.request("account/rateLimits/read", None,
                                  timeout=self.connect_timeout)
        except (RpcError, RpcTimeout) as exc:
            self.capabilities["rate_limits_read"] = False
            self.capabilities["rate_limits_error"] = scrub_text(str(exc), 200)
            return [ev.rate_limits_unavailable()]
        rate_limits = resp.get("rateLimits") if isinstance(resp, dict) else None
        windows = windows_from_rate_limits(rate_limits)
        if windows:
            self.capabilities["rate_limits_read"] = True
            self.capabilities["rate_limits_startup"] = [dict(w) for w in windows]
            return [ev.rate_limits(windows)]
        self.capabilities["rate_limits_read"] = False
        self.capabilities["rate_limits_error"] = "response had no usable windows"
        return [ev.rate_limits_unavailable()]

    # ---- 落盘辅助 ------------------------------------------------------------
    def _apply(self, events, raw_note) -> None:
        for event in events:
            snapshot = self.engine.apply(event, self._mono_ms())
            self.snapshots.append(snapshot)
        if raw_note is not None:
            self._log_raw(raw_note)

    def _log_raw(self, entry: dict) -> None:
        # 原始日志入列前已逐条 scrub（凭证/邮箱/home 路径/用户内容）。
        self.raw_log.append({"at_ms": self._wall_ms(), **entry})

    def _build_report(self, exit_code: int, exit_reason: str) -> dict:
        last = self.snapshots[-1] if self.snapshots else None
        return {
            "task": "P3.1",
            "bridge_epoch": self.epoch,
            "source_kind": SOURCE_BRIDGE_OWNED,
            "model": self.model,
            "prompt": "<content len=%d>" % len(self.prompt),
            "capabilities": self.capabilities,
            "attempts": self.attempts,
            "exit_code": exit_code,
            "exit_reason": exit_reason,
            "snapshot_count": len(self.snapshots),
            "last_usage": last.get("usage") if last else None,
            "notifications_seen": dict(self.mapper.notifications_seen),
            "server_requests_seen": list(self.mapper.server_requests_seen),
            "redaction": "raw IO log scrubbed via bridge/redact.py; "
                         "credentials/emails/home paths/user content excluded",
        }


# ---- CLI / 工件 ------------------------------------------------------------

def write_artifacts(out_dir: str, result: CodexRunResult) -> list:
    """快照 JSONL、脱敏原始日志、运行报告写进 out_dir；返回文件路径列表。"""
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for snap in result.snapshots:
        path = os.path.join(out_dir, "snapshot_%04d.json" % snap["seq"])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, ensure_ascii=False, separators=(",", ":")) + "\n")
        paths.append(path)
    raw_path = os.path.join(out_dir, "events_raw_redacted.jsonl")
    with open(raw_path, "w", encoding="utf-8") as fh:
        for entry in result.raw_log:
            fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    paths.append(raw_path)
    report_path = os.path.join(out_dir, "report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(result.report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    paths.append(report_path)
    return paths


def state_flow(snapshots) -> list:
    """(seq, selected_thread_state) 状态流转（报告/冒烟用）。"""
    flow = []
    for snap in snapshots:
        threads = snap.get("threads") or []
        flow.append((snap["seq"], threads[0]["state"] if threads else None))
    return flow


def run_cli(args) -> int:
    """`python -m bridge --source codex ...` 的实现（由 bridge/__main__.py 分发）。"""
    model = args.model or DEFAULT_MODEL
    if not args.live:
        # dry-run：不启动任何进程、不建目录，只打印将执行的计划（安全默认）。
        plan = {
            "mode": "dry-run (pass --live to actually run codex app-server)",
            "codex_bin": args.codex_bin,
            "model": model,
            "prompt": args.prompt,
            "out": args.out,
            "sandbox": {"type": "readOnly", "networkAccess": False},
            "approvalPolicy": "never",
            "ephemeral": True,
            "turn_timeout_s": args.turn_timeout,
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    adapter = CodexAdapter(
        prompt=args.prompt, model=model, codex_bin=args.codex_bin,
        turn_timeout=args.turn_timeout, epoch=args.epoch,
    )
    result = adapter.run()
    if args.out:
        write_artifacts(args.out, result)
        print(
            "bridge: wrote %d snapshots to %s (source=codex, exit_code=%d, reason=%s)"
            % (len(result.snapshots), args.out, result.exit_code, result.exit_reason),
            file=sys.stderr,
        )
    else:
        for snap in result.snapshots:
            sys.stdout.write(json.dumps(snap, ensure_ascii=False,
                                        separators=(",", ":")) + "\n")
    return result.exit_code
