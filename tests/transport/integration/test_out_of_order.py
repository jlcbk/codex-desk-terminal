"""P3.5 故障矩阵·乱序（O1–O3）：片乱序/消息交叠/seq 乱序 → 终点收敛于最新。

验收锚点（DEVELOPMENT_PLAN P3.5 行 + INTERFACES §3/§7）：
  - BLE 片乱序（位图重组）与两消息片交叠（一次仅 1 条在途 → 矛盾头拒绝、
    残留孤儿片拒绝）→ 旧消息不污染新消息；
  - WSS 按 TCP 序到达但业务层注入 seq 乱序快照 → 低 seq 被 store 丢弃；
  - UI 不回退（真实 presenter 判定）+ link 事件序列记录。
"""

from __future__ import annotations

import asyncio
import random

from bridge.transports.ble.fragmenter import Fragmenter

import p35_common as pc
import wss_driver as wd


# ---------------------------------------------------------------------------
# O1：BLE 单消息片乱序（位图重组；MTU 23/53/247 矩阵）
# ---------------------------------------------------------------------------


def _fragment_gap(frame_count: int, cap_ms: int = 200) -> int:
    """片间隔：远小于 3s 进展阈值，且整趟时长受控（避开 30s 整包阈值）。"""
    return max(1, min(cap_ms, 15000 // (frame_count + 1)))


def _o1_case(mtu: int, harness_bin: str) -> None:
    """乱序注入分两趟，对应 §7 冻结恢复语义：
    第 1 趟：全片乱序到达 —— index 0 之前到达的片被孤儿片拒绝（NACK）；
    第 2 趟：发送端按 NACK 全量重发（§7：rejected → NACK → 重发全包），
    重复片忽略、缺失片补齐 → 恰好完成一次重组并应用一次。
    """
    case = "O1_ble_frag_shuffle_mtu%d" % mtu
    payload = pc.make_state(epoch="run-1", seq=11, activity="shuffled-mtu%d" % mtu)
    fg = Fragmenter(payload, message_id=7, mtu=mtu)
    frames = list(fg.frames())
    rng = random.Random(20260910 + mtu)
    shuffled = list(frames)
    rng.shuffle(shuffled)

    gap1 = _fragment_gap(len(frames))
    gap2 = _fragment_gap(len(frames), cap_ms=50)
    t2 = gap1 * len(frames) + 1000  # 第 2 趟起点（发送端重发）
    entries = [pc.frag(gap1 * i, f) for i, f in enumerate(shuffled)]
    entries += [pc.frag(t2 + gap2 * i, f) for i, f in enumerate(frames)]

    report = pc.run_case(case, mtu, "ble", entries, binary=harness_bin)
    rounds = report["rounds"]
    orphans = [r for r in rounds if r["reassemble"] == "rejected:orphan"]
    assert orphans, "乱序趟必须产生孤儿片拒绝（NACK 触发全量重发的依据）"
    completed = [r for r in rounds if r["reassemble"] == "completed"]
    assert len(completed) == 1, "重发后应恰好完成一次重组（位图去重+补齐）"
    assert completed[0]["apply"] == "applied" and completed[0]["seq"] == 11
    assert completed[0]["ack"] == "ack"
    assert report["store_final"]["seq"] == 11, "乱序之后终点必须是唯一的新快照"
    assert report["ui_no_regress"], report["no_regress_notes"]
    pc.assert_link_trace(report, [(0, "connected")], case)
    print("[O1] PASS：mtu=%d %d 片乱序（%d 孤儿片 NACK）→ 全量重发 → 重组一次、"
          "应用一次、终态 seq=11" % (mtu, len(frames), len(orphans)))


def test_o1_ble_frag_shuffle_mtu23(harness_bin: str) -> None:
    _o1_case(23, harness_bin)


def test_o1_ble_frag_shuffle_mtu53(harness_bin: str) -> None:
    _o1_case(53, harness_bin)


def test_o1_ble_frag_shuffle_mtu247(harness_bin: str) -> None:
    _o1_case(247, harness_bin)


# ---------------------------------------------------------------------------
# O2：BLE 两消息片交叠 → 旧消息不污染新消息
# ---------------------------------------------------------------------------


def test_o2_ble_two_message_mixed_fragments(harness_bin: str) -> None:
    """旧消息（seq4）半途插入新消息（seq5）首片 → 矛盾头拒绝旧上下文；
    新消息整包完成后，旧消息残留片为孤儿片；终点收敛于 seq5。"""
    case = "O2_ble_two_message_mixed_fragments"
    old_fg = Fragmenter(pc.make_state(epoch="run-1", seq=4, activity="OLD-state"),
                        message_id=100, mtu=53)
    new_fg = Fragmenter(pc.make_state(epoch="run-1", seq=5, activity="NEW-state"),
                        message_id=101, mtu=53)
    old_f, new_f = list(old_fg.frames()), list(new_fg.frames())

    entries = [
        pc.frag(0, old_f[0]),
        pc.frag(10, old_f[1]),
        pc.frag(20, new_f[0]),   # 异 message_id 中途到达 → 旧上下文矛盾头拒绝
    ]
    entries += [pc.frag(100 + 100 * i, f) for i, f in enumerate(new_f)]  # 新消息整包
    entries += [pc.frag(5000, old_f[2]), pc.frag(5100, old_f[3])]        # 迟到的旧片

    report = pc.run_case(case, 53, "ble", entries, binary=harness_bin)
    reasons = [r["reassemble"] for r in report["rounds"]]
    assert "rejected:context_mismatch" in reasons, "交叠的两消息必须触发矛盾头拒绝"
    assert "rejected:orphan" in reasons, "旧消息迟到残留片必须按孤儿片拒绝"
    completed = [r for r in report["rounds"] if r["reassemble"] == "completed"]
    assert len(completed) == 1 and completed[0]["apply"] == "applied"
    assert completed[0]["seq"] == 5, "只有新消息被应用"
    assert report["store_final"]["seq"] == 5
    assert report["rounds"][-1]["view"]["activity"] == "NEW-state", \
        "旧消息不得污染新消息的 UI 内容"
    assert report["ui_no_regress"], report["no_regress_notes"]
    pc.assert_link_trace(report, [(0, "connected")], case)
    print("[O2] PASS：两消息片交叠 → 矛盾头+孤儿片拒绝，终点收敛 seq=5")


# ---------------------------------------------------------------------------
# O3：WSS 快照 seq 乱序注入 → 低 seq 丢弃
# ---------------------------------------------------------------------------


async def _o3_amain(harness_bin: str) -> None:
    case = "O3_wss_seq_disorder_low_seq_dropped"
    d = pc.case_dir(case)
    s5 = pc.make_state(epoch="run-1", seq=5, activity="FIVE-content")
    s3 = pc.make_state(epoch="run-1", seq=3, activity="THREE-content")
    s6 = pc.make_state(epoch="run-1", seq=6, activity="SIX-content")

    sink, links = wd.FrameSink(), wd.LinkLog()
    server = await wd.start_server()
    client = wd.make_client(wd.ws_url(server), sink=sink, links=links)
    task = asyncio.create_task(client.run())
    try:
        await wd.wait_connected(client)
        for s in (s5, s3, s6):  # TCP 保序；业务层注入 seq 乱序
            await wd.send_snapshot(server, s)
        await sink.wait_count(3)
    finally:
        await wd.stop_client(client, task)
        await wd.stop_server(server)

    report = pc.run_case(case, 53, "wifi", [
        pc.snap(0, s5),
        pc.snap(1000, s3),   # 低 seq 乱序注入
        pc.snap(2000, s6),
        pc.probe(3000),
    ], binary=harness_bin)

    deliveries = [r["apply"] for r in report["rounds"]]
    assert deliveries == ["applied", "ignored_stale_seq", "applied", "none"], deliveries
    assert pc.applied_pairs(report) == [("run-1", 5), ("run-1", 6)], "低 seq 不得进入应用轨迹"
    assert report["rounds"][-1]["view"]["activity"] == "SIX-content", "终态必须是最新的 seq6"
    assert report["ui_no_regress"], report["no_regress_notes"]
    pc.assert_link_trace(report, [(0, "connected")], case)
    pc.dump_json(f"{d}/wss_trace.json", {
        "messages_received": client.stats.messages_received,
        "link_states": links.states(),
    })
    print("[O3] PASS：WSS seq 乱序 5→3→6，低 seq 3 丢弃，终态收敛 seq=6")


def test_o3_wss_seq_disorder(harness_bin: str) -> None:
    asyncio.run(_o3_amain(harness_bin))
