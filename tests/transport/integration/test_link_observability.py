"""P3.5 连接状态可观测（L1）：§4 链路新鲜度全时序（虚拟时钟）。

验收锚点（DEVELOPMENT_PLAN P3.5 行「连接状态可观测」+ INTERFACES §4）：
  45s 未收到有效快照 → stale；150s 无更新 → disconnected；恢复由「应用成功」
  触发回 connected；**同 seq 重复数据不得续鲜**（§4 明文）——本用例用判别点
  验证：重复快照在 40s 送达，若它刷新了 last_rx，46s 处将不会转 stale。

事件序列（精确比对）：
  connected@0 → (重复@40s 不续鲜) → stale@46s → disconnected@151s
  → connected@152s（新 epoch 快照应用）→ 观测点 153s 各标志复位。
"""

from __future__ import annotations

import p35_common as pc


def test_l1_link_freshness_timeline(harness_bin: str) -> None:
    case = "L1_link_freshness_timeline"
    s1 = pc.make_state(epoch="ep-L", seq=1, activity="L1-base")
    dup = pc.make_state(epoch="ep-L", seq=1, activity="L1-dup-bytes")  # 同 seq 异字节
    s2 = pc.make_state(epoch="ep-L2", seq=1, activity="L1-recovered")

    entries = [
        pc.snap(0, s1),        # applied，last_rx=0
        pc.snap(40000, dup),   # 同 epoch 同 seq → ignored；不得刷新 last_rx
        pc.probe(46000),       # 46s 无有效快照 → stale
        pc.probe(151000),      # 151s → disconnected
        pc.snap(152000, s2),   # 新 epoch（seq 重置）→ applied → 恢复 connected
        pc.probe(153000),
    ]

    report = pc.run_case(case, 53, "wifi", entries, binary=harness_bin)

    # 精确事件序列（含恢复）；若重复续鲜了 last_rx，stale 将不出现 → 此处 FAIL
    pc.assert_link_trace(report, [
        (0, "connected"),
        (46000, "stale"),
        (151000, "disconnected"),
        (152000, "connected"),
    ], case)

    rounds = report["rounds"]
    assert rounds[1]["apply"] == "ignored_stale_seq", "重复快照必须被丢弃"
    assert rounds[2]["link"] == "stale" and rounds[2]["view"]["link_stale"] is True
    assert rounds[2]["view"]["time_frozen"] is True, "stale 期间时长必须冻结"
    assert rounds[3]["link"] == "disconnected"
    assert rounds[3]["view"]["link_disconnected"] is True
    assert rounds[4]["apply"] == "applied" and rounds[4]["seq"] == 1
    assert rounds[5]["link"] == "connected"
    assert rounds[5]["view"]["link_stale"] is False \
        and rounds[5]["view"]["link_disconnected"] is False \
        and rounds[5]["view"]["time_frozen"] is False, "恢复后陈旧标志必须复位"
    assert rounds[5]["view"]["activity"] == "L1-recovered", "恢复后显示新快照内容"
    assert pc.applied_pairs(report) == [("ep-L", 1), ("ep-L2", 1)]
    assert report["store_final"] == {"has_state": True, "epoch": "ep-L2", "seq": 1}
    assert report["ui_no_regress"], report["no_regress_notes"]
    print("[L1] PASS：§4 新鲜度全时序 connected→stale→disconnected→connected；"
          "重复不续鲜；恢复后标志复位")
