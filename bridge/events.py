"""NormalizedEvent：Bridge 内部规范化事件类型（P1.1）。

字段命名与 docs/CODEX_CAPABILITIES.md 的 codex 0.152.0 实测事件对齐，
映射关系（app-server 方法 → 本事件 type）：

    thread/started                          -> thread_started
    thread/status/changed                   -> thread_status_changed
    turn/started                            -> turn_started
    turn/completed                          -> turn_completed
    item/started                            -> item_started
    item/completed                          -> item_completed
    turn/plan/updated                       -> plan_updated
    thread/tokenUsage/updated               -> token_usage_updated
    account/rateLimits/read | .../updated   -> rate_limits_updated
    （额度不可用/未登录，adapter 推导）      -> rate_limits_unavailable
    item/commandExecution/requestApproval、
    item/fileChange/requestApproval、
    item/permissions/requestApproval、
    item/tool/requestUserInput、
    mcpServer/elicitation/request           -> approval_requested
    serverRequest/resolved                  -> server_request_resolved
    （bridge 本地：用户选中线程）            -> select_thread
    （bridge 本地：上游断连/重连信号）       -> source_disconnected / source_reconnected

约定：
- 事件是"已脱敏的最小字段"（INTERFACES §5 SourceAdapter 行）；summary/project 由
  adapter（或 mock）提供，凭证与完整输出不允许进入本类型。
- at_ms 是上游事件时间（emittedAtMs，UTC Unix 毫秒或 None）。replay 时它同时
  充当虚拟单调时钟：reduce(event, monotonic_now) 的单调毫秒由回放器从 at_ms 提供。
- reducer 只消费本类型；未知 type 被忽略（前向兼容），不抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# 事件类型常量（上表左列的规范化名）。
EVENT_THREAD_STARTED = "thread_started"
EVENT_THREAD_STATUS = "thread_status_changed"
EVENT_TURN_STARTED = "turn_started"
EVENT_TURN_COMPLETED = "turn_completed"
EVENT_ITEM_STARTED = "item_started"
EVENT_ITEM_COMPLETED = "item_completed"
EVENT_PLAN_UPDATED = "plan_updated"
EVENT_TOKEN_USAGE = "token_usage_updated"
EVENT_RATE_LIMITS = "rate_limits_updated"
EVENT_RATE_LIMITS_UNAVAILABLE = "rate_limits_unavailable"
EVENT_APPROVAL_REQUESTED = "approval_requested"
EVENT_SERVER_REQUEST_RESOLVED = "server_request_resolved"
EVENT_SELECT_THREAD = "select_thread"
EVENT_SOURCE_DISCONNECTED = "source_disconnected"
EVENT_SOURCE_RECONNECTED = "source_reconnected"

# turn/completed 的 status 取值（end_reason 语义，INTERFACES §3 end_reason 行）。
TURN_STATUS_COMPLETED = "completed"
TURN_STATUS_FAILED = "failed"
TURN_STATUS_CANCELLED = "cancelled"

# thread/status/changed 的 status 取值（0.152.0 实测 active/idle/notLoaded/systemError）。
THREAD_STATUS_ACTIVE = "active"
THREAD_STATUS_IDLE = "idle"
THREAD_STATUS_SYSTEM_ERROR = "systemError"

# plan step 的规范化 status（上游 inProgress 由 adapter 转成 in_progress 后才进来）。
PLAN_PENDING = "pending"
PLAN_IN_PROGRESS = "in_progress"
PLAN_COMPLETED = "completed"

PlanStep = tuple  # (text: str, status: str)
UsageWindow = dict  # {"id","label","used_percent","duration_mins","resets_at_ms"}


@dataclass(frozen=True)
class NormalizedEvent:
    """一个规范化事件。所有字段除 type 外均可缺省。

    summary 是已脱敏的单行摘要（命令行/消息预览/批准说明等），允许中文；
    adapter 负责截断与脱敏，reducer/render 仍会按冻结字节上限二次裁剪。
    """

    type: str
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    item_id: Optional[str] = None
    request_id: Optional[int] = None
    at_ms: Optional[int] = None
    # turn_completed: completed|failed|cancelled；thread_status_changed: active|idle|systemError
    status: Optional[str] = None
    active_flags: tuple = field(default=())
    item_kind: Optional[str] = None  # reasoning|commandExecution|agentMessage|...
    summary: Optional[str] = None
    project: Optional[str] = None
    plan_steps: tuple = field(default=())  # ((text, status), ...)
    plan_total: Optional[int] = None
    used_tokens: Optional[int] = None
    capacity_tokens: Optional[int] = None
    windows: tuple = field(default=())  # (UsageWindow, ...)
    # select_thread 用：None 表示清除本地选中，回到"默认选首项"。
    select_thread_id: Optional[str] = None

    def to_dict(self) -> dict:
        """转成可写入 JSONL 的 dict；省略缺省字段，元组转列表。"""
        d: dict = {"type": self.type}
        if self.thread_id is not None:
            d["thread_id"] = self.thread_id
        if self.turn_id is not None:
            d["turn_id"] = self.turn_id
        if self.item_id is not None:
            d["item_id"] = self.item_id
        if self.request_id is not None:
            d["request_id"] = self.request_id
        if self.at_ms is not None:
            d["at_ms"] = self.at_ms
        if self.status is not None:
            d["status"] = self.status
        if self.active_flags:
            d["active_flags"] = list(self.active_flags)
        if self.item_kind is not None:
            d["item_kind"] = self.item_kind
        if self.summary is not None:
            d["summary"] = self.summary
        if self.project is not None:
            d["project"] = self.project
        if self.plan_steps:
            d["plan_steps"] = [[t, s] for (t, s) in self.plan_steps]
        if self.plan_total is not None:
            d["plan_total"] = self.plan_total
        if self.used_tokens is not None:
            d["used_tokens"] = self.used_tokens
        if self.capacity_tokens is not None:
            d["capacity_tokens"] = self.capacity_tokens
        if self.windows:
            d["windows"] = [dict(w) for w in self.windows]
        if self.type == EVENT_SELECT_THREAD:
            d["select_thread_id"] = self.select_thread_id
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "NormalizedEvent":
        """从 JSONL 行还原事件；缺省字段取默认值。type 必填。"""
        if not isinstance(d, dict) or not isinstance(d.get("type"), str):
            raise ValueError("event line must be an object with a string 'type'")
        steps = tuple((s[0], s[1]) for s in d.get("plan_steps", ()))
        ev = cls(
            type=d["type"],
            thread_id=d.get("thread_id"),
            turn_id=d.get("turn_id"),
            item_id=d.get("item_id"),
            request_id=d.get("request_id"),
            at_ms=d.get("at_ms"),
            status=d.get("status"),
            active_flags=tuple(d.get("active_flags", ())),
            item_kind=d.get("item_kind"),
            summary=d.get("summary"),
            project=d.get("project"),
            plan_steps=steps,
            plan_total=d.get("plan_total"),
            used_tokens=d.get("used_tokens"),
            capacity_tokens=d.get("capacity_tokens"),
            windows=tuple(dict(w) for w in d.get("windows", ())),
            select_thread_id=d.get("select_thread_id"),
        )
        return ev


# 便捷构造器（保持调用点可读；字段与上面一致）。


def thread_started(thread_id: str, project: str = "", at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_THREAD_STARTED, thread_id=thread_id, project=project, at_ms=at_ms)


def thread_status(thread_id: str, status: str, active_flags=(), at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(
        EVENT_THREAD_STATUS, thread_id=thread_id, status=status,
        active_flags=tuple(active_flags), at_ms=at_ms,
    )


def turn_started(thread_id: str, turn_id: str, summary: str | None = None,
                 at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_TURN_STARTED, thread_id=thread_id, turn_id=turn_id,
                           summary=summary, at_ms=at_ms)


def turn_completed(thread_id: str, turn_id: str, status: str, summary: str | None = None,
                   at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_TURN_COMPLETED, thread_id=thread_id, turn_id=turn_id,
                           status=status, summary=summary, at_ms=at_ms)


def item_started(thread_id: str, turn_id: str, item_kind: str, item_id: str | None = None,
                 summary: str | None = None, at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_ITEM_STARTED, thread_id=thread_id, turn_id=turn_id,
                           item_id=item_id, item_kind=item_kind, summary=summary, at_ms=at_ms)


def item_completed(thread_id: str, turn_id: str, item_kind: str, item_id: str | None = None,
                   at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_ITEM_COMPLETED, thread_id=thread_id, turn_id=turn_id,
                           item_id=item_id, item_kind=item_kind, at_ms=at_ms)


def plan_updated(thread_id: str, turn_id: str, steps, total: int | None = None,
                 at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_PLAN_UPDATED, thread_id=thread_id, turn_id=turn_id,
                           plan_steps=tuple(steps), plan_total=total, at_ms=at_ms)


def token_usage(thread_id: str, used_tokens: int | None, capacity_tokens: int | None,
                turn_id: str | None = None, at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_TOKEN_USAGE, thread_id=thread_id, turn_id=turn_id,
                           used_tokens=used_tokens, capacity_tokens=capacity_tokens, at_ms=at_ms)


def rate_limits(windows, at_ms: int | None = None) -> NormalizedEvent:
    """windows: 序列 of UsageWindow dicts（id/label/used_percent/duration_mins/resets_at_ms）。"""
    return NormalizedEvent(EVENT_RATE_LIMITS, windows=tuple(dict(w) for w in windows), at_ms=at_ms)


def rate_limits_unavailable(at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_RATE_LIMITS_UNAVAILABLE, at_ms=at_ms)


def approval_requested(thread_id: str, request_id: int, summary: str,
                       turn_id: str | None = None, at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_APPROVAL_REQUESTED, thread_id=thread_id, turn_id=turn_id,
                           request_id=request_id, summary=summary, at_ms=at_ms)


def server_request_resolved(thread_id: str, request_id: int,
                            at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_SERVER_REQUEST_RESOLVED, thread_id=thread_id,
                           request_id=request_id, at_ms=at_ms)


def select_thread(thread_id: str | None, at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_SELECT_THREAD, select_thread_id=thread_id, at_ms=at_ms)


def source_disconnected(at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_SOURCE_DISCONNECTED, at_ms=at_ms)


def source_reconnected(at_ms: int | None = None) -> NormalizedEvent:
    return NormalizedEvent(EVENT_SOURCE_RECONNECTED, at_ms=at_ms)
