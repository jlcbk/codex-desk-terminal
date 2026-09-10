#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""p43_align.py — P4.3 帧哈希对齐校验（固件 vs 模拟器 vs golden 参考）。

对 firmware/main/main.c 的 k_scenes 同批场景：
  1. 从 tests/fixtures（与 gen_p43_scenes.py 同源同法）提取 AppState JSON 写盘；
  2. 逐场景跑模拟器 --state 路径（--fixed-clock 对齐固件 cdt_present 的固定时钟、
     --battery-mv 3900 对齐固件合成 Runtime、--page 对齐 selected_page），
     --capture-frame 抓 400x300 1bpp 逻辑帧 BMP（1=黑，行 4 字节对齐 52B）；
  3. 从 BMP 重打包 15000B cdt 逻辑帧（每行取前 50B、行序翻转），算 zlib.crc32
     （与板上 cdt_crc32 逐位一致）；
  4. 解析固件串口日志（scene=<name> crc32=0x...）逐场景比对，同场景跨循环
     CRC 必须稳定；
  5. 有 golden 参考帧的场景，解码 golden PNG 与模拟器帧逐像素比对（预期差异
     仅电压字段：golden 场景未注入电池显示 "--"，对齐路径合成 3900mV 显示
     3.90V），打印差异像素数供报告引用。

用法：
  python3 firmware/tools/p43_align.py --log artifacts/board/p43-serial.log \
      --out-dir artifacts/board/p43-align
退出码：0 全部对齐；1 固件≠模拟器或循环不稳定；2 用法/环境错误。
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import subprocess
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SIM = REPO / "build" / "simulator" / "codex-display-sim"
SCEN_DIR = REPO / "tests" / "fixtures" / "scenarios"
PROTO_DIR = REPO / "tests" / "fixtures" / "protocol"
GOLDEN_DIR = REPO / "tests" / "golden"

# 与 firmware/main/main.c k_scenes 严格同表（name/page/now_ms/golden）
SCENES = [
    ("F01_idle",       ("S01_idle.jsonl", "idle"),     "now",   1,
     "S01_idle__f001_idle.png"),
    ("F03_working",    ("S03_working.jsonl", "working"), "now", 1,
     "S03_working__f001_working.png"),
    ("F05_needs_you",  ("S05_needs_you.jsonl", "needs_you"), "now", 1,
     "S05_needs_you__f001_needs_you.png"),
    ("F19_usage_0",    ("S19_usage_0.jsonl", "online"), "usage", 3000,
     "S19_usage_0__f004_usage_page.png"),
    ("F20_usage_100",  ("S20_usage_100.jsonl", "online"), "usage", 3000,
     "S20_usage_100__f004_usage_page.png"),
    ("F06_valid_full", None, "now", 1, None),  # valid_full.json（协议 fixture）
]


def compact(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def scene_json(spec) -> str:
    if spec is None:
        return compact(json.loads(
            (PROTO_DIR / "valid_full.json").read_text(encoding="utf-8")))
    fname, tag = spec
    for line in (SCEN_DIR / fname).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        obj = json.loads(line)
        if obj.get("tag") == tag and obj.get("action") == "app_state":
            return compact(obj["payload"])
    raise SystemExit(f"[align] {fname}: tag={tag} app_state 未找到")


def bmp_to_frame(path: Path) -> bytes:
    """1bpp BMP（write_frame_bmp 布局：行 52B、自底向上、palette 0=白 1=黑）
    → 15000B cdt 逻辑帧（行 50B、自顶向下、MSB=左、1=黑）。"""
    data = path.read_bytes()
    off, hdr = struct.unpack_from("<I", data, 10)[0], data[:54]
    w, h = struct.unpack_from("<ii", hdr, 18)
    bpp = struct.unpack_from("<H", hdr, 28)[0]
    if (w, h, bpp) != (400, 300, 1):
        raise SystemExit(f"[align] {path}: 非预期 BMP 规格 {w}x{h}@{bpp}")
    row_b, out = 52, bytearray()
    for y in range(299, -1, -1):  # 自底向上存储 → 翻回自顶向下
        base = off + y * row_b
        out += data[base:base + 50]
    return bytes(out)


def png_to_frame(path: Path) -> bytes:
    """golden PNG（400x300 8bit 灰度、filter 0、stored deflate）→ 15000B 帧。"""
    data = path.read_bytes()
    pos, idat = 8, b""
    while pos < len(data):
        (length,), typ = struct.unpack_from(">I", data, pos), data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if typ == b"IHDR":
            w, h, depth, ctype = struct.unpack_from(">IIBB", body)
            if (w, h, depth, ctype) != (400, 300, 8, 0):
                raise SystemExit(f"[align] {path}: 非预期 PNG 规格")
        elif typ == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    if len(raw) != (1 + 400) * 300:
        raise SystemExit(f"[align] {path}: 解压长度 {len(raw)} 非预期")
    out = bytearray(15000)
    for y in range(300):
        row = raw[y * 401:(y + 1) * 401]
        if row[0] != 0:
            raise SystemExit(f"[align] {path}: row {y} filter={row[0]} 非预期")
        for x in range(400):
            if row[1 + x] < 128:  # 黑=0 → cdt bit 1
                out[y * 50 + (x >> 3)] |= 0x80 >> (x & 7)
    return bytes(out)


def crc(frame: bytes) -> int:
    return zlib.crc32(frame) & 0xFFFFFFFF


def diff_pixels(a: bytes, b: bytes) -> int:
    n = 0
    for i in range(15000):
        n += bin(a[i] ^ b[i]).count("1")
    return n


def run_sim(name: str, jsontext: str, page: str, clock: int, out: Path,
            battery_mv: int = 3900, suffix: str = "") -> bytes:
    state = out / f"{name}.json"
    bmp = out / f"{name}{suffix}.bmp"
    state.write_text(jsontext, encoding="utf-8")
    cmd = [str(SIM), "--state", str(state), "--page", page,
           "--battery-mv", str(battery_mv), "--fixed-clock", str(clock),
           "--capture-frame", str(bmp), "--quit-after-ms", "600"]
    env = {"SDL_VIDEODRIVER": "dummy", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(Path.home())}
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=str(REPO), env=env, timeout=120)
    if proc.returncode != 0 or not bmp.is_file():
        print(proc.stdout[-1500:], proc.stderr[-1500:], file=sys.stderr)
        raise SystemExit(f"[align] 模拟器失败：{name}（exit {proc.returncode}）")
    return bmp_to_frame(bmp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="固件串口日志（p43-*.log）")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    if not SIM.is_file():
        raise SystemExit("[align] 模拟器不存在，先 sh scripts/build_simulator.sh")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 固件 CRC：同场景多循环必须稳定
    fw_crcs: dict[str, set[str]] = {}
    pat = re.compile(r"scene=(\S+) crc32=0x([0-9a-fA-F]{8})")
    for line in Path(args.log).read_text(encoding="utf-8", errors="replace").splitlines():
        m = pat.search(line)
        if m:
            fw_crcs.setdefault(m.group(1), set()).add(m.group(2).lower())

    print(f"{'scene':<15} {'firmware':<12} {'simulator':<12} {'match':<6} "
          f"{'golden ref':<38} {'ref_crc':<10} {'golden_crc':<11} {'ref==golden'}")
    ok = True
    for name, spec, page, clock, golden in SCENES:
        frame = run_sim(name, scene_json(spec), page, clock, out)
        sim_crc = f"{crc(frame):08x}"
        fw = fw_crcs.get(name, set())
        if len(fw) != 1:
            print(f"[align] {name}: 固件 CRC 缺失或不稳定（{sorted(fw)}）",
                  file=sys.stderr)
            ok = False
        fw_crc = next(iter(fw)) if fw else "--------"
        match = "OK" if fw_crc == sim_crc else "FAIL"
        if match == "FAIL":
            ok = False

        # golden 参考列：把对齐路径唯一的场景注入差异（电压 3900mV vs golden
        # 场景未注入电池的 "--"）中和后（battery-mv 0 → 电压 "--"）再跑一帧，
        # 与 golden PNG 逐位比对——证明渲染路径与 golden 全等（P2 冻结基线）。
        gcol, ref_crc, gcrc, gr = "-", "-", "-", "-"
        if golden:
            gpath = GOLDEN_DIR / golden
            if gpath.is_file():
                gframe = png_to_frame(gpath)
                gcrc = f"{crc(gframe):08x}"
                refframe = run_sim(name, scene_json(spec), page, clock, out,
                                   battery_mv=0, suffix="_goldref")
                ref_crc = f"{crc(refframe):08x}"
                dpx = diff_pixels(gframe, refframe)
                gr = "OK" if (ref_crc == gcrc and dpx == 0) else f"FAIL({dpx}px)"
                gcol = golden
            else:
                gcol = f"{golden} (missing)"

        print(f"{name:<15} 0x{fw_crc:<10} 0x{sim_crc:<11} {match:<6} {gcol:<38} "
              f"{ref_crc:<10} {gcrc:<11} {gr}")

    print("[align] RESULT:", "PASS（固件=模拟器 全场景一致）" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
