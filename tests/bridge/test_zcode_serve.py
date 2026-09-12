"""ZC2 传输 e2e：ZCode 观察源语义事件 → StateEngine → serve 管道 → 订户逐字节对账。

对应任务书第 4 节（PC 环传输 e2e，无真机）：

- 合成 NormalizedEvent 序列（ZCode 语义，与任务书冻结序列一致）：
  source_reconnected → thread_started → thread_status active →
  approval_requested → server_request_resolved → turn_completed completed →
  rate_limits 窗口；
- 直接喂 StateEngine(source_kind=zcode_observed)（契约 v1.1）；
- 逐份快照经 serve 管道（bridge_serve_zcode.py 的同一编排：ServeHub 盖章 →
  PublishBridge → _publisher_loop → WssServer.send → 订户 MockDeviceClient）
  发送，断言订户收到的每一帧与 engine.apply 直出快照逐字节一致（复用 R6
  连续编排测试的字节级比对手法）；
- 覆盖 device token 401 拒绝路径（错 token → CONFIG_ERROR 终态 +
  server.stats.rejected_auth_401 计数）；
- 证据落 artifacts/zcode_serve/（e2e 日志；不含 token，落盘前自检）。

不 import bridge/sources/zcode.py（ZC1 领地，可能尚未落地）；本测试只依赖
契约 v1.1 已落地的 engine/render 与 P3.3 的 WSS 传输。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import time
from pathlib import Path

import pytest

from bridge import events as ev
from bridge.serve_codex import ServeHub
from bridge.state.engine import SOURCE_ZCODE_OBSERVED, StateEngine
from bridge.transports.wss import (
    BackoffConfig,
    LinkState,
    MockDeviceClient,
    WssClientConfig,
    WssServer,
    WssServerConfig,
)
from conftest import REPO_ROOT, assert_invariants, encode

SCRIPT_PATH = REPO_ROOT / "scripts" / "bridge_serve_zcode.py"
ART = REPO_ROOT / "artifacts" / "zcode_serve"

EPOCH = "zcode-e2e-001"
ANCHOR_MS = 1789000000000          # 与 SCENARIOS.md §2 虚构 UTC 基准一致
TOKEN = "zc2-e2e-dev-token"        # 测试注入凭证；不入任何日志/证据
TEST_BACKOFF = BackoffConfig(sequence_s=(0.05, 0.1, 0.2, 0.4, 0.8, 1.0))
WAIT_S = 10.0

THREAD_ID = "thr-z1"
TURN_ID = "turn-z1"


def load_script():
    """按路径加载 scripts/bridge_serve_zcode.py（WSS/ZC1 导入延迟，无需
    websockets 也可加载——这正是 ZC2 的契约：--help/--dry-run 不受依赖影响）。"""
    spec = importlib.util.spec_from_file_location("bridge_serve_zcode_script",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    return load_script()


def zcode_event_sequence() -> list:
    """任务书冻结的 ZCode 语义事件序列（固定虚拟时钟，确定性输出）。"""
    at = lambda ms: ANCHOR_MS + ms  # noqa: E731
    return [
        (ev.source_reconnected(at_ms=at(1000)), 1000),
        (ev.thread_started(THREAD_ID, project="codex-desk-terminal",
                           at_ms=at(2000)), 2000),
        (ev.thread_status(THREAD_ID, "active", at_ms=at(3000)), 3000),
        (ev.approval_requested(THREAD_ID, request_id=42,
                               summary="Allow command: pytest tests/bridge -q",
                               turn_id=TURN_ID, at_ms=at(4000)), 4000),
        (ev.server_request_resolved(THREAD_ID, request_id=42,
                                    at_ms=at(5000)), 5000),
        (ev.turn_completed(THREAD_ID, TURN_ID, "completed",
                           summary="任务完成", at_ms=at(6000)), 6000),
        (ev.rate_limits(
            [
                {"id": "primary", "label": "5h", "used_percent": 29.0,
                 "duration_mins": 300, "resets_at_ms": at(3600 * 1000)},
                {"id": "secondary", "label": "weekly", "used_percent": 61.0,
                 "duration_mins": 10080, "resets_at_ms": at(7 * 86400 * 1000)},
            ],
            at_ms=at(7000)), 7000),
    ]


def build_engine_snapshots() -> list[dict]:
    """事件序列直喂 StateEngine 的逐份快照（engine.apply 直出，seq 1..7）。"""
    engine = StateEngine(EPOCH, source_kind=SOURCE_ZCODE_OBSERVED,
                         utc_anchor_ms=ANCHOR_MS)
    return [engine.apply(event, mono) for event, mono in zcode_event_sequence()]


async def _wait_for(predicate, timeout: float = WAIT_S, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("超时等待 %s（%.1fs）" % (what, timeout))


def make_server(port: int = 0, *, token: str = TOKEN, on_link=None) -> WssServer:
    cfg = WssServerConfig(
        host="127.0.0.1", port=port, max_clients=2,
        keepalive_interval_s=None,  # 静止点对账：keepalive 不注入额外帧
        allow_insecure_loopback=True,
    )
    return WssServer(cfg, device_token=token,
                     snapshot_provider=None, on_link=on_link)


def make_client(url: str, *, token: str = TOKEN, on_message=None,
                on_link=None) -> MockDeviceClient:
    cfg = WssClientConfig(url=url, dev_insecure_loopback=True,
                          backoff=TEST_BACKOFF, connect_timeout_s=5.0)
    return MockDeviceClient(cfg, device_token=token, on_message=on_message,
                            on_link=on_link)


# ---------------------------------------------------------------------------
# serve 脚本纯逻辑（不依赖 ZC1 模块）
# ---------------------------------------------------------------------------

def test_script_loads_without_zc1_and_websockets(script):
    """契约：脚本顶层不 import ZC1 观察器（观察器在 observer_worker 内延迟导入，
    ZC1 未落地时本脚本 --help/--dry-run 仍可用——加载本脚本本身即证明）。"""
    assert hasattr(script, "observer_worker")
    source = Path(SCRIPT_PATH).read_text(encoding="utf-8")
    top = source.split("def observer_worker")[0]
    assert "from bridge.sources.zcode" not in top, \
        "观察器 import 必须延迟到 observer_worker 内"


def test_script_initial_snapshot_source_kind(script, validator):
    snap = script.initial_snapshot(EPOCH, ANCHOR_MS, now_mono=0)
    assert snap["source"]["kind"] == SOURCE_ZCODE_OBSERVED == "zcode_observed"
    assert snap["source"]["connected"] is True
    assert snap["bridge_epoch"] == EPOCH and snap["seq"] == 0
    assert snap["threads"] == []
    assert not list(validator.iter_errors(snap))
    assert_invariants(snap)


def test_script_resolve_spool_default_parity(script):
    """服务端 spool 默认与 hook 写入端同源：CDT_HOOK_SPOOL 优先。"""
    assert script.resolve_spool_path({}) == script.DEFAULT_SPOOL
    assert script.resolve_spool_path({"CDT_HOOK_SPOOL": "/tmp/w.jsonl"}) == \
        "/tmp/w.jsonl"


def test_script_resolve_tls_variants(script, tmp_path):
    class Args:
        cert = None
        key = None
        certs_dir = str(tmp_path)
        allow_insecure_loopback = False
    cert, key, plaintext = script.resolve_tls(Args)
    assert cert is None and key is None and plaintext is False  # 缺证书→报错路径
    (tmp_path / "server.crt").write_text("c")
    (tmp_path / "server.key").write_text("k")
    cert, key, plaintext = script.resolve_tls(Args)
    assert cert == str(tmp_path / "server.crt") and not plaintext
    Args.allow_insecure_loopback = True
    cert, key, plaintext = script.resolve_tls(Args)
    assert cert is None and plaintext is True


def test_script_dry_run_exit_zero(script, tmp_path):
    """--dry-run：不起 WSS，打印路径/参数摘要，退出 0（无设备环境冒烟）。"""
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env["CDT_HOOK_SPOOL"] = str(tmp_path / "spool.jsonl")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dry-run",
         "--rollout-dir", str(tmp_path / "rollout"),
         "--agents-dir", str(tmp_path / "agents"),
         "--port", "8797"],
        capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "dry-run PASS" in proc.stdout
    assert "zcode_observed" in proc.stdout
    # 摘要只报存在性，不打印 token 内容
    assert "device_token 内容" not in proc.stdout


# ---------------------------------------------------------------------------
# e2e 主链：事件 → engine → serve 管道 → 订户逐字节一致
# ---------------------------------------------------------------------------

def test_e2e_zcode_serve_byte_identity(tmp_path, validator, script):
    verdict = asyncio.run(_e2e_byte_identity(ART, validator, script))
    assert verdict["pass"] is True
    assert verdict["frames_received"] == 7
    assert verdict["byte_mismatches"] == []
    assert verdict["token_leak"] is False


async def _e2e_byte_identity(evidence_dir: Path, validator, script) -> dict:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    engine_snaps = build_engine_snapshots()
    expected = [encode(s) for s in engine_snaps]  # engine.apply 直出快照编码

    server = make_server()
    await server.start()
    url = "ws://127.0.0.1:%d/v1/state" % server.bound_port

    # 与 scripts/bridge_serve_zcode.py 相同的编排对象（复用脚本实现，不另写一份）：
    # ServeHub 盖章（同 epoch 字符串 → 字节中立）→ PublishBridge → publisher 任务。
    hub = ServeHub(EPOCH)
    bridge = script.PublishBridge()
    sink = script.make_sink(hub, bridge)
    bridge.start(asyncio.get_running_loop())
    publisher = asyncio.create_task(script._publisher_loop(bridge.queue, server))

    received: list[bytes] = []
    link_log: list[str] = []

    def on_message(data: bytes, _n: int) -> None:
        received.append(bytes(data))

    def on_link(state, _detail=None) -> None:
        link_log.append(state.value if hasattr(state, "value") else str(state))

    client = make_client(url, on_message=on_message, on_link=on_link)
    client_task = asyncio.create_task(client.run())

    try:
        await _wait_for(lambda: client.link_state is LinkState.CONNECTED,
                        what="订户 CONNECTED")
        # 订户已连接：经生产 sink 逐份投递（观察器线程在真实服务中的等价调用）。
        for snap in engine_snaps:
            sink(snap)
        await _wait_for(lambda: len(received) >= len(expected),
                        what="订户收齐 %d 帧" % len(expected))
        await asyncio.sleep(0.1)  # 静止点：确认没有多余帧
    finally:
        await client.stop()
        try:
            await asyncio.wait_for(asyncio.shield(client_task), timeout=5.0)
        except asyncio.TimeoutError:
            client_task.cancel()
        publisher.cancel()
        try:
            await publisher
        except asyncio.CancelledError:
            pass
        await server.stop()

    # ---- 断言：逐字节一致（复用 R6 e2e 比对手法）--------------------------
    mismatches = []
    for i, (got, want) in enumerate(zip(received, expected)):
        if got != want:
            mismatches.append({"frame": i, "got_len": len(got),
                               "want_len": len(want)})
    assert len(received) == len(expected), (
        "帧数不符：received=%d expected=%d" % (len(received), len(expected)))
    assert not mismatches, "字节不一致: %r" % mismatches
    assert "config_error" not in link_log, link_log

    # ---- 语义与契约检查 ----------------------------------------------------
    states_timeline = []
    frames = []
    for i, raw in enumerate(received):
        snap = json.loads(raw.decode("utf-8"))
        assert not list(validator.iter_errors(snap)), "frame %d schema 违例" % i
        assert_invariants(snap)
        assert snap["bridge_epoch"] == EPOCH
        assert snap["seq"] == i + 1, "同 epoch 内 seq 严格递增（1 起）"
        assert snap["source"]["kind"] == "zcode_observed"
        assert snap["source"]["connected"] is True
        sel = snap["threads"][0] if snap["threads"] else None
        states_timeline.append(sel["state"] if sel else None)
        frames.append({
            "frame": i,
            "seq": snap["seq"],
            "bytes": len(raw),
            "sha256_16": hashlib.sha256(raw).hexdigest()[:16],
            "states": [t["state"] for t in snap["threads"]],
            "source_kind": snap["source"]["kind"],
        })

    # ZCode 语义时间线（reducer 冻结行为）：
    # 无线程 → idle → working → needs_you → idle(resolved 后无 turn 归 idle)
    # → done → done
    assert states_timeline == [None, "idle", "working", "needs_you", "idle",
                               "done", "done"], states_timeline
    usage = json.loads(received[-1].decode("utf-8"))["usage"]
    assert usage["available"] is True and len(usage["windows"]) == 2
    assert [w["label"] for w in usage["windows"]] == ["5h", "weekly"]
    assert json.loads(received[3].decode("utf-8"))["threads"][0][
        "attention"] is not None, "approval → NEEDS YOU 注意呈现"

    # server 侧对账：无去重（每帧字节不同）、无超限拒绝
    assert server.stats.snapshots_pushed == len(expected)
    assert server.stats.snapshots_deduped == 0
    assert server.stats.oversize_out_rejected == 0

    # ---- 证据落盘（不含 token）---------------------------------------------
    report = {
        "case": "zc2_e2e_byte_identity",
        "epoch": EPOCH,
        "source_kind": "zcode_observed",
        "frames_received": len(received),
        "byte_mismatches": mismatches,
        "server_stats": {
            "snapshots_pushed": server.stats.snapshots_pushed,
            "snapshots_deduped": server.stats.snapshots_deduped,
            "rejected_auth_401": server.stats.rejected_auth_401,
        },
        "link_log": link_log,
        "frames": frames,
        "pass": True,
    }
    (evidence_dir / "e2e_frames.jsonl").write_text(
        "\n".join(json.dumps(f, ensure_ascii=False) for f in frames) + "\n",
        encoding="utf-8")
    (evidence_dir / "e2e_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    report["token_leak"] = TOKEN in json.dumps(report) or \
        TOKEN in (evidence_dir / "e2e_frames.jsonl").read_text(encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# 401 拒绝路径
# ---------------------------------------------------------------------------

def test_e2e_wrong_token_rejected_401(tmp_path):
    verdict = asyncio.run(_e2e_wrong_token(ART))
    assert verdict["client_terminal"] == "config_error"
    assert verdict["rejected_auth_401"] == 1


async def _e2e_wrong_token(evidence_dir: Path) -> dict:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    server = make_server()
    await server.start()
    url = "ws://127.0.0.1:%d/v1/state" % server.bound_port
    # 错 token：CONFIG_ERROR 终态（§5.3），不重试
    bad = make_client(url, token="definitely-wrong-token")
    bad_task = asyncio.create_task(bad.run())
    final_state = None
    try:
        final_state = await asyncio.wait_for(bad_task, timeout=WAIT_S)
    except asyncio.TimeoutError:
        bad_task.cancel()
        raise AssertionError("错 token 客户端未按 CONFIG_ERROR 终态返回")
    finally:
        await server.stop()
    assert final_state is LinkState.CONFIG_ERROR, final_state
    assert bad.stats.config_errors == 1
    assert bad.stats.messages_received == 0
    assert server.stats.rejected_auth_401 == 1
    assert server.stats.connections_accepted == 0
    report = {
        "case": "zc2_e2e_token_401",
        "client_terminal": final_state.value,
        "rejected_auth_401": server.stats.rejected_auth_401,
        "connections_accepted": server.stats.connections_accepted,
        "pass": True,
        "note": "token 值不写入本报告（红线）",
    }
    (evidence_dir / "e2e_token401.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return report
