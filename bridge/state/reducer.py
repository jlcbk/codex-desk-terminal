"""纯 reducer（P1.1）。

规则真源：docs/INTERFACES.md §3（已冻结）。要点映射：

- keyed by thread_id（turn 维度以 rec.turn_id + turn_finished 门闸实现）；
  turn 开始清除旧 plan/pending/计时，新 turn 不继承 DONE。
- THINKING 只在明确上游活动（reasoning item）可辨认时使用；其余活动阶段显示 WORKING。
- pending 集合非空即 NEEDS YOU；本地静音属于 DeviceRuntime，本 reducer 不处理静音。
  只有明确的 approval_requested / server_request_resolved / turn 终态增删 pending；
  其他 item 事件不能抢掉等待状态。
- 同线程多个 pending 保留计数；有可靠失败终态 ERROR，成功终态 DONE，
  取消 IDLE+cancelled。终态清理该 turn pending 并禁止迟到事件复活该 turn。
- PLAN UPDATE 只改 plan，不改状态（保持 WORKING；等待中也不改变 NEEDS YOU）。
- 上游断连不把任务改成 IDLE：只置 source connected=False / stale=True。
- 排序与裁剪在 render.py（渲染时执行），选中线程记录在本状态 user_selected。

纯函数约定：无 IO、无墙钟、无随机；reduce(state, event, monotonic_now) 只依据
入参修改并返回内部状态 dict。相同事件序列 + 相同时钟序列 ⇒ 相同输出。
时间约定：单调毫秒 now 仅用于 elapsed/waiting/updated 的相对计算；
渲染时由 engine 用固定 utc_anchor 映射成 *_at_ms（UTC 毫秒），保证确定性。
"""

from __future__ import annotations

from typing import Optional

from .. import events as ev

# 业务状态（INTERFACES §3 state 行）。
ST_IDLE = "idle"
ST_THINKING = "thinking"
ST_WORKING = "working"
ST_NEEDS_YOU = "needs_you"
ST_DONE = "done"
ST_ERROR = "error"

END_COMPLETED = "completed"
END_FAILED = "failed"
END_CANCELLED = "cancelled"

# pending request 摘要的兜底文案（与 mock 无关的 reducer 默认值，允许中文）。
_DEFAULT_SUMMARY = "待处理请求"


class ThreadRecord:
    """单线程内部记录（不直接输出；render 负责转换与裁剪）。"""

    __slots__ = (
        "id", "project", "turn_id", "state", "activity",
        "plan_total", "plan_steps",
        "pending", "turn_started_mono", "waiting_since_mono",
        "turn_finished", "finished_mono", "end_reason",
        "updated_mono", "used_tokens", "capacity_tokens",
    )

    def __init__(self, thread_id: str) -> None:
        self.id: str = thread_id
        self.project: str = ""
        self.turn_id: Optional[str] = None
        self.state: str = ST_IDLE
        self.activity: str = ""
        self.plan_total: int = 0
        self.plan_steps: list = []          # [(text, status)]
        self.pending: dict = {}             # request_id -> summary（插入序即发生序）
        self.turn_started_mono: Optional[int] = None
        self.waiting_since_mono: Optional[int] = None
        self.turn_finished: bool = False
        self.finished_mono: Optional[int] = None
        self.end_reason: Optional[str] = None
        self.updated_mono: int = 0
        self.used_tokens: Optional[int] = None
        self.capacity_tokens: Optional[int] = None


def new_internal_state() -> dict:
    """内部状态。threads 用插入序 dict（排序在渲染时做）。"""
    return {
        "threads": {},           # thread_id -> ThreadRecord
        "usage": {
            "available": False,
            "windows": [],       # [UsageWindow dict]
            "updated_mono": None,
        },
        "source_connected": True,
        "source_stale": False,
        "last_event_mono": None,
        "user_selected": None,   # 本地选中线程 id（select_thread 事件写入）
    }


def _record(state: dict, thread_id: Optional[str]) -> Optional[ThreadRecord]:
    if not thread_id:
        return None
    rec = state["threads"].get(thread_id)
    if rec is None:
        rec = ThreadRecord(thread_id)
        state["threads"][thread_id] = rec
    return rec


def _clear_waiting(rec: ThreadRecord) -> None:
    rec.pending.clear()
    rec.waiting_since_mono = None


def _effective_state(rec: ThreadRecord) -> str:
    """pending 非空即 NEEDS YOU（优先于其他状态）。"""
    if rec.pending:
        return ST_NEEDS_YOU
    return rec.state


def _turn_gate(rec: ThreadRecord, event) -> bool:
    """迟到事件门闸：turn 已终态、或事件属于旧 turn 时返回 True（应丢弃）。"""
    if rec.turn_finished:
        return True
    if rec.turn_id is not None and event.turn_id is not None and event.turn_id != rec.turn_id:
        return True
    return False


def reduce(state: dict, event, now_mono: int) -> dict:
    """应用一个事件；原地修改并返回 state。未知事件类型被忽略。"""
    state["last_event_mono"] = now_mono
    t = event.type

    if t == ev.EVENT_THREAD_STARTED:
        rec = _record(state, event.thread_id)
        if rec is not None:
            if event.project:
                rec.project = event.project
            # 已有线程不因重复 thread/started 改状态（不复活）。
            if rec.updated_mono == 0:
                rec.updated_mono = now_mono

    elif t == ev.EVENT_TURN_STARTED:
        rec = _record(state, event.thread_id)
        if rec is not None:
            if not (rec.turn_id == event.turn_id and not rec.turn_finished):
                # 新 turn（含未知线程首 turn）：清旧 plan/pending/计时；不继承 DONE。
                rec.turn_id = event.turn_id
                rec.plan_total = 0
                rec.plan_steps = []
                _clear_waiting(rec)
                rec.turn_started_mono = now_mono
                rec.turn_finished = False
                rec.finished_mono = None
                rec.end_reason = None
                rec.state = ST_WORKING
                rec.activity = event.summary if event.summary is not None else ""
                rec.updated_mono = now_mono

    elif t == ev.EVENT_ITEM_STARTED:
        rec = state["threads"].get(event.thread_id)
        if rec is not None and not _turn_gate(rec, event):
            if event.item_kind == "reasoning":
                # 明确可辨认的上游推理活动才允许 THINKING。
                rec.state = ST_THINKING
            else:
                # 未知工作阶段显示 WORKING，不从延迟猜"思考"。
                rec.state = ST_WORKING
            if event.summary is not None:
                rec.activity = event.summary
            rec.state = _effective_state(rec)
            rec.updated_mono = now_mono

    elif t == ev.EVENT_ITEM_COMPLETED:
        # item 完成不改变状态（等待/运行判定只由 turn/request/plan 驱动）。
        rec = state["threads"].get(event.thread_id)
        if rec is not None and not _turn_gate(rec, event):
            rec.updated_mono = now_mono

    elif t == ev.EVENT_PLAN_UPDATED:
        # 内容事件：只改 plan，不动 state（INTERFACES：PLAN UPDATE 保持 WORKING）。
        rec = state["threads"].get(event.thread_id)
        if rec is not None and not _turn_gate(rec, event):
            steps = []
            for step in event.plan_steps:
                text, status = step[0], step[1]
                if status == "inProgress":
                    status = ev.PLAN_IN_PROGRESS  # 防御：adapter 未归一化的上游拼写
                if status not in (ev.PLAN_PENDING, ev.PLAN_IN_PROGRESS, ev.PLAN_COMPLETED):
                    status = ev.PLAN_PENDING
                steps.append((text, status))
            rec.plan_steps = steps
            rec.plan_total = event.plan_total if event.plan_total is not None else len(steps)
            rec.updated_mono = now_mono

    elif t == ev.EVENT_APPROVAL_REQUESTED:
        rec = state["threads"].get(event.thread_id)
        if rec is not None and not _turn_gate(rec, event):
            rec.pending[event.request_id] = event.summary or _DEFAULT_SUMMARY
            if rec.waiting_since_mono is None:
                rec.waiting_since_mono = now_mono
            rec.state = _effective_state(rec)
            rec.updated_mono = now_mono

    elif t == ev.EVENT_SERVER_REQUEST_RESOLVED:
        rec = state["threads"].get(event.thread_id)
        if rec is not None:
            rec.pending.pop(event.request_id, None)
            if not rec.turn_finished:
                if rec.pending:
                    rec.state = ST_NEEDS_YOU
                else:
                    rec.waiting_since_mono = None
                    rec.state = ST_WORKING if rec.turn_id is not None else ST_IDLE
                    rec.updated_mono = now_mono

    elif t == ev.EVENT_TURN_COMPLETED:
        rec = _record(state, event.thread_id)
        if rec is not None and not rec.turn_finished:
            if not (rec.turn_id is not None and event.turn_id is not None
                    and event.turn_id != rec.turn_id):
                status = event.status
                if status == ev.TURN_STATUS_COMPLETED:
                    rec.state = ST_DONE
                    rec.end_reason = END_COMPLETED
                    if event.summary is not None:
                        rec.activity = event.summary
                elif status == ev.TURN_STATUS_FAILED:
                    rec.state = ST_ERROR
                    rec.end_reason = END_FAILED
                    rec.activity = event.summary if event.summary is not None else "任务失败"
                elif status == ev.TURN_STATUS_CANCELLED:
                    # cancelled 显示 idle 及取消说明（INTERFACES §3 end_reason 行）。
                    rec.state = ST_IDLE
                    rec.end_reason = END_CANCELLED
                    rec.activity = event.summary if event.summary is not None else "已取消"
                else:
                    return state  # 未知终态：忽略，不产生半应用
                _clear_waiting(rec)
                rec.turn_finished = True
                rec.finished_mono = now_mono
                rec.updated_mono = now_mono

    elif t == ev.EVENT_THREAD_STATUS:
        rec = state["threads"].get(event.thread_id)
        if rec is not None and not rec.turn_finished:
            status = event.status
            if status == ev.THREAD_STATUS_ACTIVE:
                # 有 pending 时等待优先；否则上游明确活动 → WORKING。
                if rec.pending:
                    if rec.waiting_since_mono is None:
                        rec.waiting_since_mono = now_mono
                    rec.state = ST_NEEDS_YOU
                else:
                    rec.state = ST_WORKING
                rec.updated_mono = now_mono
            elif status == ev.THREAD_STATUS_IDLE:
                if not rec.pending:  # pending 未解除前不能被 idle 抢掉 NEEDS YOU
                    rec.state = ST_IDLE
                    rec.updated_mono = now_mono
            elif status == ev.THREAD_STATUS_SYSTEM_ERROR:
                rec.state = ST_ERROR
                rec.updated_mono = now_mono

    elif t == ev.EVENT_TOKEN_USAGE:
        rec = state["threads"].get(event.thread_id)
        if rec is not None:
            rec.used_tokens = event.used_tokens
            rec.capacity_tokens = event.capacity_tokens
            if not rec.turn_finished:  # 迟到 usage 不复活该 turn 的排序时间
                rec.updated_mono = now_mono

    elif t == ev.EVENT_RATE_LIMITS:
        state["usage"]["available"] = True
        state["usage"]["windows"] = [dict(w) for w in event.windows]
        state["usage"]["updated_mono"] = now_mono

    elif t == ev.EVENT_RATE_LIMITS_UNAVAILABLE:
        state["usage"]["available"] = False
        state["usage"]["windows"] = []
        state["usage"]["updated_mono"] = now_mono

    elif t == ev.EVENT_SELECT_THREAD:
        if event.select_thread_id is None:
            state["user_selected"] = None
        elif event.select_thread_id in state["threads"]:
            state["user_selected"] = event.select_thread_id
        # 未知 id 忽略，保持上次选中。

    elif t == ev.EVENT_SOURCE_DISCONNECTED:
        # 上游断连：保持各线程状态不变，只标记链路（不全部转 IDLE、不伪造 DONE）。
        state["source_connected"] = False
        state["source_stale"] = True

    elif t == ev.EVENT_SOURCE_RECONNECTED:
        state["source_connected"] = True
        state["source_stale"] = False

    # 其他未知类型：忽略（前向兼容）。
    return state
