"""Mock Bridge 场景（P1.2）。

- 固定虚拟时钟：事件自带 at_ms（单调毫秒），StateEngine 用固定 utc_anchor
  映射为 *_at_ms；不用墙钟、不用随机 ⇒ 相同场景重复运行字节相同。
- 确定性 seq：每个事件产出一份快照，seq 从 1 严格递增。
- lifecycle 覆盖 DEVELOPMENT_PLAN P1.2 验收四阶段：
  WORKING → PLAN UPDATE → NEEDS YOU → DONE（另含 IDLE 起始快照）。
- 数据全部虚构，不含任何真实凭证/用户内容（红线）。
"""

from __future__ import annotations

from ..state.engine import StateEngine
from .. import events as ev

# 固定 UTC 锚点（对齐 INTERFACES §2 示例量级），仅用于 *_at_ms 映射。
ANCHOR_MS = 1789002000000
DEFAULT_EPOCH = "mock-run-001"

SCENARIO_LIFECYCLE = "lifecycle"
SCENARIO_CANCELLED = "cancelled"
SCENARIO_ERROR = "error"
SCENARIO_MULTI_AGENTS = "multi_agents"
SCENARIO_USAGE_MISSING = "usage_missing"
SCENARIO_USAGE_0 = "usage_0"
SCENARIO_USAGE_100 = "usage_100"

SCENARIO_NAMES = (
    SCENARIO_LIFECYCLE,
    SCENARIO_CANCELLED,
    SCENARIO_ERROR,
    SCENARIO_MULTI_AGENTS,
    SCENARIO_USAGE_MISSING,
    SCENARIO_USAGE_0,
    SCENARIO_USAGE_100,
)

_PRIMARY_MIN = 300
_SECONDARY_MIN = 10080


def _windows(primary_pct: float, secondary_pct: float, at_ms: int):
    return (
        {
            "id": "codex-primary",
            "label": f"{_PRIMARY_MIN} MIN WINDOW",
            "used_percent": primary_pct,
            "duration_mins": _PRIMARY_MIN,
            "resets_at_ms": ANCHOR_MS + at_ms + 3600000,
        },
        {
            "id": "codex-secondary",
            "label": f"{_SECONDARY_MIN} MIN WINDOW",
            "used_percent": secondary_pct,
            "duration_mins": _SECONDARY_MIN,
            "resets_at_ms": ANCHOR_MS + at_ms + 86400000,
        },
    )


def _lifecycle():
    """WORKING → PLAN UPDATE → NEEDS YOU → DONE（每阶段至少一份快照）。"""
    t = "thread-demo"
    return [
        ev.thread_started(t, "codex-desk-terminal", at_ms=0),
        ev.turn_started(t, "turn-001", summary="重构状态同步", at_ms=100),
        ev.token_usage(t, 154000, 258400, turn_id="turn-001", at_ms=200),
        ev.plan_updated(t, "turn-001", (
            ("检查需求", ev.PLAN_COMPLETED),
            ("实现界面", ev.PLAN_IN_PROGRESS),
            ("运行测试", ev.PLAN_PENDING),
        ), at_ms=300),
        ev.item_started(t, "turn-001", "commandExecution", item_id="item-exec-1",
                        summary="pytest -q tests", at_ms=600),
        ev.approval_requested(t, 0, "运行命令需要批准", turn_id="turn-001", at_ms=900),
        ev.rate_limits(_windows(42.0, 22.0, at_ms=1200), at_ms=1200),
        ev.server_request_resolved(t, 0, at_ms=1500),
        ev.item_completed(t, "turn-001", "commandExecution", item_id="item-exec-1", at_ms=1800),
        ev.turn_completed(t, "turn-001", ev.TURN_STATUS_COMPLETED, at_ms=2100),
    ]


def _cancelled():
    t = "thread-cancel"
    return [
        ev.thread_started(t, "batch-rename", at_ms=0),
        ev.turn_started(t, "turn-c1", summary="批量重命名文件", at_ms=100),
        ev.item_started(t, "turn-c1", "commandExecution", item_id="item-c1",
                        summary="mv old_*.png new_*.png", at_ms=200),
        ev.approval_requested(t, 0, "运行命令需要批准", turn_id="turn-c1", at_ms=300),
        ev.turn_completed(t, "turn-c1", ev.TURN_STATUS_CANCELLED, at_ms=400),
    ]


def _error():
    t = "thread-err"
    return [
        ev.thread_started(t, "db-migration", at_ms=0),
        ev.turn_started(t, "turn-e1", summary="迁移数据库", at_ms=100),
        ev.item_started(t, "turn-e1", "commandExecution", item_id="item-e1",
                        summary="make -C build", at_ms=200),
        ev.turn_completed(t, "turn-e1", ev.TURN_STATUS_FAILED,
                          summary="编译错误：类型不匹配", at_ms=300),
    ]


def _multi_agents():
    """3 线程，含排序交换：beta(working) 反超 alpha → alpha(needs_you) 再反超。"""
    a, b, g = "thread-alpha", "thread-beta", "thread-gamma"
    return [
        ev.thread_started(a, "proj-alpha", at_ms=0),
        ev.thread_started(b, "proj-beta", at_ms=10),
        ev.thread_started(g, "proj-gamma", at_ms=20),
        ev.turn_started(a, "turn-a1", summary="修复登录", at_ms=100),
        ev.turn_started(b, "turn-b1", summary="更新文档", at_ms=200),
        ev.approval_requested(a, 0, "等待确认部署目标", turn_id="turn-a1", at_ms=300),
        ev.turn_completed(b, "turn-b1", ev.TURN_STATUS_COMPLETED, at_ms=400),
    ]


def _usage_missing():
    t = "thread-usage"
    return [
        ev.thread_started(t, "proj-usage", at_ms=0),
        ev.rate_limits_unavailable(at_ms=100),
    ]


def _usage_pct(primary_pct: float, secondary_pct: float):
    t = "thread-usage"
    return [
        ev.thread_started(t, "proj-usage", at_ms=0),
        ev.rate_limits(_windows(primary_pct, secondary_pct, at_ms=100), at_ms=100),
    ]


def scenario_events(name: str) -> list:
    if name == SCENARIO_LIFECYCLE:
        return _lifecycle()
    if name == SCENARIO_CANCELLED:
        return _cancelled()
    if name == SCENARIO_ERROR:
        return _error()
    if name == SCENARIO_MULTI_AGENTS:
        return _multi_agents()
    if name == SCENARIO_USAGE_MISSING:
        return _usage_missing()
    if name == SCENARIO_USAGE_0:
        return _usage_pct(0.0, 0.0)
    if name == SCENARIO_USAGE_100:
        return _usage_pct(100.0, 100.0)
    raise ValueError(f"unknown scenario: {name!r}; expected one of {', '.join(SCENARIO_NAMES)}")


def run(name: str, *, epoch: str = DEFAULT_EPOCH, anchor_ms: int = ANCHOR_MS) -> list:
    """运行 mock 场景，返回 AppState 快照列表（每事件一份，seq 严格递增）。"""
    engine = StateEngine(epoch, source_kind="mock", utc_anchor_ms=anchor_ms)
    snapshots = []
    for event in scenario_events(name):
        snapshots.append(engine.apply(event, event.at_ms))
    return snapshots
