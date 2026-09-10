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
                    if phase.get("interrupt_result") == "interrupted":
                        # P3.2 取消路径：interrupt 后上游给出 interrupted 终态
                        # （0.152.0 真实行为，探针 6b 先例）。
                        schedule([
                            {"kind": "notification", "delay": 0.15, "raw": {
                                "jsonrpc": "2.0", "method": "turn/completed",
                                "params": {"threadId": phase["thread_id"],
                                           "turn": {"id": phase["turn_id"],
                                                    "status": "interrupted"}},
                                "emittedAtMs": 1789015000900}},
                        ])
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


def phase(thread_id, turn_id, steps, rate_limits="ok", thread_start="ok",
          interrupt_result=None):
    return {"rate_limits": rate_limits, "thread_start": thread_start,
            "thread_id": thread_id, "turn_id": turn_id, "steps": steps,
            "interrupt_result": interrupt_result}


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
    指数退避重启成功 → 重建 StateEngine（P3.2 裁决 C：新 epoch 全量替换，
    旧线程消失）→ source_reconnected（connected 恢复）。"""
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

    # 重连快照是新 epoch 的 seq=1（seq 归零），只能按位置序定位，不能按 seq 比较。
    last_disc_idx = max(i for i, s in enumerate(snaps) if not s["source"]["connected"])
    reconnected = snaps[last_disc_idx + 1]
    assert reconnected["source"]["connected"] is True
    assert reconnected["source"]["stale"] is False
    assert reconnected["bridge_epoch"] != disconnected[-1]["bridge_epoch"]

    last = snaps[-1]
    # 快照全量替换：旧线程 th-crash 随旧 engine 消失，只剩新会话线程（不复活展示）
    assert [t["id"] for t in last["threads"]] == ["th-after"]
    assert last["threads"][0]["state"] == "done"
    assert last["threads"][0]["end_reason"] == "completed"
    assert not replies.exists()
    assert len(result.report["attempts"]) == 2


def test_reconnect_rebuilds_engine_full_replacement(validator, fake_server_path, tmp_path):
    """P3.2 老化裁决 C（重连后缺线程老化）：模拟"进程退出→重启成功"序列——

    - 断连期间旧 engine 持续产 stale 快照，旧线程记录原样保留（不伪造终态）；
    - 重连成功（initialize 通过）后首份快照：新 bridge_epoch 与旧不同、seq 从
      1 重新开始、source.connected 恢复、线程集合=新会话集合（此刻为空，
      旧线程全量替换消失，后续快照只出现新会话线程）；
    - 报告 ledger 跨 epoch 合并；证据文件名带 epoch，seq 归零后不互相覆盖。"""
    crash_steps = happy_steps("th-old", "turn-old")[:3]  # thread/状态/turn 开始
    crash_steps.append({"kind": "crash", "delay": 0.25})
    adapter, replies = make_adapter(
        fake_server_path, tmp_path,
        [phase("th-old", "turn-old", crash_steps),
         phase("th-new", "turn-new", happy_steps("th-new", "turn-new"))],
        backoff=(0.0,), epoch="codex-test-epoch")
    result = adapter.run()

    assert result.exit_code == 0, result.report
    snaps = result.snapshots
    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, f"seq={snap['seq']}: {errors[0].message}"
        assert_invariants(snap)

    # 断连期间：旧 engine（旧 epoch）保持 stale，旧线程 working 原样保留
    disc = [s for s in snaps if not s["source"]["connected"]]
    assert disc, "process exit must produce stale snapshots on the old engine"
    for snap in disc:
        assert snap["source"]["stale"] is True
        assert snap["bridge_epoch"] == "codex-test-epoch"
        assert any(t["id"] == "th-old" and t["state"] == "working"
                   for t in snap["threads"])

    # 重连成功后的第一份快照：新 epoch、seq 归零重启、connected 恢复、无旧线程
    last_disc_idx = max(i for i, s in enumerate(snaps) if not s["source"]["connected"])
    new_snaps = snaps[last_disc_idx + 1:]
    first_new = new_snaps[0]
    assert first_new["bridge_epoch"] != "codex-test-epoch"
    assert first_new["bridge_epoch"] == "codex-test-epoch-r1"  # 派生自基底、可读
    assert first_new["seq"] == 1
    assert first_new["source"]["connected"] is True
    assert first_new["source"]["stale"] is False
    assert first_new["threads"] == []   # 线程集合=新会话集合：旧线程自然消失

    # 新 epoch 内：seq 连续从 1 重新开始；旧线程永不复现；线程只来自新会话
    assert [s["seq"] for s in new_snaps] == list(range(1, len(new_snaps) + 1))
    assert all(t["id"] != "th-old" for s in new_snaps for t in s["threads"])
    assert {t["id"] for t in new_snaps[-1]["threads"]} == {"th-new"}
    assert new_snaps[-1]["threads"][0]["state"] == "done"
    assert new_snaps[-1]["threads"][0]["end_reason"] == "completed"

    # 报告：最终 epoch=新 epoch；通知计数跨 epoch 合并（旧+新会话各一次
    # thread/started）；审批台账保留
    assert result.report["bridge_epoch"] == new_snaps[-1]["bridge_epoch"]
    assert result.report["notifications_seen"]["thread/started"] == 2
    assert len(result.report["attempts"]) == 2
    assert not replies.exists()

    # 证据落盘：seq 归零后文件名靠 epoch 前缀区分，不覆盖断连期 stale 快照
    out = tmp_path / "artifacts"
    paths = codex_mod.write_artifacts(str(out), result)
    names = [os.path.basename(p) for p in paths if "snapshot_" in os.path.basename(p)]
    assert len(names) == len(result.snapshots)
    assert len(set(names)) == len(names)
    assert any(n.startswith("snapshot_codex-test-epoch_") for n in names)
    assert any(n.startswith("snapshot_codex-test-epoch-r1_") for n in names)


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
# P3.2：错误/取消路径、等待解除语义、plan/updated 映射（adapter × 假 app-server）
# ---------------------------------------------------------------------------

def _base_start(thread_id, turn_id):
    """thread/started → active → turn/started 的开头三步（真实样本）。"""
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
    return s


def _turn_completed_step(thread_id, turn_id, status, error_message=None):
    turn = {"id": turn_id, "status": status}
    if error_message is not None:
        turn["error"] = {"message": error_message}
    return {"kind": "notification", "delay": 0.05,
            "raw": {"jsonrpc": "2.0", "method": "turn/completed",
                    "params": {"threadId": thread_id, "turn": turn},
                    "emittedAtMs": 1789015000900}}


def test_failed_turn_maps_to_error_not_done(validator, fake_server_path, tmp_path):
    """turn/completed(failed) → error 终态：失败绝不显示 DONE（P3.2 验收）。"""
    s = _base_start("th-fail", "turn-fail")
    s.raw(_turn_completed_step("th-fail", "turn-fail", "failed",
                               "model gpt-6-astra requires a newer Codex"))
    adapter, _ = make_adapter(
        fake_server_path, tmp_path, [phase("th-fail", "turn-fail", s.steps)])
    result = adapter.run()

    assert result.exit_code == codex_mod.EXIT_TURN_FAILED, result.report
    snaps = result.snapshots
    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, f"seq={snap['seq']}: {errors[0].message}"
        assert_invariants(snap)
    assert codex_mod.state_flow(snaps)[-1] == (snaps[-1]["seq"], "error")
    last = snaps[-1]["threads"][0]
    assert last["state"] == "error"
    assert last["end_reason"] == "failed"
    assert "gpt-6-astra" in last["activity"]      # 脱敏后的上游错误消息
    assert all(t["state"] != "done" for t in snaps[-1]["threads"])
    assert result.report["exit_reason"] == "turn_completed"


def test_interrupted_turn_maps_to_idle_cancelled(validator, fake_server_path, tmp_path):
    """interrupt 自己的受控 turn → interrupted → idle+cancelled（P3.2 取消路径）。"""
    stalled = happy_steps("th-cancel", "turn-cancel")[:3]  # thread/状态/turn 开始
    adapter, replies = make_adapter(
        fake_server_path, tmp_path,
        [phase("th-cancel", "turn-cancel", stalled, interrupt_result="interrupted")],
        backoff=(), interrupt_after_s=0.4, interrupt_grace=2.0)
    result = adapter.run()

    assert result.exit_code == codex_mod.EXIT_TURN_INTERRUPTED, result.report
    assert result.report["exit_reason"] == "turn_completed"
    # attempts 记录规范化终态（interrupted→cancelled，与退出码表一致；
    # 上游原始 status 在脱敏 IO 日志里可见）
    assert result.report["attempts"][-1]["turn_status"] == "cancelled"
    interrupted_raw = [e for e in result.raw_log
                       if e.get("dir") == "in" and e.get("payload", {}).get("method") ==
                       "turn/completed"
                       and e["payload"].get("params", {}).get("turn", {}).get("status")
                       == "interrupted"]
    assert interrupted_raw, "upstream raw status 'interrupted' must be in the IO log"
    snaps = result.snapshots
    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, f"seq={snap['seq']}: {errors[0].message}"
        assert_invariants(snap)
    last = snaps[-1]["threads"][0]
    assert last["state"] == "idle"                 # cancelled 显示 idle
    assert last["end_reason"] == "cancelled"       # end_reason 保留取消说明
    assert last["activity"] == "已取消"
    assert last["attention"] is None
    # interrupt 只落在自己的受控 thread/turn 上
    interrupts = [e for e in result.raw_log if e.get("method") == "turn/interrupt"]
    assert interrupts and interrupts[0]["payload"]["params"] == \
        {"threadId": "th-cancel", "turnId": "turn-cancel"}
    assert not replies.exists()


def test_waiting_on_user_input_flag_then_real_request_swap(validator, fake_server_path, tmp_path):
    """waitingOnUserInput 标志 → 合成 needs_you pending（负数 id）；真实
    item/tool/requestUserInput 到达后换真实项；取消才解除等待。"""
    s = _base_start("th-ui", "turn-ui")
    # 等待标志先到、无请求载荷（adapter 重启错过请求的形状）→ 合成 pending
    s.add("thread/status/changed", {
        "threadId": "th-ui",
        "status": {"type": "active", "activeFlags": ["waitingOnUserInput"]}})
    # 真实 requestUserInput（v1 schema ToolRequestUserInputParams 形状）
    s.raw({"kind": "server_request", "delay": 0.1, "raw": {
        "jsonrpc": "2.0", "id": 11, "method": "item/tool/requestUserInput",
        "params": {"threadId": "th-ui", "turnId": "turn-ui", "isBlocking": True,
                   "itemId": "tool-1",
                   "questions": [{"header": "选择", "id": "q1",
                                  "question": "选择 A 还是 B？",
                                  "options": [{"label": "A", "description": "选项 A"},
                                              {"label": "B", "description": "选项 B"}]}]}}})
    adapter, replies = make_adapter(
        fake_server_path, tmp_path,
        [phase("th-ui", "turn-ui", s.steps, interrupt_result="interrupted")],
        backoff=(), interrupt_after_s=0.8, interrupt_grace=2.0)
    result = adapter.run()

    assert result.exit_code == codex_mod.EXIT_TURN_INTERRUPTED, result.report
    snaps = result.snapshots
    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, f"seq={snap['seq']}: {errors[0].message}"
        assert_invariants(snap)

    # 1) 标志合成：needs_you + 合成摘要（无请求载荷，只读等待）
    synthetic = next(s for s in snaps if s["threads"]
                     and s["threads"][0]["state"] == "needs_you")
    att = synthetic["threads"][0]["attention"]
    assert att["pending_count"] == 1
    assert "等待用户输入" in att["summary"]

    # 2) 真实请求换入：摘要用 questions[0].question；等待期间其他事件不解除
    real = max((s for s in snaps if s["threads"]
                and s["threads"][0]["state"] == "needs_you"),
               key=lambda s: s["seq"])
    assert "选择 A 还是 B" in real["threads"][0]["attention"]["summary"]
    assert real["threads"][0]["waiting_ms"] >= 0

    # 3) 等待只在明确取消后消除
    last = snaps[-1]["threads"][0]
    assert last["state"] == "idle" and last["end_reason"] == "cancelled"
    assert last["attention"] is None
    # 红线：requestUserInput 请求从未被回答
    assert not replies.exists()
    seen = result.report["server_requests_seen"]
    assert {"method": "item/tool/requestUserInput", "id": 11} in seen


def test_plan_updated_maps_to_plan_steps_integration(validator, fake_server_path, tmp_path):
    """turn/plan/updated（含 inProgress）→ AppState plan.steps，状态映射按 §3；
    PLAN UPDATE 保持 WORKING。"""
    s = _base_start("th-plan", "turn-plan")
    s.add("turn/plan/updated", {
        "threadId": "th-plan", "turnId": "turn-plan", "explanation": "三步计划",
        "plan": [{"step": "列出目录", "status": "pending"},
                 {"step": "统计文件数", "status": "pending"},
                 {"step": "总结", "status": "pending"}]})
    s.add("item/started", {
        "item": {"type": "commandExecution", "id": "exec-1", "command": "ls"},
        "threadId": "th-plan", "turnId": "turn-plan",
        "startedAtMs": 1789015000600})
    s.add("turn/plan/updated", {
        "threadId": "th-plan", "turnId": "turn-plan", "explanation": None,
        "plan": [{"step": "列出目录", "status": "completed"},
                 {"step": "统计文件数", "status": "inProgress"},
                 {"step": "总结", "status": "pending"}]})
    s.raw(_turn_completed_step("th-plan", "turn-plan", "completed"))
    adapter, _ = make_adapter(
        fake_server_path, tmp_path, [phase("th-plan", "turn-plan", s.steps)])
    result = adapter.run()

    assert result.exit_code == 0, result.report
    snaps = result.snapshots
    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, f"seq={snap['seq']}: {errors[0].message}"
        assert_invariants(snap)
    # 第二次 plan 更新：inProgress → in_progress（adapter 归一化）
    # （首个快照是 source_reconnected，threads 为空，需跳过；plan 在本 turn
    #   的后续快照中一直保留，因此 plans 含终态前后的全部快照）
    plans = [s for s in snaps
             if s["threads"] and s["threads"][0]["plan"]["total"] > 0]
    # 第一次更新：全部 pending
    assert [st["status"] for st in plans[0]["threads"][0]["plan"]["steps"]] == \
        ["pending", "pending", "pending"]
    # 最终 plan：inProgress → in_progress（adapter 归一化，INTERFACES §3）
    assert [st["status"] for st in plans[-1]["threads"][0]["plan"]["steps"]] == \
        ["completed", "in_progress", "pending"]
    assert [st["text"] for st in plans[-1]["threads"][0]["plan"]["steps"]] == \
        ["列出目录", "统计文件数", "总结"]
    assert plans[-1]["threads"][0]["plan"]["total"] == 3
    assert plans[-1]["threads"][0]["plan"]["truncated"] is False
    # PLAN UPDATE 是内容事件：更新期间保持 working（终态才变 done；
    # 最后一个 plan 快照即 turn/completed，plan 内容随终态保留）
    for snap in plans[:-1]:
        assert snap["threads"][0]["state"] == "working"
    last = snaps[-1]["threads"][0]
    assert last["state"] == "done" and last["end_reason"] == "completed"
    # 终态后 plan 保留（新 turn 才清旧计划；本 turn 未清）
    assert last["plan"]["total"] == 3


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


def test_mapper_request_user_input_summary_uses_question_text():
    """requestUserInput 摘要取 questions[0].question（P3.2：该载荷没有 command 键）。"""
    m = codex_mod.CodexEventMapper()
    events = m.server_request({
        "method": "item/tool/requestUserInput", "id": 11,
        "params": {"threadId": "t", "turnId": "u", "isBlocking": True,
                   "itemId": "tool-1",
                   "questions": [{"header": "选择", "id": "q1",
                                  "question": "选择 A 还是 B？", "options": None}]}})
    assert [e.type for e in events] == ["approval_requested"]
    assert events[0].summary == "选择 A 还是 B？"
    # questions 缺失/为空 → 回退方法名兜底，不编造问题文本
    events2 = m.server_request({
        "method": "item/tool/requestUserInput", "id": 12,
        "params": {"threadId": "t", "turnId": "u", "isBlocking": True,
                   "itemId": "tool-2", "questions": []}})
    assert events2[-1].summary == "审批请求：item/tool/requestUserInput"


def test_mapper_thread_system_error_and_not_loaded():
    m = codex_mod.CodexEventMapper()
    events = m.notification(
        "thread/status/changed",
        {"threadId": "t", "status": {"type": "systemError"}}, at_ms=1)
    assert [e.type for e in events] == ["thread_status_changed"]
    assert events[0].status == "systemError"
    # notLoaded（跨实例线程的常态）不产生事件
    assert m.notification(
        "thread/status/changed",
        {"threadId": "t", "status": {"type": "notLoaded"}}, at_ms=2) == []


def test_mapper_reducer_composition_late_events_after_cancel():
    """mapper+reducer 组合：interrupted 终态后迟到的 item/审批请求不能复活等待
    （P3.2 钉死项的 mapper 层佐证；reducer 门闸单测见 test_reducer_rules）。"""
    from bridge.state.engine import StateEngine

    m = codex_mod.CodexEventMapper()
    engine = StateEngine("epoch-compose")
    seq = []
    seq += m.notification("thread/started",
                          {"thread": {"id": "t", "cwd": "/tmp/x"}}, at_ms=0)
    seq += m.notification("thread/status/changed",
                          {"threadId": "t",
                           "status": {"type": "active", "activeFlags": []}}, at_ms=10)
    seq += m.notification("turn/started",
                          {"threadId": "t", "turn": {"id": "u"}}, at_ms=20)
    seq += m.notification("turn/completed",
                          {"threadId": "t",
                           "turn": {"id": "u", "status": "interrupted"}}, at_ms=30)
    # 终态后迟到：缓冲/重排导致的事件（真实抓包中存在）
    seq += m.notification(
        "item/started",
        {"threadId": "t", "turnId": "u",
         "item": {"type": "commandExecution", "id": "i1", "command": "ls"}}, at_ms=40)
    seq += m.server_request(
        {"method": "item/commandExecution/requestApproval", "id": 5,
         "params": {"threadId": "t", "turnId": "u", "command": "ls"}})

    snap = {}
    for e in seq:
        snap = engine.apply(e, e.at_ms if e.at_ms is not None else 0)
    rec = snap["threads"][0]
    assert rec["state"] == "idle"
    assert rec["end_reason"] == "cancelled"
    assert rec["attention"] is None  # 迟到审批不复活等待


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
    assert "snapshot_codex-test-001_0001.json" in names  # 文件名含 bridge_epoch
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
