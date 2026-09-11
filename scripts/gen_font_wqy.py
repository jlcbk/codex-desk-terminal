#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_font_wqy.py — 文泉驿点阵宋 BDF → LVGL 9.3 C 字体（wqy 点阵落地，A2）。

生成 shared/ui/cdt_font_wqy16.c/.h：唯一 UI 正文字体档（16×16 点阵，
line_height 18 / base_line 4）。1bpp（A1）fmt_txt、LV_FONT_FMT_TXT_PLAIN：
点阵字体原生 1bit，无 AA——用户终选正文改点阵宋的根因（Noto 4bpp 在 1bpp
屏上量化发虚）。

字体栈定稿（用户实测确认 2026-09-11，A0 核准；覆盖原 fallback 方案）：
  全量 wqy 单字体：wenquanyi_12pt.bdf 全部 41,295 字形（CJK 统一区+扩展 A+
  假名+全韩文+希腊/西里尔+Latin），不做任何子集裁剪；
  fallback 链简化为 wqy → 可见替代符（无 noto 层，noto 已退出两端构建）；
  单一字号档（次级字号用行距/布局手段，不引第二档字体）；
  大字状态词 28px 维持 Montserrat；ASCII 用 wqy 自带 Latin（观感对比与理由
  见 artifacts/board/font-eval/ 与本次报告）。

字符集（确定性）：BDF 全量收编——ENCODING ≥ 0x20 的全部字形，同码点后到者
优先（本源实测无重复）。控制字符（<0x20）与未编码槽位不收录。生成器纯标准
库解析 BDF 文本，零第三方依赖、无随机——同输入逐字节同输出。

源 BDF（GPLv2 + 字体嵌入例外，COPYING 同目录入库）：
    third_party/dl/wqy-bitmapsong/wenquanyi_12pt.bdf
    sha256 eb8e295f761f28691ed9f199ba74c222d3392c6144174cb0b66c78c89a97fa49
    来源 wqy-bitmapsong 0.9.9.8 release 8（完整 tarball 留档
    artifacts/board/font-eval/）

BDF→LVGL 换算（推导记录）：
  编码：BDF 规范写十六进制，但 wqy 0.9.9.8 实测十进制（一=U+4E00 记作
  "ENCODING 19968"），按十进制解析。
  坐标：BDF y 轴向上、基线 y=0；BBX yoff=字形盒底到基线距离。LVGL fmt_txt
  glyph_top = baseline - box_h - ofs_y ⇒ ofs_y = yoff、ofs_x = xoff。
  adv_w = DWIDTH × 16（fmt_txt 28.4 定点，real×16）。
  位图：fmt_txt 1bpp PLAIN 以**连续位流**解码（vendor/lvgl/src/font/
  lv_font_fmt_txt.c bpp==1 分支：i&7 跨行不重置、每 8 位推进一字节）——
  整张字形 box_w×box_h 位、行优先、MSB 在左、行末 padding 不落盘。与 BDF
  原生「每行对齐到字节」在 box_w%8≠0 时不同（16×16 汉字两者恰好同构；
  ASCII 首跑曾因此错位，教训固化为本打包逻辑）。

用法：
    python3 scripts/gen_font_wqy.py [--bdf-dir third_party/dl/wqy-bitmapsong]
                                    [--out-dir shared/ui] [--check]
退出码 0 成功；--check 与已生成文件逐字节比对（一致 0，差异 1）。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BDF_DIR = REPO / "third_party" / "dl" / "wqy-bitmapsong"
OUT_DIR = REPO / "shared" / "ui"

# 期望 sha256（A0 入 VERSIONS；不符即拒生成，防错档/错版本 BDF 混入）
EXPECTED_SHA256 = {
    "wenquanyi_12pt.bdf": "eb8e295f761f28691ed9f199ba74c222d3392c6144174cb0b66c78c89a97fa49",
}

# 唯一字体档案：文件名沿用任务书（cdt_font_wqy16）；符号名按真实像素 16。
FONTS = [
    {
        "bdf": "wenquanyi_12pt.bdf",
        "header_name": "cdt_font_wqy16",
        "symbol": "cdt_font_wqy_16",
        "role": "正文/紧凑唯一字体档（activity/attention/plan/usage/项目名/底栏/行列表/提示条）",
    },
]


def parse_bdf(path: Path) -> tuple[dict[int, Glyph], int, int, int]:
    """解析 BDF：返回 (码点→字形[同码点后到者优先，确定性]、 ascent、descent、总条目数)。"""
    ascent = descent = None
    total = 0
    glyphs: dict[int, Glyph] = {}
    cp = None
    dwidth = 0
    bbx = (0, 0, 0, 0)
    rows: list[bytes] = []
    in_bitmap = False

    with path.open("r", encoding="ascii", errors="strict") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if in_bitmap:
                rows.append(bytes.fromhex(line))
                if len(rows) == bbx[1]:  # box_h 行即收口（ENDCHAR 前恰好等量）
                    in_bitmap = False
                continue
            key, *rest = line.split()
            if key == "STARTCHAR":
                cp, rows = None, []
            elif key == "ENCODING":
                # 注：BDF 规范写十六进制，但 wqy 0.9.9.8 实测为十进制（如
                # 一=U+4E00 记作 "ENCODING 19968"、字形名 U_4E00）；按十进制
                # 解析（GB2312 6763 汉字全可命中，十六进制解析只剩 216 个假
                # 命中）。越出 Unicode 上界立即报错防解析漂移。
                cp = int(rest[0], 10)
                if cp > 0x10FFFF:
                    raise SystemExit(f"{path.name}: ENCODING {rest[0]} 越出 Unicode")
            elif key == "DWIDTH":
                dwidth = int(rest[0])
            elif key == "BBX":
                bbx = tuple(int(v) for v in rest)  # type: ignore[assignment]
            elif key == "BITMAP":
                in_bitmap = True
            elif key == "ENDCHAR":
                total += 1
                if cp is not None and cp >= 0x20:  # 跳过未编码/控制字符占位
                    # 同码点重复时后到者优先（X11 惯例；本源实测无重复，防御性）
                    glyphs[cp] = Glyph(cp, dwidth, bbx, rows)  # type: ignore[arg-type]
            elif key == "FONT_ASCENT" and rest and rest[0].isdigit():
                ascent = int(rest[0])
            elif key == "FONT_DESCENT" and rest and rest[0].isdigit():
                descent = int(rest[0])
    if ascent is None or descent is None:
        raise SystemExit(f"{path.name}: 缺 FONT_ASCENT/FONT_DESCENT")
    return glyphs, ascent, descent, total


def gb2312_hanzi_cps() -> set[int]:
    """GB2312 全部 6763 汉字码点（程序化区位遍历），仅作覆盖报告用。"""
    cps: set[int] = set()
    for qu in range(0xB0, 0xF8):
        for wei in range(0xA1, 0xFF):
            try:
                cps.add(ord(bytes((qu, wei)).decode("gb2312")))
            except UnicodeDecodeError:
                continue
    return cps


class Glyph:
    __slots__ = ("cp", "dwidth", "box_w", "box_h", "ofs_x", "ofs_y", "rows")

    def __init__(self, cp: int, dwidth: int, bbx: tuple[int, int, int, int],
                 rows: list[bytes]):
        self.cp = cp
        self.dwidth = dwidth
        self.box_w, self.box_h, self.ofs_x, self.ofs_y = bbx
        self.rows = rows


def build_font(bdf_glyphs: dict[int, Glyph], cps: list[int]) -> tuple[bytes, list[dict]]:
    """按升序码点组装 1bpp 位图 blob 与 glyph_dsc 表（gid 0 保留占位）。

    位图打包必须复刻 LVGL fmt_txt 1bpp PLAIN 解码器语义
    （vendor/lvgl/src/font/lv_font_fmt_txt.c bpp==1 分支）：stride=0 时解码器
    以**连续位流**读取（i&7 计数跨行不重置、每 8 位推进一字节），即整张字形
    box_w×box_h 位行优先、MSB 在左、行末 padding 位不落盘——与 BDF 原生
    「每行独立对齐到字节」不同！box_w%8==0（如 16×16 汉字）两者恰好同构，
    其余宽度若按 BDF 原样拼接会整体错位（ASCII 首跑已翻车，教训固化）。
    """
    blob = bytearray()
    dscs: list[dict] = []
    for cp in cps:
        g = bdf_glyphs[cp]
        data = b"".join(g.rows)
        if any(data):  # 空白字形（如空格）：box 0×0，仅保留步进
            stride = (g.box_w + 7) // 8
            bits = bytearray()
            for y in range(g.box_h):
                row = data[y * stride:(y + 1) * stride]
                for x in range(g.box_w):
                    if row[x >> 3] & (0x80 >> (x & 7)):
                        bits.append(1)
                    else:
                        bits.append(0)
            packed = bytearray((len(bits) + 7) // 8)
            for i, b in enumerate(bits):
                if b:
                    packed[i >> 3] |= 0x80 >> (i & 7)
            offset = len(blob)
            blob += packed
            dscs.append({"bitmap_index": offset, "adv_w": g.dwidth * 16,
                         "box_w": g.box_w, "box_h": g.box_h,
                         "ofs_x": g.ofs_x, "ofs_y": g.ofs_y})
        else:
            dscs.append({"bitmap_index": len(blob), "adv_w": g.dwidth * 16,
                         "box_w": 0, "box_h": 0, "ofs_x": 0, "ofs_y": 0})
    return bytes(blob), dscs


def fmt_bytes(data: bytes, indent: str = "    ", per_line: int = 16) -> str:
    lines = []
    for i in range(0, len(data), per_line):
        chunk = data[i:i + per_line]
        lines.append(indent + " ".join(f"0x{b:02x}," for b in chunk))
    return "\n".join(lines) if lines else indent


def fmt_u16(values: list[int], indent: str = "    ", per_line: int = 12) -> str:
    lines = []
    for i in range(0, len(values), per_line):
        chunk = values[i:i + per_line]
        lines.append(indent + " ".join(f"0x{v:04x}," for v in chunk))
    return "\n".join(lines) if lines else indent


def generate_font(font: dict, bdf_path: Path, out_dir: Path) -> tuple[str, str, dict]:
    glyphs, ascent, descent, total_entries = parse_bdf(bdf_path)
    cps = sorted(glyphs)  # BDF 全量（ENCODING≥0x20），升序

    range_start = cps[0]
    range_length = cps[-1] - cps[0] + 1
    if range_length > 65535:
        raise SystemExit(f"码点跨度过大 {range_length}（>65535，SPARSE_TINY 放不下）")
    rcp_list = [cp - range_start for cp in cps]
    line_height = ascent + descent
    base_line = descent
    sha = hashlib.sha256(bdf_path.read_bytes()).hexdigest()

    # GB2312/CJK 覆盖报告（全量收编下的覆盖声明，非裁剪依据）
    gb_hanzi = gb2312_hanzi_cps()
    gb_hit = len(gb_hanzi & set(cps))
    if gb_hit != 6763:
        raise SystemExit(f"GB2312 汉字覆盖 {gb_hit}/6763 ≠ 全覆盖——源 BDF 异常")

    blob, dscs = build_font(glyphs, cps)

    c_src = f"""\
/*
 * {font["header_name"]}.c — 文泉驿点阵宋 1bpp 字体（生成文件，勿手改；wqy 点阵落地，A2）
 *
 * 角色：{font["role"]}
 * 生成器：scripts/gen_font_wqy.py（纯标准库 BDF 解析，零第三方依赖，确定性输出）：
 *   python3 scripts/gen_font_wqy.py            # 再生成（同输入逐字节同输出）
 *   python3 scripts/gen_font_wqy.py --check    # 与本文件逐字节比对
 * 字体真源：WenQuanYi Bitmap Song 0.9.9.8 release 8（GPLv2 + 字体嵌入例外，
 *   COPYING 入库 third_party/dl/wqy-bitmapsong/；tarball 留档
 *   artifacts/board/font-eval/）
 *   源 BDF：{bdf_path.name}  sha256 {sha}
 *   BDF 头：FONT_ASCENT {ascent} / FONT_DESCENT {descent} → line_height {line_height}、
 *   base_line {base_line}；源文件 CHARS {total_entries} 条
 * 字符集（字体栈定稿 2026-09-11：BDF 全量收编，无子集裁剪、无 fallback 层）：
 *   共 {len(cps)} 码点（U+{range_start:04X}–U+{cps[-1]:04X}，跨区 {range_length}）=
 *   CJK 统一表意+扩展 A+假名+韩文+希腊/西里尔+Latin 全部字形；GB2312
 *   {gb_hit}/6763 汉字原生全覆盖。
 * 格式：LVGL fmt_txt，bpp=1（LV_FONT_FMT_TXT_PLAIN），SPARSE_TINY cmap；
 *   点阵原生 1bit 无 AA——正文换点阵宋的根因（Noto 4bpp 在 1bpp 屏量化发虚）。
 * fallback 链（定稿）：wqy → 可见替代符（cdt_ui_ascii_safe 折 '?'，契约 §6
 *   不静默缺字）。emoji 等本字体没有的码点由 ascii_safe 折为 '?'。
 */
#include <stdint.h>

#include "lvgl.h"

#include "{font["header_name"]}.h"

/* ---- 覆盖表（SPARSE_TINY unicode_list，rcp = cp - range_start 升序）---- */
#define CDT_WQY_RANGE_START ((uint32_t)0x{range_start:04X}u)
#define CDT_WQY_RANGE_LENGTH ((uint16_t)0x{range_length:04X}u) /* = {range_length} */

static const uint16_t wqy_unicode_list[] = {{
{fmt_u16(rcp_list)}
}};

static LV_ATTRIBUTE_LARGE_CONST const uint8_t glyph_bitmap[] = {{
{fmt_bytes(blob)}
}};

/* glyph_dsc[0] 为保留占位（gid 0 = 未命中）；之后按码点升序一一对应 */
static const lv_font_fmt_txt_glyph_dsc_t glyph_dsc[] = {{
    {{.bitmap_index = 0, .adv_w = 0, .box_w = 0, .box_h = 0, .ofs_x = 0, .ofs_y = 0}},"""
    for cp, d in zip(cps, dscs):
        c_src += (
            "\n    /* U+%04X */ {.bitmap_index = %u, .adv_w = %u, .box_w = %u, "
            ".box_h = %u, .ofs_x = %d, .ofs_y = %d}," % (
                cp, d["bitmap_index"], d["adv_w"], d["box_w"], d["box_h"],
                d["ofs_x"], d["ofs_y"]))
    c_src += f"""\
}};

static const lv_font_fmt_txt_cmap_t cmaps[] = {{
    {{
        .range_start = CDT_WQY_RANGE_START, .range_length = CDT_WQY_RANGE_LENGTH,
        .glyph_id_start = 1,
        .unicode_list = wqy_unicode_list, .glyph_id_ofs_list = NULL,
        .list_length = {len(rcp_list)}, .type = LV_FONT_FMT_TXT_CMAP_SPARSE_TINY
    }},
}};

static const lv_font_fmt_txt_dsc_t font_dsc = {{
    .glyph_bitmap = glyph_bitmap,
    .glyph_dsc = glyph_dsc,
    .cmaps = cmaps,
    .kern_dsc = NULL,
    .kern_scale = 0,
    .cmap_num = 1,
    .bpp = 1,
    .kern_classes = 0,
    .bitmap_format = 0,
    .stride = 0,
}};

const lv_font_t {font["symbol"]} = {{
    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,
    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,
    .line_height = {line_height},
    .base_line = {base_line},
    .subpx = LV_FONT_SUBPX_NONE,
    .kerning = LV_FONT_KERNING_NORMAL,
    .static_bitmap = 1,
    .underline_position = -1,
    .underline_thickness = 1,
    .dsc = &font_dsc,
    .fallback = NULL,
    .user_data = NULL,
}};

/* ---- 码点覆盖查询（cdt_ui_ascii_safe 放行判断；二分） ---- */
bool {font["header_name"]}_covers(uint32_t codepoint)
{{
    const uint32_t rcp = codepoint - CDT_WQY_RANGE_START;
    int lo = 0, hi = (int)(sizeof(wqy_unicode_list) / sizeof(wqy_unicode_list[0])) - 1;

    if (codepoint < CDT_WQY_RANGE_START ||
        rcp >= (uint32_t)CDT_WQY_RANGE_LENGTH) return false;
    while (lo <= hi) {{
        int mid = lo + (hi - lo) / 2;
        uint16_t v = wqy_unicode_list[mid];
        if (v == (uint16_t)rcp) return true;
        if (v < (uint16_t)rcp) lo = mid + 1;
        else hi = mid - 1;
    }}
    return false;
}}
"""

    h_src = f"""\
/*
 * {font["header_name"]}.h — 文泉驿点阵宋 1bpp 字体（生成文件，勿手改）
 * 真源、字符集与 fallback 链见 {font["header_name"]}.c 头注释；
 * 契约：docs/VERSIONS.md 字体行、tests/SCENARIOS.md §5.3（不静默缺字）。
 */
#ifndef CDT_WQY_{font["symbol"].upper()}_H
#define CDT_WQY_{font["symbol"].upper()}_H

#include <stdint.h>

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {{
#endif

/* {font["role"]} */
extern const lv_font_t {font["symbol"]};

/* 码点是否在本字体覆盖范围内（cdt_ui_ascii_safe 放行判断） */
bool {font["header_name"]}_covers(uint32_t codepoint);

#ifdef __cplusplus
}}
#endif

#endif /* CDT_WQY_{font["symbol"].upper()}_H */
"""
    stats = {
        "count": len(cps),
        "cp_first": range_start,
        "cp_last": cps[-1],
        "range_len": range_length,
        "blob_bytes": len(blob),
        "line_height": line_height,
        "base_line": base_line,
        "entries": total_entries,
    }
    return c_src, h_src, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bdf-dir", type=Path, default=DEFAULT_BDF_DIR)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--check", action="store_true",
                    help="与已生成文件逐字节比对，不写入")
    args = ap.parse_args()

    targets: list[tuple[Path, str]] = []
    for font in FONTS:
        bdf_path = args.bdf_dir / font["bdf"]
        expect = EXPECTED_SHA256.get(font["bdf"])
        sha = hashlib.sha256(bdf_path.read_bytes()).hexdigest()
        if expect is None or sha != expect:
            print(f"{font['bdf']} sha256 不符：{sha}\n  期望 {expect}", file=sys.stderr)
            return 2
        c_src, h_src, st = generate_font(font, bdf_path, args.out_dir)
        targets.append((args.out_dir / f"{font['header_name']}.c", c_src))
        targets.append((args.out_dir / f"{font['header_name']}.h", h_src))
        print(
            f"[wqy] {font['bdf']} → {font['header_name']}.c：全量 {st['count']} 码点"
            f"（U+{st['cp_first']:04X}–U+{st['cp_last']:04X}，跨区 {st['range_len']}）；"
            f"1bpp 位图 {st['blob_bytes']} B（未含 dsc/cmap 表）；"
            f"line_height {st['line_height']} / base_line {st['base_line']}")

    if args.check:
        ok = True
        for path, want in targets:
            got = path.read_text(encoding="utf-8") if path.is_file() else None
            if got != want:
                ok = False
                print(f"差异：{path}")
        print("check: 一致" if ok else "check: 不一致（需重新生成）")
        return 0 if ok else 1
    for path, content in targets:
        path.write_text(content, encoding="utf-8")
        print(f"写出 {path} ({len(content)} 字节)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
