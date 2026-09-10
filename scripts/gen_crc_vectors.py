#!/usr/bin/env python3
"""gen_crc_vectors.py — 生成/复核 P0.5 冻结的 CRC32 测试向量（A4，2026-09-10）。

任务：P0.5（Transport）。真源：protocol/transport.md §4、docs/INTERFACES.md §7。

用途：
  1) 打印冻结向量（markdown 表 + C 常量两种形式），并写入 artifacts/crc_vectors.txt；
  2) `--verify`：重算全部向量并核对 protocol/transport.md 中写死的值一致，
     全部一致输出 PASS 并以 0 退出；任一不一致输出 FAIL 并以 1 退出。

冻结向量定义（payload 构造规则是契约的一部分，不得静默修改）：
  V1 empty_payload   : b""                                   （0 字节，下界）
  V2 short_json      : b'{"kind":"state","seq":1}'           （短 JSON，单片即可装下）
  V3 pattern_16k     : bytes(range(256)) * 64                （16384 字节，正好等于
                       INTERFACES §3/§7 的整包上限，上界）
  V4 fox_reference   : b"The quick brown fox jumps over the lazy dog"
                       （公开文献参照值，用于与外部 CRC32 实现交叉核对）

算法冻结：标准 IEEE CRC32（反射多项式 0xEDB88320，初值 0xFFFFFFFF，
结果异或 0xFFFFFFFF）——与 Python zlib.crc32 逐位一致。
本脚本自带一个逐位参考实现，与 zlib/binascii 交叉验证，证明"标准 IEEE"。

运行：系统 python3（≥3.9，仅标准库）或 uv run python 均可。
  python3 scripts/gen_crc_vectors.py            # 生成并打印
  python3 scripts/gen_crc_vectors.py --verify   # 对照 protocol/transport.md 复核
"""

import argparse
import binascii
import os
import sys
import zlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSPORT_MD = os.path.join(REPO_ROOT, "protocol", "transport.md")
ARTIFACTS_DIR = os.path.join(REPO_ROOT, "artifacts")

# --- 冻结 payload 构造（见模块 docstring） --------------------------------

SHORT_JSON = b'{"kind":"state","seq":1}'
PATTERN_16K = bytes(range(256)) * 64  # 16384 字节
FOX = b"The quick brown fox jumps over the lazy dog"

VECTORS = [
    ("V1", "empty_payload", b""),
    ("V2", "short_json", SHORT_JSON),
    ("V3", "pattern_16k", PATTERN_16K),
    ("V4", "fox_reference", FOX),
]


# --- 逐位 IEEE CRC32 参考实现（仅用于交叉验证 zlib） ----------------------

def ieee_crc32_bitwise(data: bytes) -> int:
    """标准 IEEE CRC32：多项式 0xEDB88320（反射），初值/终值异或 0xFFFFFFFF。"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def compute_all():
    """返回 [(vid, name, len, crc_hex_upper, crc_hex_lower), ...]，先做三实现交叉验证。"""
    rows = []
    for vid, name, payload in VECTORS:
        c_zlib = zlib.crc32(payload) & 0xFFFFFFFF
        c_binascii = binascii.crc32(payload) & 0xFFFFFFFF
        c_bitwise = ieee_crc32_bitwise(payload) & 0xFFFFFFFF
        if not (c_zlib == c_binascii == c_bitwise):
            print(f"FAIL: {vid} 三种实现不一致 zlib={c_zlib:#010x} "
                  f"binascii={c_binascii:#010x} bitwise={c_bitwise:#010x}", file=sys.stderr)
            sys.exit(2)
        rows.append((vid, name, len(payload), c_zlib))
    return rows


def format_markdown(rows) -> str:
    lines = [
        "| 向量 | 名称 | payload 长度（字节） | CRC32（hex） |",
        "|---|---|---:|---|",
    ]
    for vid, name, length, crc in rows:
        lines.append(f"| {vid} | {name} | {length} | 0x{crc:08X} |")
    return "\n".join(lines)


def format_c(rows) -> str:
    lines = ["/* 由 scripts/gen_crc_vectors.py 生成；与 protocol/transport.md §4 一致。 */"]
    for vid, name, _length, crc in rows:
        lines.append(f"#define CDT_CRC32_VEC_{vid.split('V')[1]}_{name.upper()} 0x{crc:08X}u /* {name} */")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verify", action="store_true",
                        help="重算向量并核对 protocol/transport.md 中写死的值")
    args = parser.parse_args()

    # 上界自检：V3 必须正好等于 16 KiB（INTERFACES §3 完整消息行）。
    if len(PATTERN_16K) != 16384:
        print("FAIL: pattern_16k 构造不再是 16384 字节", file=sys.stderr)
        return 2

    rows = compute_all()
    md = format_markdown(rows)
    c = format_c(rows)
    report = (
        "P0.5 CRC32 冻结向量（算法：IEEE CRC32 == zlib.crc32，三实现交叉验证通过）\n"
        f"生成时间：2026-09-10（冻结日期；重跑值不变）\n\n{md}\n\n{c}\n"
    )

    if args.verify:
        if not os.path.exists(TRANSPORT_MD):
            print(f"FAIL: 未找到 {TRANSPORT_MD}", file=sys.stderr)
            return 1
        with open(TRANSPORT_MD, "r", encoding="utf-8") as fh:
            doc = fh.read()
        missing = []
        for vid, name, length, crc in rows:
            token = f"0x{crc:08X}"
            if token not in doc:
                missing.append(f"{vid} {name}: transport.md 缺少 {token}")
            # 长度也必须写死且一致（对 V1/V2/V3 检查 payload 长度标注）
            if f"| {length} |" not in doc and vid != "V4":
                missing.append(f"{vid} {name}: transport.md 缺少长度标注 {length}")
        if missing:
            for m in missing:
                print("FAIL:", m, file=sys.stderr)
            return 1
        print("PASS: 4/4 个冻结向量与 protocol/transport.md 一致")
        print(report)
        return 0

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    out_path = os.path.join(ARTIFACTS_DIR, "crc_vectors.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report)
    print(f"已写入 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
