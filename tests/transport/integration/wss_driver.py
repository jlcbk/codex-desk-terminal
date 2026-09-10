"""wss_driver.py — P3.5 集成测试的 WSS 真链路驱动（复用 P3.3 交付，零改动）。

只做薄封装：bridge/transports/wss 的 WssServer + MockDeviceClient 在 127.0.0.1
真实 socket 上运行（与 tests/transport/wss/conftest.py 同模式；该文件属 P3.3
目录，不跨目录 import，这里保留最小副本并注明出处）。本模块不复制任何协议
语义——认证 token、退避形状均来自 P3.3 冻结实现。

「Bridge 重启」用同端口重建 WssServer（Bridge 进程重启的真实拓扑：地址不变，
连接中断后设备退避重连）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Callable, List, Optional

REPO = None  # 由 conftest.py 注入 sys.path；此处直接 import bridge.*


from bridge.transports.wss import (  # noqa: E402
    BackoffConfig,
    LinkState,
    MockDeviceClient,
    WssServer,
    WssServerConfig,
)
from bridge.transports.wss.server import SendStatus as ServerSendStatus  # noqa: E402

#: 测试用缩短退避（形状与冻结序列一致；冻结数值 1/2/4/8/16/30s 由 P3.3 锁定）
TEST_BACKOFF = BackoffConfig(sequence_s=(0.05, 0.1, 0.2, 0.4, 0.8, 1.0))

DEFAULT_TOKEN = "p35-integration-dev-token"


async def send_snapshot(server: WssServer, data: bytes) -> None:
    """server.send 并断言 ACCEPTED（注意：server.send 返回 server.SendStatus，
    与包级导出的 client.SendStatus 是两个枚举，P3.3 conftest 同款双导入）。"""
    status = await server.send(data)
    assert status is ServerSendStatus.ACCEPTED, status


class FrameSink:
    """on_message 收集器：按序记录下行帧字节（P3.3 conftest 同款最小副本）。"""

    def __init__(self) -> None:
        self.frames: List[bytes] = []

    def __call__(self, data: bytes, n: int) -> None:
        self.frames.append(bytes(data))

    async def wait_count(self, n: int, timeout: float = 5.0) -> None:
        await wait_for(lambda: len(self.frames) >= n, timeout=timeout,
                       what="累计 %d 帧（当前 %d）" % (n, len(self.frames)))


class LinkLog:
    """on_link 记录器：[(state, detail)]（连接状态可观测的 Python 侧证据）。"""

    def __init__(self) -> None:
        self.events: List[tuple] = []

    def __call__(self, state, detail=None) -> None:
        self.events.append((state, detail))

    def states(self) -> List[str]:
        return [getattr(s, "value", str(s)) for s, _ in self.events]


async def wait_for(predicate: Callable[[], bool], timeout: float = 5.0,
                   interval: float = 0.02, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("超时等待 %s（%.1fs）" % (what, timeout))


async def start_server(*, token: str = DEFAULT_TOKEN, provider=None, port: int = 0,
                       keepalive: Optional[float] = None, host: str = "127.0.0.1",
                       on_link=None) -> WssServer:
    """启动测试服务端（明文 ws:// 仅 loopback，显式 dev 标记，同 P3.3 夹具）。"""
    cfg = WssServerConfig(
        host=host,
        port=port,
        max_clients=1,
        keepalive_interval_s=keepalive,
        allow_insecure_loopback=True,
    )
    server = WssServer(cfg, device_token=token, snapshot_provider=provider,
                       on_link=on_link)
    await server.start()
    return server


def ws_url(server: WssServer) -> str:
    return "ws://127.0.0.1:%d/v1/state" % server.bound_port


def make_client(url: str, *, token: str = DEFAULT_TOKEN, sink: Optional[FrameSink] = None,
                links: Optional[LinkLog] = None,
                backoff: BackoffConfig = TEST_BACKOFF) -> MockDeviceClient:
    from bridge.transports.wss import WssClientConfig

    cfg = WssClientConfig(url=url, dev_insecure_loopback=True, backoff=backoff)
    return MockDeviceClient(cfg, device_token=token, on_message=sink,
                            on_link=links)


async def wait_connected(client: MockDeviceClient, timeout: float = 5.0) -> None:
    await wait_for(lambda: client.link_state is LinkState.CONNECTED,
                   timeout=timeout, what="客户端 CONNECTED（当前 %s）" % client.link_state)


async def wait_link(client: MockDeviceClient, state: LinkState,
                    timeout: float = 5.0) -> None:
    await wait_for(lambda: client.link_state is state, timeout=timeout,
                   what="客户端 %s（当前 %s）" % (state, client.link_state))


async def stop_client(client: MockDeviceClient, task: asyncio.Task) -> None:
    """停客户端并等待 run() 循环退出（P3.3 conftest 同款语义）。

    不等待 STOPPED 状态：stop 打断退避/失败路径时 run() 以 DISCONNECTED
    返回（客户端实现 冻结行为），任务退出即算停止。
    """
    await client.stop()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except asyncio.TimeoutError:
        task.cancel()
        raise


async def stop_server(server: WssServer) -> None:
    await server.stop()
