#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_ui.py — UI golden/actual 像素回归比较（P2.4 工具部分，A5）。

契约真源：tests/UI_CONTRACT.md §2（CLI、帧配对、输出、退出码）、tests/SCENARIOS.md §2/§5、
docs/DEVELOPMENT_PLAN.md §8。帧格式：shared/display/cdt_frame.h —— 400×300、1bpp、
每行 50 字节、行优先、字节内 MSB=左像素、1=黑/0=白，共 15000 字节。

用法：
    python scripts/check_ui.py --golden tests/golden --actual artifacts/ui [--scenario S01]
    python scripts/check_ui.py --self-test

输入图像：P4 PBM（1=黑，与逻辑帧同 packing）与 PNG（灰度 1/8bit，按「非白=黑」二值化）。
尺寸必须恰为 400×300，否则该帧 FAIL（防布局漂移）。

退出码（契约 §2.4）：0=全部场景 PASS；1=存在像素差异/缺帧/多余帧/尺寸不符等测试失败；
2=环境或用法错误（golden 目录不存在、文件不可读等），不得用于掩盖回归。

红线（契约 §2.6）：对 --golden 目录永远只读；不提供 --update-golden / --accept 类参数；
禁止自动接受或重生成 golden。

语义断言表（tests/SCENARIOS.md §3 逐场景断言要点）按计划在 P2 集成时随 SCENARIOS 同步接入；
本版本仅做像素比较与帧完整性检查，输出行中「语义=未实现(P2)」如实标注。
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 10):  # 项目锁定 Python 3.12，经 uv 运行
    sys.stderr.write(
        "环境错误: check_ui.py 需要 Python >= 3.10（项目锁定 3.12），请经 uv 运行：\n"
        "  uv run --python 3.12 python scripts/check_ui.py ...\n"
    )
    sys.exit(2)

import argparse
import dataclasses
import json
import os
import re
import shutil
import zlib
from dataclasses import dataclass
from pathlib import Path

# ---- 逻辑帧几何（shared/display/cdt_frame.h）----
WIDTH = 400
HEIGHT = 300
STRIDE = 50            # (WIDTH+7)/8
NBYTES = STRIDE * HEIGHT  # 15000

IMG_EXTS = {".png", ".pbm"}
EXP_SUFFIX = "__expected.png"   # expected（golden 重编码）落 actual 目录侧（SCENARIOS §5.8）
DIFF_SUFFIX = "__diff.png"      # 契约 §2.3：diff 图写到 actual 同目录
MANIFEST_NAME = "manifest.jsonl"
SKIP_DIR_NAMES = {"selftest"}   # 契约 §2.5 自检证据目录，避免污染真实比较

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_FRAME_RE = re.compile(r"^(?P<scen>.+?)__f(?P<idx>\d{3,})_(?P<tag>[0-9A-Za-z_-]+)$")

# 1 字节 → 8 个灰度输出字节（MSB=左像素），黑=0xFF/白=0x00
_EXP = tuple(
    bytes(0xFF if (i >> (7 - j)) & 1 else 0x00 for j in range(8)) for i in range(256)
)


class EnvErr(Exception):
    """环境错误 → 退出码 2（契约 §2.4）。"""


class UnsupportedPng(Exception):
    """内部信号：stdlib PNG 读取器不支持的变体，转 Pillow。"""


@dataclass
class Frame:
    w: int
    h: int
    raster: bytes  # 400×300 时为 15000 字节逻辑帧（1=黑）；其他尺寸内容不保证


@dataclass(frozen=True)
class FrameRef:
    rel: str      # 相对扫描根的 posix 路径
    path: Path
    stem: str
    scen: str     # 场景名（__f 前部分）
    idx: int | None  # 帧序号；单帧场景为 None


@dataclass
class ScenResult:
    name: str
    passed: bool
    lines: list


# ---------------------------------------------------------------- 位操作（与 cdt_frame.c 逐位一致）

def set_px(frame: bytearray, x: int, y: int, black: int) -> None:
    """等价 shared/display/cdt_frame.c cdt_frame_set：非 0=黑。越界忽略。"""
    if x < 0 or x >= WIDTH or y < 0 or y >= HEIGHT:
        return
    i = y * STRIDE + (x >> 3)
    bit = 0x80 >> (x & 7)  # MSB = 最左像素
    if black:
        frame[i] |= bit
    else:
        frame[i] &= ~bit & 0xFF


def get_px(frame: bytes | bytearray, x: int, y: int) -> int:
    """等价 cdt_frame_get：返回 0/1，越界 -1。"""
    if x < 0 or x >= WIDTH or y < 0 or y >= HEIGHT:
        return -1
    return (frame[y * STRIDE + (x >> 3)] >> (7 - (x & 7))) & 1


# ---------------------------------------------------------------- 解码：P4 PBM（自实现）

def parse_pbm(data: bytes, src: str) -> Frame:
    if data[:2] != b"P4":
        raise EnvErr(f"{src}: 不是 P4（二进制）PBM")
    n = len(data)
    i = 2
    fields: list[int] = []
    while len(fields) < 2:
        while i < n and data[i] in b" \t\r\n":
            i += 1
        if i < n and data[i : i + 1] == b"#":  # 头部注释
            while i < n and data[i] not in b"\r\n":
                i += 1
            continue
        if i >= n:
            raise EnvErr(f"{src}: PBM 头部不完整")
        j = i
        while j < n and data[j] not in b" \t\r\n":
            j += 1
        try:
            fields.append(int(data[i:j]))
        except ValueError:
            raise EnvErr(f"{src}: PBM 头部含非法 token")
        i = j
    i += 1  # 高度 token 后恰一个空白字节，随后即位图
    w, h = fields
    if w <= 0 or h <= 0:
        raise EnvErr(f"{src}: PBM 尺寸非法 {w}x{h}")
    rowbytes = (w + 7) // 8
    need = rowbytes * h
    raster = data[i : i + need]
    if len(raster) < need:
        raise EnvErr(f"{src}: PBM 位图数据截断（需 {need} 字节，得 {len(raster)}）")
    return Frame(w, h, bytes(raster))


# ---------------------------------------------------------------- 解码：PNG（stdlib 灰度实现 + Pillow 兜底）

def gray8_to_canonical(data: bytes) -> bytes:
    """400×300 灰度 8bit（行优先）→ 逻辑帧：「非白(≠255)=黑」二值化，1=黑。"""
    out = bytearray(NBYTES)
    ci = 0
    for y in range(HEIGHT):
        row = data[y * WIDTH : (y + 1) * WIDTH]
        acc = 0
        for b in row:
            acc = (acc << 1) | (1 if b != 255 else 0)
        out[ci : ci + STRIDE] = acc.to_bytes(STRIDE, "big")
        ci += STRIDE
    return bytes(out)


def parse_png_stdlib(data: bytes, src: str) -> Frame:
    """最小 PNG 读取器：灰度（color type 0）1/2/4/8bit、非隔行。其余变体抛 UnsupportedPng。"""
    if data[:8] != _PNG_SIG:
        raise EnvErr(f"{src}: PNG 签名不符")
    pos = 8
    n = len(data)
    ihdr: bytes | None = None
    idat = bytearray()
    while pos + 8 <= n:
        ln = int.from_bytes(data[pos : pos + 4], "big")
        typ = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + ln]
        if len(body) < ln:
            raise EnvErr(f"{src}: PNG chunk 截断")
        if typ == b"IHDR" and ihdr is None:
            if ln != 13:
                raise EnvErr(f"{src}: IHDR 长度非法")
            ihdr = body
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + ln
    if ihdr is None:
        raise EnvErr(f"{src}: PNG 缺 IHDR")
    w = int.from_bytes(ihdr[0:4], "big")
    h = int.from_bytes(ihdr[4:8], "big")
    depth, color, comp, filt, inter = ihdr[8], ihdr[9], ihdr[10], ihdr[11], ihdr[12]
    if w <= 0 or h <= 0:
        raise EnvErr(f"{src}: PNG 尺寸非法 {w}x{h}")
    if comp != 0 or filt != 0:
        raise EnvErr(f"{src}: 不支持的 PNG 压缩/滤波方式")
    if inter != 0 or color != 0 or depth not in (1, 2, 4, 8):
        raise UnsupportedPng()  # 交 Pillow 兜底
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise EnvErr(f"{src}: PNG 像素数据损坏（{e}）")
    rowbytes = (w * depth + 7) // 8
    if len(raw) < (rowbytes + 1) * h:
        raise EnvErr(f"{src}: PNG 像素数据不足")
    # 逐行去滤波（灰度所有位深 bpp=1）
    rows: list[bytes] = []
    prev = bytes(rowbytes)
    for y in range(h):
        off = y * (rowbytes + 1)
        ft = raw[off]
        row = bytearray(raw[off + 1 : off + 1 + rowbytes])
        if ft == 1:  # Sub
            for i in range(1, rowbytes):
                row[i] = (row[i] + row[i - 1]) & 0xFF
        elif ft == 2:  # Up
            for i in range(rowbytes):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ft == 3:  # Average
            for i in range(rowbytes):
                a = row[i - 1] if i else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:  # Paeth
            for i in range(rowbytes):
                a = row[i - 1] if i else 0
                b = prev[i]
                c = prev[i - 1] if i else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 0xFF
        elif ft != 0:
            raise EnvErr(f"{src}: 未知 PNG 行滤波类型 {ft}")
        rows.append(bytes(row))
        prev = row
    # 展开样本并按「非白=黑」二值化为逻辑帧（PNG 灰度 0=黑 → 逻辑 1=黑）
    canon = bytearray(NBYTES) if (w, h) == (WIDTH, HEIGHT) else None
    ci = 0
    if depth == 8:
        for row in rows:
            if canon is None:
                break
            acc = 0
            for b in row:
                acc = (acc << 1) | (1 if b != 255 else 0)
            canon[ci : ci + STRIDE] = acc.to_bytes(STRIDE, "big")
            ci += STRIDE
    else:
        maxv = (1 << depth) - 1
        per_byte = 8 // depth
        for row in rows:
            if canon is None:
                break
            acc = 0
            for byte in row:
                for k in range(per_byte):
                    sample = (byte >> (8 - depth * (k + 1))) & maxv
                    acc = (acc << 1) | (1 if sample != maxv else 0)
            canon[ci : ci + STRIDE] = acc.to_bytes(STRIDE, "big")
            ci += STRIDE
    return Frame(w, h, bytes(canon) if canon is not None else b"")


def load_frame_pillow(path: Path) -> Frame:
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise EnvErr(
            f"{path}: 该 PNG 变体（隔行/非灰度/16bit）需要 Pillow 解码：\n"
            "  uv run --with pillow python scripts/check_ui.py ..."
        )
    try:
        with Image.open(path) as im:
            w, h = im.size
            data = im.convert("L").tobytes()
    except Exception as e:  # noqa: BLE001 — 任何 Pillow 失败都归为环境错误
        raise EnvErr(f"{path}: Pillow 解码失败（{e}）")
    if (w, h) == (WIDTH, HEIGHT):
        return Frame(w, h, gray8_to_canonical(data))
    return Frame(w, h, b"")


def load_frame(path: Path) -> Frame:
    try:
        data = path.read_bytes()
    except OSError as e:
        raise EnvErr(f"{path}: 文件不可读（{e}）")
    if data[:8] == _PNG_SIG:
        try:
            return parse_png_stdlib(data, str(path))
        except UnsupportedPng:
            return load_frame_pillow(path)
    if data[:2] == b"P4":
        return parse_pbm(data, str(path))
    raise EnvErr(f"{path}: 不支持的图像格式（仅支持 P4 PBM / PNG）")


# ---------------------------------------------------------------- 编码：PNG / PBM（输出三图用）

def _png_chunk(typ: bytes, data: bytes) -> bytes:
    return (
        len(data).to_bytes(4, "big")
        + typ
        + data
        + (zlib.crc32(typ + data) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def canonical_to_gray_rows(raster: bytes) -> list:
    """逻辑帧 → 每行 400 字节灰度（黑=0x00/白=0xFF），供 PNG 输出。"""
    rows = []
    for off in range(0, NBYTES, STRIDE):
        v = int.from_bytes(raster[off : off + STRIDE], "big")
        acc = bytearray()
        for sh in range(STRIDE - 1, -1, -1):
            acc += _EXP[(v >> (8 * sh)) & 0xFF]
        rows.append(bytes(acc))
    return rows


def write_png(path, w: int, h: int, gray_rows) -> None:
    ihdr = w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([8, 0, 0, 0, 0])
    raw = bytearray()
    for row in gray_rows:
        raw += b"\x00" + bytes(row)
    body = (
        _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )
    Path(path).write_bytes(_PNG_SIG + body)


def write_pbm(path, raster: bytes, w: int = WIDTH, h: int = HEIGHT) -> None:
    Path(path).write_bytes(f"P4\n{w} {h}\n".encode("ascii") + raster)


# ---------------------------------------------------------------- 比较

def compare_rasters(a: bytes, b: bytes):
    """逐像素 XOR（契约 §2.3）。返回（每行 400bit 异或整数列表, 差异像素总数）。"""
    total = 0
    rows = []
    for off in range(0, NBYTES, STRIDE):
        x = int.from_bytes(a[off : off + STRIDE], "big") ^ int.from_bytes(
            b[off : off + STRIDE], "big"
        )
        total += x.bit_count()
        rows.append(x)
    return rows, total


def write_pair_outputs(actual_path: Path, golden_raster: bytes, xor_rows: list) -> None:
    """expected/diff 两图写 actual 目录侧（actual 本身已在盘上）；黑=差异、白=一致。"""
    stem = actual_path.stem
    d = actual_path.parent
    xor_raster = b"".join(x.to_bytes(STRIDE, "big") for x in xor_rows)
    write_png(d / (stem + EXP_SUFFIX), WIDTH, HEIGHT, canonical_to_gray_rows(golden_raster))
    write_png(d / (stem + DIFF_SUFFIX), WIDTH, HEIGHT, canonical_to_gray_rows(xor_raster))


# ---------------------------------------------------------------- 扫描、配对、manifest

def parse_frame_stem(stem: str):
    m = _FRAME_RE.match(stem)
    if m:
        return m.group("scen"), int(m.group("idx"))
    return stem, None


def frame_label(ref: FrameRef) -> str:
    """FAIL 行的 帧= 标签：多帧取 __f 后缀（契约示例 帧=__f004_forced_page），单帧取全名。"""
    if ref.idx is None or "__" not in ref.stem:
        return ref.stem
    return "__" + ref.stem.split("__", 1)[1]


def scan_frames(root: Path):
    out: dict[str, FrameRef] = {}
    if not root.is_dir():
        raise EnvErr(f"目录不存在：{root}")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")
        )
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            if p.suffix.lower() not in IMG_EXTS:
                continue
            stem = p.stem
            low = stem.lower()
            if low.endswith("__diff") or low.endswith("__expected"):
                continue  # 本工具自身输出，不作为帧参与配对
            scen, idx = parse_frame_stem(stem)
            out[p.relative_to(root).as_posix()] = FrameRef(
                p.relative_to(root).as_posix(), p, stem, scen, idx
            )
    return out


def group_by_scenario(scan, side: str, strict: bool):
    grouped: dict = {}
    dups: list = []
    for rel in sorted(scan):
        ref = scan[rel]
        m = grouped.setdefault(ref.scen, {})
        if ref.idx in m:
            if strict:
                raise EnvErr(
                    f"{side} 场景 {ref.scen} 帧序号冲突：{m[ref.idx].path} 与 {ref.path}"
                )
            dups.append(ref)
            continue
        m[ref.idx] = ref
    return grouped, dups


def scenario_selected(name: str, flt: str | None) -> bool:
    """--scenario 匹配：S 编号（S04）或全名（S04_plan_update）。"""
    if not flt:
        return True
    return name == flt or name.startswith(flt + "_")


def collect_manifest(actual_root: Path, scenario: str | None):
    """消费 actual 目录内的 manifest.jsonl（契约 §3.4/§2.2）。

    返回 (issues, idx_override, declared)：
      issues       [(scenario, 帧标签, 原因)] —— 声明帧缺失/声明与实际不符（§4.2：FAIL）
      idx_override relpath → frame_index（帧名无 __f 时以 manifest 为准）
      declared     scenario → set(实际存在的帧 relpath)
    """
    issues: list = []
    idx_override: dict = {}
    declared: dict = {}
    for mf in sorted(actual_root.rglob(MANIFEST_NAME)):
        rel_parts = mf.relative_to(actual_root).parts[:-1]
        if any(pp in SKIP_DIR_NAMES or pp.startswith(".") for pp in rel_parts):
            continue
        text = mf.read_text(encoding="utf-8", errors="replace")
        for lineno, raw in enumerate(text.splitlines(), 1):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as e:
                raise EnvErr(f"{mf}:{lineno}: manifest 行不是合法 JSON（{e}）")
            if not isinstance(obj, dict):
                raise EnvErr(f"{mf}:{lineno}: manifest 行必须是 JSON 对象")
            frame = obj.get("frame")
            if not frame or not isinstance(frame, str):
                continue  # 无 ViewModel 变化的 action 也记入 manifest（§3.3），无帧文件
            scen = obj.get("scenario") or parse_frame_stem(Path(frame).stem)[0] or "?"
            cand = mf.parent / frame
            path = cand if cand.is_file() else (actual_root / frame)
            if not path.is_file():
                issues.append((scen, Path(frame).name, "manifest声明帧缺失(missing)"))
                continue
            try:
                rel = path.resolve().relative_to(actual_root.resolve()).as_posix()
            except ValueError:
                rel = None
            stem_idx = parse_frame_stem(Path(frame).stem)[1]
            mi = obj.get("frame_index")
            if isinstance(mi, int) and not isinstance(mi, bool):
                if stem_idx is not None and stem_idx != mi:
                    issues.append(
                        (
                            scen,
                            Path(frame).stem,
                            f"manifest frame_index={mi} 与文件名 f{stem_idx:03d} 不符",
                        )
                    )
                elif rel is not None:
                    idx_override[rel] = mi
            if rel is not None:
                declared.setdefault(scen, set()).add(rel)
    return issues, idx_override, declared


def compare_pair(gref: FrameRef, aref: FrameRef):
    """比较一对帧。返回 (差异像素数或 None, 失败原因或 None)。"""
    gf = load_frame(gref.path)
    af = load_frame(aref.path)
    if (gf.w, gf.h) != (WIDTH, HEIGHT) or (af.w, af.h) != (WIDTH, HEIGHT):
        return None, f"尺寸不符(golden {gf.w}x{gf.h} / actual {af.w}x{af.h})"
    xor_rows, total = compare_rasters(gf.raster, af.raster)
    write_pair_outputs(aref.path, gf.raster, xor_rows)
    if total:
        return total, "像素差异"
    return 0, None


def compare_dirs(golden_dir, actual_dir, scenario: str | None = None):
    """主流程：返回 ScenResult 列表（打印交给调用方）。环境问题抛 EnvErr。"""
    if scenario:
        scenario = scenario.strip()
    golden_dir, actual_dir = Path(golden_dir), Path(actual_dir)
    gscan = scan_frames(golden_dir)
    ascan = scan_frames(actual_dir)
    if not gscan:
        raise EnvErr(f"golden 目录没有任何帧图（{golden_dir}）")
    m_issues, idx_override, declared = collect_manifest(actual_dir, scenario)
    # 契约 §2.2：golden f<NNN> ↔ manifest frame_index。仅在 golden 侧为多帧序号
    # 且按文件名找不到 actual 时兜底使用；不改动单帧场景（golden 无 __f）的配对。
    rel_by_midx = {mi: rel for rel, mi in idx_override.items()}
    gby, _ = group_by_scenario(gscan, "golden", strict=True)
    aby, dups = group_by_scenario(ascan, "actual", strict=False)

    issues_by_scen: dict = {}
    for scen, label, reason in m_issues:
        issues_by_scen.setdefault(scen, []).append((label, reason))
    for ref in dups:
        issues_by_scen.setdefault(ref.scen, []).append(
            (ref.stem, "actual同帧序号重复(dup-frame)")
        )
    # manifest 覆盖的场景：actual 帧必须全部在 manifest 声明（§4.2 声明与实际不符 = FAIL）
    for scen, rels in declared.items():
        if not scenario_selected(scen, scenario):
            continue
        for rel, ref in sorted(ascan.items()):
            if ref.scen == scen and rel not in rels:
                issues_by_scen.setdefault(scen, []).append(
                    (ref.stem, "帧未在manifest声明(unmanifested-frame)")
                )

    selected = sorted(
        s for s in set(gby) | set(aby) if scenario_selected(s, scenario)
    )
    if not selected:
        raise EnvErr(f"--scenario {scenario!r} 未匹配任何场景")

    results: list = []
    for scen in selected:
        gframes = gby.get(scen, {})
        aframes = aby.get(scen, {})
        n_golden = len(gframes)
        n_actual = sum(1 for ref in ascan.values() if ref.scen == scen)
        n = n_golden if n_golden else n_actual
        lines: list = []
        ok = True
        consumed: set = set()
        if gframes:
            for idx in sorted(gframes, key=lambda v: (v is None, v or 0)):
                gref = gframes[idx]
                aref = aframes.get(idx)
                if aref is None and idx is not None:
                    alt_rel = rel_by_midx.get(idx)
                    if alt_rel is not None and ascan[alt_rel].scen == scen:
                        aref = ascan[alt_rel]
                if aref is None:
                    lines.append(
                        _fail_line(scen, n, "n/a", gref.stem, "actual缺帧(missing)")
                    )
                    ok = False
                    continue
                consumed.add(aref.rel)
                diff, reason = compare_pair(gref, aref)
                if reason:
                    lines.append(
                        _fail_line(
                            scen,
                            n,
                            "n/a" if diff is None else str(diff),
                            frame_label(aref),
                            reason,
                        )
                    )
                    ok = False
        # 无 golden 对应的 actual 帧 → unapproved-frame FAIL（契约 §2.4）
        for rel, ref in sorted(ascan.items()):
            if ref.scen == scen and rel not in consumed:
                lines.append(
                    _fail_line(
                        scen, n, "n/a", frame_label(ref), "多余帧无golden(unapproved-frame)"
                    )
                )
                ok = False
        for label, reason in issues_by_scen.get(scen, []):
            lines.append(_fail_line(scen, n, "n/a", label, reason))
            ok = False
        if ok:
            results.append(ScenResult(scen, True, [_pass_line(scen, n)]))
        else:
            results.append(ScenResult(scen, False, lines))
    return results


# ---------------------------------------------------------------- 输出（契约 §2.3 stdout 格式）

def _pass_line(scen: str, n: int) -> str:
    return f"[PASS] {scen:<20} 帧数={n} 差异像素=0 语义=未实现(P2)"


def _fail_line(scen: str, n, diff: str, label: str, reason: str) -> str:
    return (
        f"[FAIL] {scen:<20} 帧数={n} 差异像素={diff} 帧={label} "
        f"原因={reason} 语义=未实现(P2)"
    )


def render_results(results):
    lines: list = []
    for r in results:
        lines.extend(r.lines)
    passed = sum(1 for r in results if r.passed)
    rc = 0 if passed == len(results) else 1
    lines.append(f"汇总: {passed}/{len(results)} PASS")
    lines.append(f"退出码: {rc}")
    return lines, rc


# ---------------------------------------------------------------- --self-test（契约 §2.5）

def _selftest_frames():
    """合成确定性 golden 帧（与 shared/display/cdt_frame 逐位一致的字节构造）。"""
    frames = []
    f1 = bytearray(NBYTES)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            border = x < 3 or x >= WIDTH - 3 or y < 3 or y >= HEIGHT - 3
            checker = 20 <= x < 380 and 40 <= y < 240 and ((x + y) & 1) == 0
            bar = 20 <= x < 220 and 260 <= y < 280
            if border or checker or bar:
                set_px(f1, x, y, 1)
    frames.append(("S71_selftest.pbm", "pbm", bytes(f1)))
    d1 = bytearray(NBYTES)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if (x - y) % 16 == 0 or (x + y) % 16 == 0:
                set_px(d1, x, y, 1)
    frames.append(("S72_selftest__f001_diag.png", "png", bytes(d1)))
    d2 = bytearray(NBYTES)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if not ((x - y) % 16 == 0 or (x + y) % 16 == 0):
                set_px(d2, x, y, 1)
    frames.append(("S72_selftest__f002_diag_inv.pbm", "pbm", bytes(d2)))
    return frames


_SELFTEST_MANIFEST = (
    "# check_ui.py --self-test manifest（格式真源：tests/UI_CONTRACT.md §3.4）\n"
    '{"frame": "S71_selftest.pbm", "scenario": "S71_selftest", "frame_index": 1,'
    ' "at_ms": 0, "action": "app_state", "seq": 1}\n'
    '{"frame": "S72_selftest__f001_diag.png", "scenario": "S72_selftest", "frame_index": 1,'
    ' "at_ms": 100, "action": "app_state", "seq": 2}\n'
    '{"frame": "S72_selftest__f002_diag_inv.pbm", "scenario": "S72_selftest", "frame_index": 2,'
    ' "at_ms": 200, "action": "key", "seq": 2}\n'
)


def _write_frame(path: Path, kind: str, raster: bytes) -> None:
    if kind == "png":
        write_png(path, WIDTH, HEIGHT, canonical_to_gray_rows(raster))
    else:
        write_pbm(path, raster)


def cmd_selftest() -> int:
    repo = Path(__file__).resolve().parents[1]
    ev = repo / "artifacts" / "ui" / "selftest"
    shutil.rmtree(ev, ignore_errors=True)
    golden = ev / "golden"
    golden.mkdir(parents=True)
    report: list = []

    def say(msg: str) -> None:
        report.append(msg)
        print(msg)

    ok = True
    say("== check_ui.py 自检（UI_CONTRACT §2.5：修改一个像素能令测试失败，失败保留图）==")
    say(f"证据目录: {ev}")
    frames = _selftest_frames()
    for name, kind, raster in frames:
        _write_frame(golden / name, kind, raster)

    # 正向对照：actual=golden → 全 PASS
    apass = ev / "actual_pass"
    apass.mkdir()
    for name, kind, raster in frames:
        (apass / name).write_bytes((golden / name).read_bytes())
    (apass / MANIFEST_NAME).write_text(_SELFTEST_MANIFEST, encoding="utf-8")
    try:
        results = compare_dirs(golden, apass)
        lines, rc = render_results(results)
    except EnvErr as e:
        lines, rc = [f"环境错误: {e}"], 2
    for l in lines:
        say("  " + l)
    pos_ok = rc == 0 and len(results) == 2 and all(r.passed for r in results)
    say(f"[自检] 正向对照（期望全 PASS、退出码 0）: {'通过' if pos_ok else '失败'}")
    ok = ok and pos_ok

    # 逐帧翻转恰好 1 像素 → 该场景 FAIL、差异像素=1、退出码 1、三图保留
    for name, kind, raster in frames:
        scen, _ = parse_frame_stem(Path(name).stem)
        flipdir = ev / f"actual_flip_{Path(name).stem}"
        flipdir.mkdir()
        for n2, k2, r2 in frames:
            _write_frame(flipdir / n2, k2, r2)
        f = bytearray(raster)
        set_px(f, 100, 100, 1 - get_px(f, 100, 100))  # 翻转恰 1 像素
        _write_frame(flipdir / name, kind, bytes(f))
        try:
            results = compare_dirs(golden, flipdir)
            lines, rc = render_results(results)
        except EnvErr as e:
            lines, rc = [f"环境错误: {e}"], 2
        for l in lines:
            say("  " + l)
        stem = Path(name).stem
        diff_png = flipdir / (stem + DIFF_SUFFIX)
        exp_png = flipdir / (stem + EXP_SUFFIX)
        scen_ok = (
            rc == 1
            and any(
                l.startswith(f"[FAIL] {scen}") and "差异像素=1 " in l for l in lines
            )
            and any(l.startswith("[PASS]") for l in lines)
            and diff_png.is_file()
            and exp_png.is_file()
        )
        say(
            f"[自检] 翻转1像素 {name}（期望 FAIL/差异像素=1/退出码 1/三图保留）: "
            f"{'通过' if scen_ok else '失败'}"
        )
        ok = ok and scen_ok

    (ev / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    say("SELF-TEST: PASS" if ok else "SELF-TEST: FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI（契约 §2.1）

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_ui.py",
        description="UI golden/actual 像素回归比较（契约：tests/UI_CONTRACT.md §2）",
        epilog=(
            "退出码: 0=全部 PASS；1=存在差异/缺帧/多余帧/尺寸不符；2=环境或用法错误。\n"
            "golden 目录只读；不提供任何更新 golden 的参数（契约 §2.6）。\n"
            "运行: uv run --python 3.12 python scripts/check_ui.py --golden tests/golden "
            "--actual artifacts/ui"
        ),
    )
    ap.add_argument("--golden", metavar="DIR", help="golden 目录（tests/golden/），只读，绝不写入")
    ap.add_argument(
        "--actual", metavar="DIR", help="actual 帧目录（通常 artifacts/ui/）；diff/expected 图写到该目录侧"
    )
    ap.add_argument(
        "--scenario",
        metavar="ID",
        default=None,
        help="只检查指定场景（S 编号或 slug，如 S04 / S04_plan_update）；缺省检查全部",
    )
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="内置自检（契约 §2.5）：翻转 1 像素必失败；证据写 artifacts/ui/selftest/",
    )
    args = ap.parse_args(argv)

    if args.self_test:
        if args.golden or args.actual:
            ap.error("--self-test 不能与 --golden/--actual 同时使用")
        return cmd_selftest()

    if not args.golden or not args.actual:
        ap.error("必须同时提供 --golden 与 --actual（内置自检请用 --self-test）")
    golden, actual = Path(args.golden), Path(args.actual)
    if not golden.is_dir():
        print(f"环境错误: --golden 目录不存在：{golden}", file=sys.stderr)
        return 2
    if not actual.is_dir():
        print(
            f"环境错误: --actual 目录不存在（回放未产出帧？）：{actual}", file=sys.stderr
        )
        return 2
    try:
        results = compare_dirs(golden, actual, args.scenario)
    except EnvErr as e:
        print(f"环境错误: {e}", file=sys.stderr)
        return 2
    lines, rc = render_results(results)
    for l in lines:
        print(l)
    return rc


if __name__ == "__main__":
    sys.exit(main())
