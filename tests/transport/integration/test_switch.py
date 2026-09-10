"""P3.5 故障矩阵·切换 transport（S1–S2）：stop 旧适配器 → start 新适配器。

验收锚点（DEVELOPMENT_PLAN P3.5 行「一次仅一个活动 transport」+ INTERFACES §8
「设备配置切换 transport 须 stop 旧适配器再 start 新适配器」+ §5 队列语义
「切换期间 store 保留最后合法状态」）：

  - S1：真实 WSS 客户端会话 + BLE stub（仅生命周期建模；帧语义经 C harness）
    → 生命周期钩子记录 start/stop 交错 → 断言任意时刻至多一个活动 transport
    （重叠即 FAIL）→ 切换间隙 probe 证明 store 保留最后合法状态；
  - S2：重叠检测器自检——构造故意重叠与失配的生命周期日志必须 FAIL
    （检测器失效=验收失效，与「一个像素必须失败」同一哲学）。

判定层级：store/presenter 终点收敛在 C harness；生命周期交错在本模块断言。
"""

from __future__ import annotations

import asyncio
import os

from bridge.transports.ble.fragmenter import Fragmenter

import p35_common as pc
import wss_driver as wd


class BleStubTransport:
    """BLE 适配器生命周期 stub（§5 Transport 行的 start/stop 形状）。

    红线说明：仅建模生命周期（host 无 GATT 栈）；帧语义/收敛由 P3.4 的
    真实重组器在 C harness 内验证，stub 不产生业务状态。
    """

    def __init__(self, name: str, log: pc.LifecycleLog) -> None:
        self._name = name
        self._log = log
        self.active = False

    async def start(self) -> None:
        assert not self.active, "stub 重复 start"
        self._log.start_begin(self._name)
        await asyncio.sleep(0)  # 模拟驱动初始化让出
        self.active = True
        self._log.start_end(self._name)

    async def stop(self) -> None:
        assert self.active, "stub 未 start 即 stop"
        self._log.stop_begin(self._name)
        await asyncio.sleep(0)  # 模拟驱动清理让出
        self.active = False
        self._log.stop_end(self._name)


# ---------------------------------------------------------------------------
# S1：stop(WSS) → start(BLE stub)，全程至多一个活动 transport
# ---------------------------------------------------------------------------


async def _s1_amain(harness_bin: str) -> None:
    case = "S1_switch_stop_wss_start_ble_single_active"
    d = pc.case_dir(case)
    log = pc.LifecycleLog()

    wss_snap = pc.make_state(epoch="ep-switch", seq=1, activity="before-switch")
    ble_snap = pc.make_state(epoch="ep-switch", seq=2, activity="after-switch")

    # -- 阶段 1：WSS 会话（设备侧视角 = MockDeviceClient 会话） --
    log.start_begin("wss")
    sink = wd.FrameSink()
    server = await wd.start_server()
    client = wd.make_client(wd.ws_url(server), sink=sink)
    task = asyncio.create_task(client.run())
    await wd.wait_connected(client)
    log.start_end("wss")
    await wd.send_snapshot(server, wss_snap)
    await sink.wait_count(1)
    assert sink.frames[0] == wss_snap

    # -- 阶段 2：先 stop 旧适配器，完成后才允许 start 新适配器（§8 顺序） --
    log.stop_begin("wss")
    await wd.stop_client(client, task)
    await wd.stop_server(server)
    log.stop_end("wss")

    # -- 阶段 3：start BLE stub（帧注入序列记录进容器，经真实重组器收敛） --
    ble = BleStubTransport("ble", log)
    await ble.start()
    ble_frames = list(Fragmenter(ble_snap, message_id=900, mtu=53).frames())
    await ble.stop()  # 场景结束，stub 关停（仍在切换序列内）

    intervals = pc.assert_single_active(log)  # 重叠即 FAIL
    names = [n for n, _, _ in intervals]
    assert names == ["wss", "ble"], names
    wss_iv, ble_iv = intervals
    assert wss_iv[2] <= ble_iv[1], "必须先完成 stop(WSS) 再 start(BLE)（§8 顺序）"

    # -- 终点收敛：切换间隙 store 保留，BLE 新快照接续应用 --
    entries = [pc.snap(0, wss_snap), pc.probe(2500)]  # 切换间隙观测点
    entries += [pc.frag(5000 + 100 * i, f) for i, f in enumerate(ble_frames)]
    entries.append(pc.probe(5000 + 100 * len(ble_frames) + 500))  # 末帧之后观测
    report = pc.run_case(case, 53, "ble", entries, binary=harness_bin)

    rounds = report["rounds"]
    assert rounds[1]["action"] == "probe" and rounds[1]["apply"] == "none"
    assert rounds[1]["view"]["activity"] == "before-switch", \
        "切换间隙 store 必须保留最后合法状态"
    completed = [r for r in rounds if r["reassemble"] == "completed"]
    assert len(completed) == 1 and completed[0]["apply"] == "applied"
    assert completed[0]["seq"] == 2
    assert rounds[-1]["view"]["activity"] == "after-switch"
    assert pc.applied_pairs(report) == [("ep-switch", 1), ("ep-switch", 2)]
    assert report["ui_no_regress"], report["no_regress_notes"]

    pc.dump_json(os.path.join(d, "lifecycle.json"), {
        "events": [{"t_monotonic": t, "kind": k, "name": n} for t, k, n in log.events],
        "intervals": [{"name": n, "begin": s, "end": e} for n, s, e in intervals],
        "verdict": "single_active",
    })
    print("[S1] PASS：stop(WSS)→start(BLE) 无重叠；切换间隙 store 保留；BLE 快照收敛")


def test_s1_switch_single_active(harness_bin: str) -> None:
    asyncio.run(_s1_amain(harness_bin))


# ---------------------------------------------------------------------------
# S2：重叠检测器自检（故意重叠/事件失配必须 FAIL）
# ---------------------------------------------------------------------------


def test_s2_switch_overlap_detector_selftest() -> None:
    # 干净日志必须通过
    clean = pc.LifecycleLog()
    clean.start_begin("wss")
    clean.start_end("wss")
    clean.stop_begin("wss")
    clean.stop_end("wss")
    clean.start_begin("ble")
    clean.start_end("ble")
    clean.stop_begin("ble")
    clean.stop_end("ble")
    assert len(pc.assert_single_active(clean)) == 2

    # 故意重叠：BLE 在 WSS stop_end 之前 start_begin → 必须 FAIL
    overlap = pc.LifecycleLog()
    overlap.start_begin("wss")
    overlap.start_end("wss")
    overlap.stop_begin("wss")
    overlap.start_begin("ble")  # 旧适配器尚未完成 stop
    overlap.start_end("ble")
    overlap.stop_end("wss")
    try:
        pc.assert_single_active(overlap)
    except AssertionError as exc:
        assert "重叠" in str(exc), str(exc)
    else:
        raise AssertionError("重叠检测器失效：故意重叠的生命周期未被拒绝")

    # 事件失配：stop_end 无配对 start_begin → 必须 FAIL
    bad = pc.LifecycleLog()
    bad.stop_end("wss")
    try:
        pc.assert_single_active(bad)
    except AssertionError:
        pass
    else:
        raise AssertionError("重叠检测器失效：失配事件未被拒绝")
    print("[S2] PASS：重叠/失配检测器自检（故意违规均被拒绝）")
