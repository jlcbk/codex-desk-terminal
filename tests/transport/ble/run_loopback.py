#!/usr/bin/env python3
"""run_loopback.py — P3.4（A4-B）BLE 分片/重组 loopback 互验驱动（host，无真机）。

链路（transport.md §7.0 纯逻辑内核 + §7.2 B1 进程内虚拟链路的 host 部分）：
  ① Python Fragmenter（bridge/transports/ble/fragmenter.py，发送端）
     → 长度前缀帧文件 → C test_transport --loopback（接收端重组器）
     → C 内 memcmp 逐字节比对 → StateStore applied/duplicate → ACK/NACK 决策
     → 重组字节落盘 → 本脚本再做一次独立 `cmp`（双重证据）。
  ② 反向互验：C cdt_fragmenter --emit-fragments → Python Reassembler
     → 与原 JSON 逐字节比对。

消息源：tests/fixtures/protocol/valid_full.json、valid_max_sizes.json（13KiB
大包）、bridge mock lifecycle 产出快照（bridge 实际编码器字节）。MTU 矩阵
23/53/247（transport.md §7.2 B5 的两档 + 中档）。

退出码 0 = 全部通过；1 = 任一失败。证据写入 artifacts/transport/。
Transport 不解释业务 JSON：store 调用只发生在 C 测试代码（tests/transport/ble）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, REPO)

from bridge.transports.ble.fragmenter import (Fragmenter, Reassembler,  # noqa: E402
                                              read_frames_file,
                                              write_frames_file)

MTUS = (23, 53, 247)


def log(lines, text):
    print(text)
    lines.append(text)


def collect_messages(lines):
    """[(name, bytes)]：两个协议 fixture + bridge mock 产出的最大快照。"""
    msgs = []
    for name in ("valid_full.json", "valid_max_sizes.json"):
        path = os.path.join(REPO, "tests", "fixtures", "protocol", name)
        with open(path, "rb") as fh:
            data = fh.read()
        msgs.append((os.path.splitext(name)[0], data))
    proc = subprocess.run(
        [sys.executable, "-m", "bridge", "--source", "mock",
         "--scenario", "lifecycle"],
        cwd=REPO, capture_output=True, check=True)
    snapshots = [l.strip().encode("utf-8")
                 for l in proc.stdout.decode("utf-8").splitlines() if l.strip()]
    biggest = max(snapshots, key=len)
    msgs.append(("mock_lifecycle", biggest))
    log(lines, "消息源：%s" % ", ".join("%s(%dB)" % (n, len(b)) for n, b in msgs))
    return msgs


def run_py_to_c(binary, name, payload, mtu, lines, art_dir, failures):
    """① Python 分片 → C 重组 + store；C 退出码与独立 cmp 双重判定。"""
    frag_path = os.path.join(art_dir, "py2c_%s_mtu%d.frag" % (name, mtu))
    out_path = os.path.join(art_dir, "py2c_%s_mtu%d.reassembled.json" % (name, mtu))
    orig_path = os.path.join(art_dir, "%s.json" % name)
    with open(orig_path, "wb") as fh:
        fh.write(payload)

    fg = Fragmenter(payload, message_id=1, mtu=mtu)
    n = write_frames_file(frag_path, fg.frames())
    log(lines, "  [py->c] %s mtu=%d: %dB -> %d frames (chunk=%d)"
        % (name, mtu, len(payload), n, fg.chunk))

    proc = subprocess.run(
        [binary, "--loopback", frag_path, orig_path, out_path, str(mtu)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        failures.append("C --loopback 退出码 %d（%s mtu=%d）"
                        % (proc.returncode, name, mtu))
        log(lines, "  [py->c] FAIL: C exit=%d\n%s%s"
            % (proc.returncode, proc.stdout, proc.stderr))
        return
    if not os.path.exists(out_path):
        failures.append("C 未写出重组文件（%s mtu=%d）" % (name, mtu))
        return
    with open(out_path, "rb") as fh:
        reassembled = fh.read()
    if reassembled != payload:
        failures.append("shell cmp 不一致（%s mtu=%d）" % (name, mtu))
        log(lines, "  [py->c] FAIL: cmp 不一致")
    else:
        log(lines, "  [py->c] PASS: C 内 memcmp 一致 + 独立 cmp 一致 + store applied/duplicate")


def run_c_to_py(binary, name, payload, mtu, lines, art_dir, failures):
    """② C 分片 → Python 重组 → 逐字节比对。"""
    cfrag_path = os.path.join(art_dir, "c2py_%s_mtu%d.frag" % (name, mtu))
    proc = subprocess.run(
        [binary, "--emit-fragments", os.path.join(art_dir, "%s.json" % name),
         str(mtu), "9", cfrag_path],
        capture_output=True, text=True)
    if proc.returncode != 0:
        failures.append("C --emit-fragments 退出码 %d（%s mtu=%d）"
                        % (proc.returncode, name, mtu))
        log(lines, "  [c->py] FAIL: %s%s" % (proc.stdout, proc.stderr))
        return
    rs = Reassembler(mtu)
    frames = list(read_frames_file(cfrag_path))
    result = "ok"
    for i, frame in enumerate(frames):
        if rs.feed(frame, i * 5) == "completed":
            result = "completed"
            break
    if result != "completed" or rs.take() != payload:
        failures.append("Python 重组与原 JSON 不一致（%s mtu=%d）" % (name, mtu))
        log(lines, "  [c->py] FAIL: %d frames" % len(frames))
    else:
        log(lines, "  [c->py] PASS: %d frames -> %dB 逐字节一致"
            % (len(frames), len(rs.take())))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bin", default=os.path.join(REPO, "build", "transport",
                                                  "test_transport"),
                    help="C 测试二进制路径（先由 scripts/build_transport_tests.sh 构建）")
    ap.add_argument("--artifacts", default=os.path.join(REPO, "artifacts",
                                                        "transport"))
    args = ap.parse_args(argv)

    if not os.path.isfile(args.bin):
        print("FAIL: C 测试二进制不存在：%s（先跑 scripts/build_transport_tests.sh）"
              % args.bin, file=sys.stderr)
        return 2

    art_dir = os.path.join(args.artifacts, "loopback")
    os.makedirs(art_dir, exist_ok=True)
    report_path = os.path.join(args.artifacts, "loopback_report.txt")
    lines = []
    failures = []

    log(lines, "== P3.4 BLE 分片/重组 loopback 互验（host；真机 GATT 未验证） ==")
    msgs = collect_messages(lines)
    for name, payload in msgs:
        for mtu in MTUS:
            run_py_to_c(args.bin, name, payload, mtu, lines, art_dir, failures)
            run_c_to_py(args.bin, name, payload, mtu, lines, art_dir, failures)

    log(lines, "== loopback 结论：%s ==" % ("全部通过" if not failures else "失败"))
    for f in failures:
        log(lines, "FAIL: %s" % f)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("报告：%s" % report_path)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
