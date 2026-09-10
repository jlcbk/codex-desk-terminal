"""渲染：内部状态 → 符合 protocol/state.schema.json 的 AppState dict（P1.1）。

实现 INTERFACES §3 的排序与裁剪顺序（冻结文本）：

- 排序：needs_you > error > working/thinking > done > idle；
  同级 updated_at 降序，再 id 升序。默认选首项；本地选中不被普通更新抢走。
- 裁剪：threads≤8、plan steps≤8、usage windows≤4；各字符串按冻结字节上限截断
  （UTF-8 完整码点）；维护 threads_total/truncated 等计数；选中线程与 NEEDS YOU
  优先保留；编码后仍超 16384 字节则逐级裁掉非选中低优先级线程/内容。
- 全部输出字段必填；可为 null 的字段严格按 §3 末段允许清单。

渲染是纯函数：相同内部状态 + 相同入参 ⇒ 字节相同的 dict（按插入序编码）。
"""

from __future__ import annotations

from typing import Optional

from . import reducer as rd

# ---- 冻结上限（docs/INTERFACES.md §3；与 protocol/state.schema.json 注解一致）----
MAX_MESSAGE_BYTES = 16384
MAX_THREADS = 8
MAX_PLAN_STEPS = 8
MAX_WINDOWS = 4
MAX_COUNT = 65535            # 计数/总数与设备 uint16 对齐
MAX_ID_BYTES = 128           # thread id / turn_id / usage window id
MAX_PROJECT_BYTES = 96
MAX_ACTIVITY_BYTES = 192     # activity 与 attention.summary 同限
MAX_PLAN_TEXT_BYTES = 128
MAX_LABEL_BYTES = 48
MAX_EPOCH_BYTES = 64
MAX_SEQ = 9007199254740991   # 2^53-1

# 状态优先级：数值小者优先（needs_you > error > working/thinking > done > idle）。
_RANK = {
    rd.ST_NEEDS_YOU: 0,
    rd.ST_ERROR: 1,
    rd.ST_WORKING: 2,
    rd.ST_THINKING: 2,
    rd.ST_DONE: 3,
    rd.ST_IDLE: 4,
}


def clamp_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 完整码点截断到 max_bytes；不产生半个码点。"""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def clamp_percent(value) -> Optional[float]:
    """百分比归一到 0–100 或 None（schema：number|null）。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    if v < 0.0:
        v = 0.0
    if v > 100.0:
        v = 100.0
    return round(v, 1)


def clamp_count(value: int) -> int:
    v = int(value)
    if v < 0:
        return 0
    return v if v <= MAX_COUNT else MAX_COUNT


def _clamp_id(text: str) -> str:
    return clamp_utf8(text, MAX_ID_BYTES)


def _plan_of(rec: rd.ThreadRecord) -> dict:
    steps_src = rec.plan_steps[:MAX_PLAN_STEPS]
    steps = [
        {"text": clamp_utf8(str(text), MAX_PLAN_TEXT_BYTES), "status": status}
        for (text, status) in steps_src
    ]
    total = clamp_count(rec.plan_total if rec.plan_total is not None else len(rec.plan_steps))
    return {
        "total": total,
        "truncated": len(rec.plan_steps) > MAX_PLAN_STEPS,
        "steps": steps,
    }


def _context_of(rec: rd.ThreadRecord) -> dict:
    used = rec.used_tokens
    cap = rec.capacity_tokens
    if used is not None and used < 0:
        used = None
    if cap is not None and cap <= 0:
        cap = None  # capacity 缺失/非正视为未知，避免除零与编造
    percent = None
    if used is not None and cap:
        percent = clamp_percent(used * 100.0 / cap)
    return {"used_tokens": used, "capacity_tokens": cap, "used_percent": percent}


def _thread_dict(rec: rd.ThreadRecord, now_mono: int, anchor_ms: int) -> dict:
    if rec.turn_started_mono is not None:
        end = rec.finished_mono if rec.turn_finished else now_mono
        elapsed = max(0, int(end) - int(rec.turn_started_mono))
    else:
        elapsed = 0
    if rec.pending and rec.waiting_since_mono is not None and not rec.turn_finished:
        waiting = max(0, int(now_mono) - int(rec.waiting_since_mono))
    else:
        waiting = 0
    if rec.pending:
        count = clamp_count(len(rec.pending))
        first_summary = next(iter(rec.pending.values()))
        attention = {
            "pending_count": count,
            "summary": clamp_utf8(first_summary, MAX_ACTIVITY_BYTES),
        }
    else:
        attention = None
    turn_id = _clamp_id(rec.turn_id) if rec.turn_id is not None else None
    return {
        "id": _clamp_id(rec.id),
        "turn_id": turn_id,
        "project": clamp_utf8(rec.project, MAX_PROJECT_BYTES),
        "state": rec.state,
        "activity": clamp_utf8(rec.activity, MAX_ACTIVITY_BYTES),
        "updated_at_ms": anchor_ms + rec.updated_mono,
        "elapsed_ms": elapsed,
        "waiting_ms": waiting,
        "end_reason": rec.end_reason,
        "attention": attention,
        "plan": _plan_of(rec),
        "context": _context_of(rec),
    }


def _sort_key(item: dict):
    updated = item["updated_at_ms"] if item["updated_at_ms"] is not None else 0
    return (_RANK.get(item["state"], 9), -updated, item["id"])


def _usage_dict(usage: dict, anchor_ms: int) -> dict:
    windows_src = usage.get("windows", [])
    windows = []
    for w in windows_src[:MAX_WINDOWS]:
        duration = w.get("duration_mins")
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 1
        if duration < 1:
            duration = 1
        resets = w.get("resets_at_ms")
        windows.append({
            "id": _clamp_id(str(w.get("id", ""))),
            "label": clamp_utf8(str(w.get("label", "")), MAX_LABEL_BYTES),
            "used_percent": clamp_percent(w.get("used_percent")),
            "duration_mins": duration,
            "resets_at_ms": resets if isinstance(resets, int) or resets is None else None,
        })
    total = clamp_count(len(windows_src))
    return {
        "available": bool(usage.get("available", False)),
        "updated_at_ms": (anchor_ms + usage["updated_mono"])
        if usage.get("updated_mono") is not None else None,
        "windows_total": total,
        "windows_truncated": len(windows_src) > MAX_WINDOWS,
        "windows": windows,
    }


def _encode(state_dict: dict) -> bytes:
    import json

    return json.dumps(state_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def render(internal: dict, *, bridge_epoch: str, seq: int, source_kind: str,
           now_mono: int, anchor_ms: int) -> dict:
    """内部状态 → AppState dict（已排序、已裁剪、可 schema 校验）。"""
    anchor_ms = int(anchor_ms)
    threads_all = [
        _thread_dict(rec, now_mono, anchor_ms) for rec in internal["threads"].values()
    ]
    threads_all.sort(key=_sort_key)

    user_selected = internal.get("user_selected")
    kept = threads_all[:MAX_THREADS]
    total = clamp_count(len(threads_all))
    threads_truncated = total > len(kept)

    # 选中线程优先保留（§3 裁剪顺序：先按优先级排序，保留选中线程）。
    if user_selected is not None:
        sel = next((t for t in threads_all if t["id"] == user_selected), None)
        if sel is not None and sel not in kept:
            kept[-1] = sel  # 占一个名额，挤掉末位（最低优先级）
            kept.sort(key=_sort_key)

    # 默认选首项；本地选中不被普通更新抢走。
    if user_selected is not None and any(t["id"] == user_selected for t in kept):
        selected_id = user_selected
    elif kept:
        selected_id = kept[0]["id"]
    else:
        selected_id = None

    def snapshot(kept_threads, truncated_flag):
        last = internal.get("last_event_mono")
        return {
            "schema_version": 1,
            "kind": "state",
            "bridge_epoch": clamp_utf8(bridge_epoch, MAX_EPOCH_BYTES),
            "seq": seq,
            "generated_at_ms": anchor_ms + now_mono,
            "source": {
                "kind": source_kind,
                "connected": bool(internal.get("source_connected", True)),
                "stale": bool(internal.get("source_stale", False)),
                "last_event_at_ms": (anchor_ms + last) if last is not None else None,
            },
            "selected_thread_id": selected_id,
            "threads_total": total,
            "threads_truncated": truncated_flag,
            "threads": kept_threads,
            "usage": _usage_dict(internal["usage"], anchor_ms),
        }

    snap = snapshot(kept, threads_truncated)
    if len(_encode(snap)) <= MAX_MESSAGE_BYTES:
        return snap

    # ---- 仍超 16KiB：逐级裁剪（§3 裁剪顺序末段）----
    # 1) 逐个丢弃最低优先级的非选中线程（total 不变，truncated=True）。
    def drop_lowest_non_selected() -> bool:
        for i in range(len(kept) - 1, -1, -1):
            if kept[i]["id"] != selected_id:
                del kept[i]
                return True
        return False

    while len(_encode(snapshot(kept, True))) > MAX_MESSAGE_BYTES:
        if not drop_lowest_non_selected():
            break
    snap = snapshot(kept, threads_truncated or total > len(kept))

    # 2) 清空非选中线程的 plan steps（total 保留，truncated=True）。
    if len(_encode(snap)) > MAX_MESSAGE_BYTES:
        for t in snap["threads"]:
            if t["id"] != selected_id and t["plan"]["steps"]:
                t["plan"]["steps"] = []
                t["plan"]["truncated"] = t["plan"]["total"] > 0
        snap = snapshot(snap["threads"], True)

    # 3) 清空非选中线程的 activity/attention.summary 文本。
    if len(_encode(snap)) > MAX_MESSAGE_BYTES:
        for t in snap["threads"]:
            if t["id"] != selected_id:
                t["activity"] = ""
                if t["attention"] is not None:
                    t["attention"]["summary"] = ""
        snap = snapshot(snap["threads"], True)

    # 4) 最后只剩选中线程仍超限（极端）：收缩选中线程内容。
    if len(_encode(snap)) > MAX_MESSAGE_BYTES and snap["threads"]:
        main = snap["threads"][0]
        main["plan"]["steps"] = []
        main["activity"] = ""
        if main["attention"] is not None:
            main["attention"]["summary"] = ""
        snap = snapshot(snap["threads"], True)

    encoded = _encode(snap)
    if len(encoded) > MAX_MESSAGE_BYTES:  # 理论不可达（单线程钳制后 <2KiB）
        raise ValueError("snapshot exceeds 16384 bytes even after full clamping")
    return snap
