"""tests/transport/wss 共用夹具（P3.3 loopback，A4-W）。

本目录测试只覆盖链路语义：认证/尺寸/错误码/退避/重连/字节透传。
快照字节由本夹具构造（构造 JSON 是测试夹具的职责，不是 Transport 的职责；
Transport 侧断言仅针对字节与计数，不解析业务语义——业务字段的断言只出现在
测试的"验证者"一侧）。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Callable, List, Optional

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge.transports.wss import (  # noqa: E402
    BackoffConfig,
    LinkState,
    MockDeviceClient,
    SendStatus,
    WssClientConfig,
    WssServer,
    WssServerConfig,
)
from bridge.transports.wss.server import SendStatus as ServerSendStatus  # noqa: E402

DEFAULT_TOKEN = "dev-token-abc123"

#: 测试用缩短退避（形状与冻结序列一致：等比 ×2、±20% 抖动、60s 稳定重置）；
#: 冻结数值 1/2/4/8/16/30 由 test_backoff_and_failures.py 用默认配置锁定。
TEST_BACKOFF = BackoffConfig(sequence_s=(0.05, 0.1, 0.2, 0.4, 0.8, 1.0))


# ---------------------------------------------------------------------------
# 快照字节构造（schema 形状；逐字节确定性，seq/epoch 可控）
# ---------------------------------------------------------------------------


def make_app_state(
    epoch: str = "mock-run-001",
    seq: int = 1,
    *,
    activity: str = "演示活动：检查需求",
    project: str = "codex-desk-terminal",
) -> bytes:
    """构造一份 schema 形状的完整 AppState JSON 字节（测试夹具）。"""
    snapshot = {
        "schema_version": 1,
        "kind": "state",
        "bridge_epoch": epoch,
        "seq": seq,
        "generated_at_ms": 1789000000000 + seq,
        "source": {
            "kind": "mock",
            "connected": True,
            "stale": False,
            "last_event_at_ms": 1789000000000,
        },
        "selected_thread_id": "thread-demo",
        "threads_total": 1,
        "threads_truncated": False,
        "threads": [
            {
                "id": "thread-demo",
                "turn_id": "turn-001",
                "project": project,
                "state": "working",
                "activity": activity,
                "updated_at_ms": 1789000000000,
                "elapsed_ms": 1000,
                "waiting_ms": 0,
                "end_reason": None,
                "attention": None,
                "plan": {"total": 0, "truncated": False, "steps": []},
                "context": {
                    "used_tokens": None,
                    "capacity_tokens": None,
                    "used_percent": None,
                },
            }
        ],
        "usage": {
            "available": False,
            "updated_at_ms": None,
            "windows_total": 0,
            "windows_truncated": False,
            "windows": [],
        },
    }
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# 异步测试小工具（不依赖 pytest-asyncio：每个测试 asyncio.run 驱动）
# ---------------------------------------------------------------------------


async def wait_for(
    predicate: Callable[[], bool],
    timeout: float = 5.0,
    interval: float = 0.02,
    what: str = "condition",
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"超时等待 {what}（{timeout:.1f}s）")


class FrameSink:
    """on_message 收集器：按序记录帧字节，供逐帧断言。"""

    def __init__(self) -> None:
        self.frames: List[bytes] = []

    def __call__(self, data: bytes, n: int) -> None:
        self.frames.append(bytes(data))

    async def next(self, timeout: float = 5.0) -> bytes:
        """等待并返回"下一帧"（按调用时刻的长度游标）。

        注意帧可能在本调用前已到达：到达时机不确定的断言请用 wait_count。
        """
        index = len(self.frames)
        await wait_for(
            lambda: len(self.frames) > index,
            timeout=timeout,
            what=f"第 {index + 1} 帧（当前 {len(self.frames)} 帧）",
        )
        return self.frames[index]

    async def wait_count(self, n: int, timeout: float = 5.0) -> None:
        """与游标无关：等待累计收到至少 n 帧。"""
        await wait_for(
            lambda: len(self.frames) >= n,
            timeout=timeout,
            what=f"累计 {n} 帧（当前 {len(self.frames)} 帧）",
        )


class LinkLog:
    """on_link 记录器：[(LinkState, detail)]。"""

    def __init__(self) -> None:
        self.events: List[tuple] = []

    def __call__(self, state, detail=None) -> None:
        self.events.append((state, detail))


# ---------------------------------------------------------------------------
# 服务端 / 客户端启动助手
# ---------------------------------------------------------------------------


async def start_server(
    *,
    token: str = DEFAULT_TOKEN,
    snapshot: Optional[bytes] = None,
    provider=None,
    max_clients: int = 1,
    keepalive: Optional[float] = None,
    tls: Optional[tuple] = None,
    host: str = "127.0.0.1",
    port: int = 0,
    on_message=None,
    on_link=None,
) -> WssServer:
    """启动测试服务端（默认 loopback 明文 dev 标记；传 tls=(cert,key) 启用 wss）。"""
    kwargs = {}
    if tls is not None:
        kwargs["tls_cert_file"], kwargs["tls_key_file"] = tls
    cfg = WssServerConfig(
        host=host,
        port=port,
        max_clients=max_clients,
        keepalive_interval_s=keepalive,
        allow_insecure_loopback=tls is None,
        **kwargs,
    )
    server = WssServer(
        cfg,
        device_token=token,
        snapshot_provider=provider,
        on_message=on_message,
        on_link=on_link,
    )
    await server.start()
    if snapshot is not None:
        status = await server.send(snapshot)
        assert status is ServerSendStatus.ACCEPTED
    return server


def ws_url(server: WssServer) -> str:
    return f"ws://127.0.0.1:{server.bound_port}/v1/state"


def wss_url(server: WssServer, hostname: str = "127.0.0.1") -> str:
    return f"wss://{hostname}:{server.bound_port}/v1/state"


def make_client(
    url: str,
    *,
    token: str = DEFAULT_TOKEN,
    sink: Optional[FrameSink] = None,
    links: Optional[LinkLog] = None,
    rng=None,
    backoff: BackoffConfig = TEST_BACKOFF,
    ca_file: Optional[str] = None,
    spki: Optional[str] = None,
) -> MockDeviceClient:
    cfg = WssClientConfig(
        url=url,
        dev_insecure_loopback=ca_file is None,
        ca_file=ca_file,
        spki_sha256_hex=spki,
        backoff=backoff,
    )
    return MockDeviceClient(
        cfg,
        device_token=token,
        on_message=sink,
        on_link=links if links is not None else None,
        rng=rng,
    )


async def wait_connected(client: MockDeviceClient, timeout: float = 5.0) -> None:
    await wait_for(
        lambda: client.link_state is LinkState.CONNECTED,
        timeout=timeout,
        what=f"客户端 CONNECTED（当前 {client.link_state}）",
    )


async def wait_link(
    client: MockDeviceClient, state: LinkState, timeout: float = 5.0
) -> None:
    await wait_for(
        lambda: client.link_state is state,
        timeout=timeout,
        what=f"客户端 {state}（当前 {client.link_state}）",
    )


async def stop_client(client: MockDeviceClient, task: asyncio.Task) -> None:
    await client.stop()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except asyncio.TimeoutError:
        task.cancel()
        raise


# ---------------------------------------------------------------------------
# 开发证书夹具（scripts/gen_dev_certs.sh 为唯一生成入口）
# ---------------------------------------------------------------------------


def _gen_certs(tmp_path_factory, name: str):
    if shutil.which("openssl") is None:
        pytest.skip("openssl 不可用")
    script = REPO_ROOT / "scripts" / "gen_dev_certs.sh"
    if not script.exists():
        pytest.skip("scripts/gen_dev_certs.sh 不存在")
    out = tmp_path_factory.mktemp(name)
    proc = subprocess.run(
        ["sh", str(script), str(out), "--force"], capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        pytest.fail(f"gen_dev_certs.sh 失败（exit {proc.returncode}）: {proc.stderr}")
    spki = (out / "server_spki_sha256.txt").read_text().strip()
    return {
        "dir": out,
        "ca": str(out / "ca.pem"),
        "cert": str(out / "server.crt"),
        "key": str(out / "server.key"),
        "spki": spki,
        "script_output": proc.stdout,
    }


@pytest.fixture(scope="session")
def dev_certs(tmp_path_factory):
    """第一套开发证书（CA + 服务器证书 + SPKI 指纹）。"""
    return _gen_certs(tmp_path_factory, "dev-certs")


@pytest.fixture(scope="session")
def alt_certs(tmp_path_factory):
    """第二套独立 CA（用于"错误 CA → 证书验证失败 = CONFIG_ERROR"路径）。"""
    return _gen_certs(tmp_path_factory, "alt-certs")
