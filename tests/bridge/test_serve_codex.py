"""R2 离线单测：bridge_serve_codex 持续服务（真实 codex source 的实时链路）。

策略与 test_codex_adapter 一致：离线为主，用假 app-server（stdio 按行
JSON-RPC，帧格式同 codex 0.152.0）驱动完整 serve 路径，不依赖网络/真实 codex。
真实 codex 的端到端验收在 artifacts/serve_codex 下的运行记录（脚本化复现）。

覆盖：
- ServeHub：wire epoch/seq 盖章、新会话新 epoch、keepalive 新 seq、并发锁。
- SubmissionQueue：空/超长/队满拒绝；job_id 串行分配。
- FileTaskSource：*.prompt 原子认领、归档 + result.json、stale .claiming 不重跑。
- run_job（ServeRuntime 全链）：快照流 idle→working→done、usage 窗口非空、
  source.kind=codex_bridge_owned、seq 单调、证据落盘、脱敏自检、
  sink 模式下 adapter 不累积快照（长驻有界）。
- 进程暴毙：单次会话不自动重跑 prompt（R2：恢复只重同步），如实
  source_disconnected、不伪造终态。
- unix socket 协议：submit 入队应答、status 快照（真 asyncio socket 往返）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import textwrap
import threading
import time
from pathlib import Path

import pytest

from bridge import serve_codex as svc
from conftest import REPO_ROOT, assert_invariants

SCRIPT_PATH = REPO_ROOT / "scripts" / "bridge_serve_codex.py"


def load_script():
    """按路径加载 scripts/bridge_serve_codex.py（WSS 导入已延迟，无需 websockets）。"""
    spec = importlib.util.spec_from_file_location("bridge_serve_codex_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 假 app-server：行为由 conf.json 的 phases 驱动（argv: conf counter）。
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
            "primary": {"usedPercent": 31, "windowDurationMins": 300,
                        "resetsAt": 1789030193},
            "secondary": {"usedPercent": 12, "windowDurationMins": 10080,
                          "resetsAt": 1789446614},
        },
    }

    def main():
        conf_path, counter_path = sys.argv[1:3]
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
        thread_id, turn_id = phase["thread_id"], phase["turn_id"]

        def schedule(steps):
            def run():
                for step in steps:
                    time.sleep(step.get("delay", 0.05))
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
            if "method" not in msg:
                continue
            meth, mid = msg.get("method"), msg.get("id")
            if meth == "initialize":
                respond(mid, {"userAgent": "codex-fake/0.152.0 (test; fake)"})
            elif meth == "account/rateLimits/read":
                respond(mid, RATE_LIMITS_READ)
            elif meth == "thread/start":
                respond(mid, {"thread": {"id": thread_id, "cwd": os.getcwd(),
                                         "ephemeral": True},
                              "model": "gpt-5.6-sol"})
                schedule(phase["steps"])
            elif meth == "turn/start":
                respond(mid, {"turn": {"id": turn_id, "status": "inProgress"}})
            elif meth == "turn/interrupt":
                respond(mid, {})
            else:
                respond(mid, None, {"code": -32601, "message": "Method not found"})

    main()
    """
)


def notification(method, params, delay):
    return {"delay": delay, "raw": {"jsonrpc": "2.0", "method": method,
                                    "params": params}}


def happy_steps(thread_id, turn_id):
    """一次受控 turn 的真实事件序列（形态对齐 docs/proto-samples）。"""
    return [
        notification("thread/started",
                     {"thread": {"id": thread_id, "cwd": "/tmp/fake-proj"}}, 0.02),
        notification("thread/status/changed",
                     {"threadId": thread_id,
                      "status": {"type": "active", "activeFlags": []}}, 0.04),
        notification("turn/started",
                     {"threadId": thread_id, "turn": {"id": turn_id}}, 0.06),
        notification("item/started",
                     {"threadId": thread_id, "turnId": turn_id,
                      "item": {"type": "agentMessage", "id": "it-1"}}, 0.08),
        notification("turn/completed",
                     {"threadId": thread_id,
                      "turn": {"id": turn_id, "status": "completed"}}, 0.10),
    ]


def crash_steps(thread_id, turn_id):
    """turn 已启动后进程暴毙（断连不重跑的验证场景）。"""
    return happy_steps(thread_id, turn_id)[:3] + [{"delay": 0.10, "kind": "crash"}]


@pytest.fixture(scope="module")
def fake_server_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("fake") / "fake_appserver.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return path


class Env:
    """一个 serve 运行环境：hub + 队列 + 目录投递 + 运行态（脚本 ServeRuntime）。"""

    def __init__(self, mod, tmp_path, fake_server_path, phases):
        self.mod = mod
        base = "codex-serve-test-%d" % int(time.time() * 1000)
        self.hub = svc.ServeHub(base)
        self.jobs = svc.SubmissionQueue(maxsize=8)
        self.tasks_dir = tmp_path / "tasks"
        self.file_source = svc.FileTaskSource(str(self.tasks_dir), self.jobs)
        self.evidence_dir = tmp_path / "evidence"
        self.conf_path = tmp_path / "conf.json"
        self.counter_path = tmp_path / "counter"
        self.conf_path.write_text(json.dumps({"phases": phases}), encoding="utf-8")
        self.args = argparse.Namespace(
            evidence_dir=str(self.evidence_dir), model=None,
            codex_bin="codex", turn_timeout=15.0,
            server_argv=["python3", str(fake_server_path),
                         str(self.conf_path), str(self.counter_path)],
        )
        self.runtime = mod.ServeRuntime(self.args, self.hub, self.jobs,
                                        self.file_source)
        self.published = []  # (bytes, snapshot dict)

        def collector(data: bytes) -> None:
            self.published.append((data, json.loads(data)))

        self.runtime._enqueue_publish = collector
        self.runtime.loop = None  # 测试不走事件循环发布

    def submit(self, prompt="只回复 ok", origin="socket"):
        ok, job_id, _ = self.jobs.submit(prompt, origin=origin)
        assert ok, job_id
        return job_id

    def run_next(self):
        job = self.jobs.get(timeout=1.0)
        assert job is not None
        self.runtime.run_job(job)
        self.jobs.task_done()

    def snapshots(self):
        return [snap for _, snap in self.published]

    def result(self, job_id):
        for path in sorted(self.evidence_dir.glob("*/result.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["job_id"] == job_id:
                return path.parent, data
        raise AssertionError("result.json not found for %s" % job_id)


# ---------------------------------------------------------------------------
# ServeHub
# ---------------------------------------------------------------------------

def test_hub_epoch_seq_and_keepalive():
    hub = svc.ServeHub("codex-serve-1")
    first = hub.publish({"seq": 99, "bridge_epoch": "internal", "threads": []})
    snap = json.loads(first)
    assert snap["bridge_epoch"] == "codex-serve-1"
    assert snap["seq"] == 1

    epoch = hub.session_epoch("t1")
    assert epoch == "codex-serve-1-t1"
    second = json.loads(hub.publish({"seq": 5, "bridge_epoch": "internal"}))
    assert second["bridge_epoch"] == "codex-serve-1-t1"
    assert second["seq"] == 1  # 新会话 seq 归 1（新 epoch 全量替换）

    keep = json.loads(hub.keepalive())
    assert keep["seq"] == 2  # 存活快照也占 seq（§4）
    keep2 = json.loads(hub.keepalive())
    assert keep2["seq"] == 3
    assert hub.seq == 3
    assert hub.keepalive() is not None
    # 空内容 hub 不编造快照
    empty_hub = svc.ServeHub("e")
    assert empty_hub.keepalive() is None
    assert b"codex-serve-1" in first


def test_hub_base_epoch_clamped_to_64_bytes():
    hub = svc.ServeHub("b" * 200)
    assert len(hub.base_epoch.encode("utf-8")) <= 64 - svc.SESSION_EPOCH_SUFFIX_MAX
    epoch = hub.session_epoch("t1")
    assert len(epoch.encode("utf-8")) <= 64
    assert epoch != hub.base_epoch  # 与 base 必然不同


def test_initial_snapshot_idle_empty_threads():
    snap = svc.initial_snapshot("codex-serve-9", anchor_ms=1000, now_mono=5)
    assert snap["threads"] == []
    assert snap["threads_total"] == 0
    assert snap["source"]["kind"] == "codex_bridge_owned"
    assert snap["source"]["connected"] is True
    assert snap["usage"]["available"] is False
    assert_invariants(snap)


# ---------------------------------------------------------------------------
# SubmissionQueue / FileTaskSource
# ---------------------------------------------------------------------------

def test_submission_queue_rules():
    jobs = svc.SubmissionQueue(maxsize=2)
    ok, jid, depth = jobs.submit("a", origin="socket")
    assert ok and jid == "t1" and depth == 1
    ok, err, _ = jobs.submit("   ", origin="socket")
    assert not ok and "non-empty" in err
    ok, err, _ = jobs.submit("x" * (svc.MAX_PROMPT_BYTES + 1), origin="socket")
    assert not ok and "exceeds" in err
    jobs.submit("b", origin="socket")
    ok, err, depth = jobs.submit("c", origin="socket")
    assert not ok and "queue full" in err and depth == 2
    job = jobs.get(timeout=0.2)
    assert job.job_id == "t1" and job.prompt == "a"
    jobs.task_done()
    jobs.task_done()


def test_file_task_source_claim_archive_stale(tmp_path):
    jobs = svc.SubmissionQueue(maxsize=4)
    src = svc.FileTaskSource(str(tmp_path / "tasks"), jobs)
    src.prepare()  # 建目录
    (tmp_path / "tasks" / "leftover.prompt.claiming").write_text("旧进程残留",
                                                                 encoding="utf-8")
    src.prepare()  # 启动清残留：归档为 interrupted，不重跑
    archive = tmp_path / "tasks" / "archive"
    leftovers = list(archive.glob("*-interrupted-leftover.prompt.claiming"))
    assert len(leftovers) == 1
    assert json.loads(
        (leftovers[0].with_suffix(leftovers[0].suffix + ".result.json"))
        .read_text(encoding="utf-8"))["status"] == "interrupted"

    # 正常投递 → 认领（rename .claiming）→ 入队
    (tmp_path / "tasks" / "r2e2e.prompt").write_text("只回复 ok\n", encoding="utf-8")
    claimed = src.poll_once()
    assert claimed == ["t1"]
    assert not (tmp_path / "tasks" / "r2e2e.prompt").exists()
    job = jobs.get(timeout=0.2)
    jobs.task_done()
    assert job.origin == "file:r2e2e.prompt"
    assert job.claimed_path.endswith("r2e2e.prompt.claiming")

    # 任务终态 → 归档 + result.json（prompt 内容不进 result）
    summary = {"job_id": "t1", "exit_code": 0, "turn_status": "completed",
               "prompt": "<content len=9>"}
    dest = src.archive_result(job, summary)
    assert dest is not None and dest.name.endswith("t1-r2e2e.prompt.claiming")
    assert json.loads(dest.with_suffix(dest.suffix + ".result.json")
                      .read_text(encoding="utf-8"))["exit_code"] == 0

    # 空 prompt 文件 → 拒绝归档，不入队
    (tmp_path / "tasks" / "empty.prompt").write_text("   \n", encoding="utf-8")
    assert src.poll_once() == []
    rejected = list(archive.glob("*-rejected-empty.prompt.claiming"))
    assert len(rejected) == 1


# ---------------------------------------------------------------------------
# run_job 全链（假 app-server）
# ---------------------------------------------------------------------------

def _flow(snapshots):
    return [(s["seq"], s["bridge_epoch"],
             s["threads"][0]["state"] if s["threads"] else None)
            for s in snapshots]


def test_run_job_real_event_sequence(tmp_path, fake_server_path):
    mod = load_script()
    env = Env(mod, tmp_path, fake_server_path,
              [{"thread_id": "th-r2-1", "turn_id": "turn-1",
                "steps": happy_steps("th-r2-1", "turn-1")}])
    job_id = env.submit("只回复 ok")
    env.run_next()

    snaps = env.snapshots()
    assert snaps, "serve 必须即时产出快照流"
    # wire 盖章：同 epoch、seq 从 1 严格递增
    epochs = {s["bridge_epoch"] for s in snaps}
    assert epochs == {env.hub.base_epoch + "-t1"}
    assert [s["seq"] for s in snaps] == list(range(1, len(snaps) + 1))
    # 协议不变量 + 真源标记
    for snap in snaps:
        assert_invariants(snap)
        assert snap["source"]["kind"] == "codex_bridge_owned"
    # 首份快照 = connected 全量（threads 空），终态 done
    assert snaps[0]["threads"] == []
    assert snaps[0]["source"]["connected"] is True
    flow = [state for _, _, state in _flow(snaps)]
    assert "working" in flow, flow
    assert flow[-1] == "done", flow
    # 额度字段非空（启动读 + turn 后补读均来自假 app-server 实测窗口）
    assert snaps[-1]["usage"]["available"] is True
    assert len(snaps[-1]["usage"]["windows"]) == 2
    assert snaps[-1]["usage"]["windows"][0]["used_percent"] == 31.0

    # 证据落盘 + 脱敏自检 + 有界（sink 模式 adapter 不累积快照）
    evidence, result = env.result(job_id)
    assert result["exit_code"] == 0 and result["turn_status"] == "completed"
    assert result["redaction_selfcheck"] == "PASS"
    assert result["snapshot_count"] == len(snaps)
    assert result["usage_windows"]
    lines = (evidence / "snapshots.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(snaps)
    report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
    assert report["capabilities"]["rate_limits_read"] is True
    assert "rate_limits_after_turn" in report["capabilities"]
    assert (evidence / "events_raw_redacted.jsonl").exists()
    raw_text = (evidence / "events_raw_redacted.jsonl").read_text(encoding="utf-8")
    assert "只回复 ok" not in raw_text  # 用户内容不入脱敏日志


def test_process_exit_no_auto_rerun(tmp_path, fake_server_path):
    mod = load_script()
    env = Env(mod, tmp_path, fake_server_path,
              [{"thread_id": "th-r2-2", "turn_id": "turn-2",
                "steps": crash_steps("th-r2-2", "turn-2")}])
    job_id = env.submit("崩溃场景")
    env.run_next()

    assert env.counter_path.read_text().strip() == "1"  # 单次会话，绝不重跑 prompt
    snaps = env.snapshots()
    last = snaps[-1]
    # 如实断连：source stale/connected 翻转；不伪造终态（线程保持 working）
    assert last["source"]["connected"] is False
    assert last["source"]["stale"] is True
    thread = last["threads"][0]
    assert thread["state"] == "working" and thread["end_reason"] is None
    _, result = env.result(job_id)
    assert result["exit_code"] == 7
    assert result["turn_status"] is None


def test_two_jobs_serial_new_epochs(tmp_path, fake_server_path):
    mod = load_script()
    env = Env(mod, tmp_path, fake_server_path,
              [{"thread_id": "th-r2-3", "turn_id": "turn-3",
                "steps": happy_steps("th-r2-3", "turn-3")},
               {"thread_id": "th-r2-4", "turn_id": "turn-4",
                "steps": happy_steps("th-r2-4", "turn-4")}])
    job1 = env.submit("第一个")
    env.run_next()
    job2 = env.submit("第二个")
    env.run_next()

    assert job1 == "t1" and job2 == "t2"
    snaps = env.snapshots()
    e1 = [s for s in snaps if s["bridge_epoch"].endswith("-t1")]
    e2 = [s for s in snaps if s["bridge_epoch"].endswith("-t2")]
    assert e1 and e2
    assert e1[0]["seq"] == 1 and e2[0]["seq"] == 1  # 新会话新 epoch、seq 归 1
    assert [s["seq"] for s in e2] == list(range(1, len(e2) + 1))
    assert e2[-1]["threads"][0]["state"] == "done"
    assert e2[-1]["threads"][0]["id"] == "th-r2-4"  # 新 epoch 全量替换旧线程
    assert env.hub.epoch.endswith("-t2")


def test_file_submission_end_to_end(tmp_path, fake_server_path):
    mod = load_script()
    env = Env(mod, tmp_path, fake_server_path,
              [{"thread_id": "th-r2-5", "turn_id": "turn-5",
                "steps": happy_steps("th-r2-5", "turn-5")}])
    env.file_source.prepare()
    (env.tasks_dir / "drop.prompt").write_text("目录投递任务", encoding="utf-8")
    assert env.file_source.poll_once() == ["t1"]
    env.run_next()
    _, result = env.result("t1")
    assert result["origin"] == "file:drop.prompt"
    assert result["exit_code"] == 0
    archived = list((env.tasks_dir / "archive").glob("*-t1-drop.prompt.claiming"))
    assert len(archived) == 1
    assert json.loads(archived[0].with_suffix(archived[0].suffix + ".result.json")
                      .read_text(encoding="utf-8"))["turn_status"] == "completed"
    # 用户内容只在用户自己的文件里，不进证据目录
    evidence_text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in env.result("t1")[0].iterdir() if p.is_file())
    assert "目录投递任务" not in evidence_text


# ---------------------------------------------------------------------------
# unix socket 协议（真 asyncio socket 往返）
# ---------------------------------------------------------------------------

class _State:
    running_job = None
    jobs_done = 0
    dropped_frames = 0


def test_socket_submit_and_status(tmp_path):
    # macOS/pytest 的 tmp_path 超过 AF_UNIX 104 字节路径上限 → 用 /tmp 短路径
    sock_path = Path("/tmp/cdt-r2-%d.sock" % (time.time_ns() % 1000000))

    async def scenario():
        hub = svc.ServeHub("codex-serve-sock")
        jobs = svc.SubmissionQueue(maxsize=4)
        server = await asyncio.start_unix_server(
            lambda r, w: svc.handle_socket_client(r, w, jobs=jobs, hub=hub,
                                                  state=_State()),
            path=str(sock_path))
        try:
            loop = asyncio.get_running_loop()
            ok, resp = await loop.run_in_executor(
                None, lambda: svc.submit_via_socket(str(sock_path), "hi"))
            assert ok and resp == {"ok": True, "job_id": "t1", "queued_depth": 1}

            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            writer.write(svc.encode_request("status"))
            await writer.drain()
            status = json.loads(await reader.readline())
            assert status["ok"] is True
            assert status["epoch"] == "codex-serve-sock"
            assert status["queued"] == 1
            assert status["jobs_done"] == 0
            writer.close()

            # 非法请求 → ok=false 应答（连接保持可用）
            reader2, writer2 = await asyncio.open_unix_connection(str(sock_path))
            writer2.write(b"not json\n")
            await writer2.drain()
            bad = json.loads(await reader2.readline())
            assert bad["ok"] is False and "error" in bad
            writer2.close()

            # 服务不可达 → (False, error)
            ok2, resp2 = await loop.run_in_executor(
                None, lambda: svc.submit_via_socket(str(tmp_path / "none.sock"), "x"))
            assert ok2 is False and resp2["ok"] is False
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 脱敏自检
# ---------------------------------------------------------------------------

def test_redaction_selfcheck(tmp_path):
    from bridge.redact import CODEX_HOME

    clean = tmp_path / "clean.json"
    clean.write_text('{"seq": 1, "state": "done"}', encoding="utf-8")
    assert svc.redaction_selfcheck([clean]) == []

    dirty = tmp_path / "dirty.json"
    dirty.write_text(json.dumps({
        "home": CODEX_HOME + "/secret",
        "email": "a@b.co",
        "key": "sk-" + "x" * 30,
    }), encoding="utf-8")
    violations = svc.redaction_selfcheck([dirty])
    assert len(violations) == 3
    assert any("home path" in v for v in violations)
