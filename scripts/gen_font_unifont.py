#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_font_unifont.py — GNU Unifont BDF → LVGL 9.3 C 字体（unifont 换型，A2）。

生成 shared/ui/cdt_font_unifont16.c/.h：F_TITLE/F_BODY/F_BAR 主字体档。
换型根因（用户真屏目检反馈 2026-09-11）：wqy 的 ASCII 非等宽不舒服、整体偏细
——Unifont 8×16 半宽 ASCII 严格等宽（2:1 终端比例）、笔画更粗、全 BMP 指派
码点覆盖。wqy 资产（cdt_font_wqy16.c/.h + gen_font_wqy.py）留仓不删，回切
= 字槽改回 cdt_font_wqy16 + 重跑 gen_font_wqy.py（见 gen_font_unifont 报告）。

源 BDF（双许可：SIL OFL 1.1 + GPLv2+ 含 GNU 字体嵌入例外；COPYING/OFL-1.1
同目录入库）：
    third_party/dl/unifont/unifont-17.0.05.bdf
    sha256 b091329695a70ab5f44e940d43bdc0e6204b03671df96b08c53bcf0ac85fde46
    来源 https://ftp.gnu.org/gnu/unifont/unifont-17.0.05/unifont-17.0.05.bdf.gz
    （.gz sha256 db0111c066edfe7583f0d77adbecbba463f00643a37dc3b9651ae9349543487f）

Unifont BDF 实测记录（17.0.05，2026-09-11 本转换器定稿依据）：
  编码：ENCODING 十进制（一=U+4E00 记作 "ENCODING 19968"，与 wqy 同坑；字形名
    为 "U+4E00" 式十六进制后缀但解析只认 ENCODING 行）。按十进制解析。
  字形规格：仅两种——49,804 全宽 BBX 16 16 0 -2（DWIDTH 16）+ 7,282 半宽
    BBX 8 16 0 -2（DWIDTH 8）；无重复 ENCODING；最大码点 0xFFFD。
  行高：FONT_ASCENT 14 / FONT_DESCENT 2 → 原生 line_height 16 / base_line 2。
    **归一**：现 UI（wqy16 定稿）line_height 18 / base_line 4；转换器输出固定
    line_height=18、base_line=4，字形仍锚定基线（ofs_y=BDF yoff 不变）——
    基线与行距和 wqy16 完全一致，布局零参数变化；两源 CJK 均为 16×16 yoff -2，
    墨迹行完全对齐（顶部 baseline-14）。Unifont 半宽 ASCII 8×16 同样占满
    baseline-14..baseline+1（比 wqy 的 13-14px Latin 高 2px，等宽终端风格）。
  覆盖（生成前程序化门禁，任一不足即拒生成）：ASCII 95/95、GB2312 汉字
    6763/6763、CJK 统一区 U+4E00–9FFF 20,992/20,992、扩展 A 6,592/6,592、
    假名 192/192、谚文 11,172/11,172。全 BMP 无字形码点仅三类（Python
    unicodedata 逐点核验 2026-09-11）：未指派 Cn、代理区 Cs、私用区 Co
    （6,400 点）——公开指派码点零缺字。

BDF→LVGL 换算（沿用 gen_font_wqy.py 固化的三坑，推导记录见彼处）：
  fmt_txt 1bpp PLAIN 连续位流打包（跨行不对齐、行末 padding 不落盘）；
  adv_w = DWIDTH × 16（28.4 定点）；ofs_x/ofs_y = BBX xoff/yoff。
  位图 blob ≈1.63MB > 2^20 → 必须 LV_FONT_FMT_TXT_LARGE=1（两端 lv_conf 已开，
  bitmap_index 才是完整 uint32_t）。

用法：
    python3 scripts/gen_font_unifont.py [--bdf-dir third_party/dl/unifont]
                                        [--out-dir shared/ui] [--check]
退出码 0 成功；--check 与已生成文件逐字节比对（一致 0，差异 1）。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BDF_DIR = REPO / "third_party" / "dl" / "unifont"
OUT_DIR = REPO / "shared" / "ui"

# 期望 sha256（不符即拒生成，防错档/错版本 BDF 混入）
EXPECTED_SHA256 = {
    "unifont-17.0.05.bdf": "b091329695a70ab5f44e940d43bdc0e6204b03671df96b08c53bcf0ac85fde46",
}

# 唯一字体档案：文件名 cdt_font_unifont16；符号名按真实像素 16。
FONTS = [
    {
        "bdf": "unifont-17.0.05.bdf",
        "header_name": "cdt_font_unifont16",
        "symbol": "cdt_font_unifont_16",
        "role": "正文/紧凑唯一字体档（activity/attention/plan/usage/项目名/底栏/行列表/提示条）",
    },
]

# 行高归一目标（与 wqy16 定稿/现 UI 一致，见模块 docstring「行高」节）
NORM_LINE_HEIGHT = 18
NORM_BASE_LINE = 4

# 覆盖门禁：块区间 → (期望数, 描述)。全 BMP 指派码点覆盖的确定性下界。
def _gb2312_hanzi_cps() -> set[int]:
    """GB2312 全部 6763 汉字码点（程序化区位遍历）。"""
    cps: set[int] = set()
    for qu in range(0xB0, 0xF8):
        for wei in range(0xA1, 0xFF):
            try:
                cps.add(ord(bytes((qu, wei)).decode("gb2312")))
            except UnicodeDecodeError:
                continue
    return cps


def _coverage_gates() -> list[tuple[range, int, str]]:
    return [
        (range(0x20, 0x7F), 95, "ASCII 可打印区"),
        (range(0x4E00, 0xA000), 20992, "CJK 统一表意区 U+4E00–9FFF"),
        (range(0x3400, 0x4DC0), 6592, "CJK 扩展 A U+3400–4DBF"),
        (range(0x3040, 0x3100), 192, "假名 U+3040–30FF"),
        (range(0xAC00, 0xD7A4), 11172, "谚文音节 U+AC00–D7A3"),
        (range(0, 0), 6763, ""),  # 占位：GB2312 在 main 中单独校验
    ]


class Glyph:
    __slots__ = ("cp", "dwidth", "box_w", "box_h", "ofs_x", "ofs_y", "rows")

    def __init__(self, cp: int, dwidth: int, bbx: tuple[int, int, int, int],
                 rows: list[bytes]):
        self.cp = cp
        self.dwidth = dwidth
        self.box_w, self.box_h, self.ofs_x, self.ofs_y = bbx
        self.rows = rows


def parse_bdf(path: Path) -> tuple[dict[int, Glyph], int, int, int]:
    """解析 BDF：返回 (码点→字形[同码点后到者优先，确定性]、ascent、descent、总条目数)。"""
    ascent = descent = None
    total = 0
    glyphs: dict[int, Glyph] = {}
    cp = None
    dwidth = 0
    bbx = (0, 0, 0, 0)
    rows: list[bytes] = []
    in_bitmap = False

    # 注：BDF 规范正文为 ASCII，但 Unifont 的 COPYRIGHT 属性含 UTF-8 作者名
    # （"Ælla…"，wqy 无此情况）——按 UTF-8 解码，位图十六进制行不受影响。
    with path.open("r", encoding="utf-8", errors="strict") as fh:
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
                # 注：BDF 规范写十六进制，但 Unifont 17.0.05 实测十进制（如
                # 一=U+4E00 记作 "ENCODING 19968"、字形名 U+4E00）；按十进制
                # 解析（与 wqy 同坑，先实测再定）。越出 Unicode 上界立即报错。
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


def build_font(bdf_glyphs: dict[int, Glyph], cps: list[int]) -> tuple[bytes, list[dict]]:
    """按升序码点组装 1bpp 位图 blob 与 glyph_dsc 表（gid 0 保留占位）。

    位图打包必须复刻 LVGL fmt_txt 1bpp PLAIN 解码器语义
    （vendor/lvgl/src/font/lv_font_fmt_txt.c bpp==1 分支）：stride=0 时解码器
    以**连续位流**读取（i&7 计数跨行不重置、每 8 位推进一字节），即整张字形
    box_w×box_h 位行优先、MSB 在左、行末 padding 位不落盘——与 BDF 原生
    「每行独立对齐到字节」不同！Unifont 全宽 16×16 两者同构，但半宽 ASCII
    8×16 的 box_w%8==0 也同构；此逻辑为任意宽度兜底（wqy ASCII 首跑教训
    固化，不因本源恰好同构而删）。
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


def generate_font(font: dict, bdf_path: Path) -> tuple[str, str, dict]:
    glyphs, ascent, descent, total_entries = parse_bdf(bdf_path)
    cps = sorted(glyphs)  # BDF 全量（ENCODING≥0x20），升序

    range_start = cps[0]
    range_length = cps[-1] - cps[0] + 1
    if range_length > 65535:
        raise SystemExit(f"码点跨度过大 {range_length}（>65535，SPARSE_TINY 放不下）")
    if len(cps) > 65534:  # glyph_id_start=1 起，gid ≤ 0xFFFF
        raise SystemExit(f"字形数 {len(cps)} 超出 uint16 gid 空间")
    rcp_list = [cp - range_start for cp in cps]
    # 行高归一：基线/行距与现 UI（wqy16 定稿 line_height 18/base_line 4）一致，
    # 字形锚定基线不变（见模块 docstring「行高」节）
    line_height = NORM_LINE_HEIGHT
    base_line = NORM_BASE_LINE
    sha = hashlib.sha256(bdf_path.read_bytes()).hexdigest()

    # 覆盖门禁（任一不足即拒生成——BMP 全覆盖声明的确定性下界）
    gb_hanzi = _gb2312_hanzi_cps()
    gb_hit = len(gb_hanzi & set(cps))
    if gb_hit != 6763:
        raise SystemExit(f"GB2312 汉字覆盖 {gb_hit}/6763 ≠ 全覆盖——源 BDF 异常")
    for block, expect, desc in _coverage_gates()[:-1]:
        hit = sum(1 for c in cps if c in block)
        if hit != expect:
            raise SystemExit(f"{desc} 覆盖 {hit}/{expect} ≠ 全覆盖——源 BDF 异常")

    blob, dscs = build_font(glyphs, cps)

    c_src = f"""\
/*
 * {font["header_name"]}.c — GNU Unifont 1bpp 字体（生成文件，勿手改；unifont 换型，A2）
 *
 * 角色：{font["role"]}
 * 换型根因：用户真屏目检（2026-09-11）——wqy ASCII 非等宽不舒服、整体偏细；
 *   Unifont 半宽 ASCII 8×16 严格等宽（2:1 终端比例）、笔画更粗、全 BMP 指派
 *   码点覆盖。wqy 资产（cdt_font_wqy16 + scripts/gen_font_wqy.py）留仓不删，
 *   回切 = 字槽改回 &cdt_font_wqy_16（cdt_ui_internal.h）+ 重跑 gen_font_wqy.py。
 * 生成器：scripts/gen_font_unifont.py（纯标准库 BDF 解析，零第三方依赖，确定性输出）：
 *   python3 scripts/gen_font_unifont.py            # 再生成（同输入逐字节同输出）
 *   python3 scripts/gen_font_unifont.py --check    # 与本文件逐字节比对
 * 字体真源：GNU Unifont 17.0.05（双许可 SIL OFL 1.1 + GPLv2+ 含 GNU 字体嵌入
 *   例外；COPYING/OFL-1.1 入库 third_party/dl/unifont/）
 *   源 BDF：{bdf_path.name}  sha256 {sha}
 *   来源 https://ftp.gnu.org/gnu/unifont/unifont-17.0.05/unifont-17.0.05.bdf.gz
 *   BDF 头：FONT_ASCENT {ascent} / FONT_DESCENT {descent}（原生 line_height {ascent + descent}/
 *   base_line {descent}）→ **归一** line_height {line_height} / base_line {base_line}
 *   （与 wqy16 定稿一致，字形锚定基线不变，布局零变化）；源文件 CHARS {total_entries} 条
 * 字符集（全量收编，无子集裁剪）：共 {len(cps)} 码点
 *   （U+{range_start:04X}–U+{cps[-1]:04X}，跨区 {range_length}）= 全 BMP 指派码点：
 *   ASCII 95/95、GB2312 汉字 6763/6763、CJK 统一区 20,992/20,992、扩展 A
 *   6,592/6,592、假名 192/192、谚文 11,172/11,172；无字形码点仅未指派（Cn）、
 *   代理区（Cs）、私用区（Co）——公开指派码点零缺字。
 * 格式：LVGL fmt_txt，bpp=1（LV_FONT_FMT_TXT_PLAIN），SPARSE_TINY cmap；
 *   点阵原生 1bit 无 AA。位图 {len(blob)} B > 2^20 → 两端 lv_conf
 *   LV_FONT_FMT_TXT_LARGE=1（bitmap_index 完整 uint32_t）为硬前提。
 * fallback 链：unifont → 可见替代符（cdt_ui_ascii_safe 折 '?'，契约 §6
 *   不静默缺字）。emoji 等本字体没有的码点由 ascii_safe 折为 '?'。
 */
#include <stdint.h>

#include "lvgl.h"

#include "{font["header_name"]}.h"

/* ---- 覆盖表（SPARSE_TINY unicode_list，rcp = cp - range_start 升序）---- */
#define CDT_UNIFONT_RANGE_START ((uint32_t)0x{range_start:04X}u)
#define CDT_UNIFONT_RANGE_LENGTH ((uint16_t)0x{range_length:04X}u) /* = {range_length} */

static const uint16_t unifont_unicode_list[] = {{
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
        .range_start = CDT_UNIFONT_RANGE_START, .range_length = CDT_UNIFONT_RANGE_LENGTH,
        .glyph_id_start = 1,
        .unicode_list = unifont_unicode_list, .glyph_id_ofs_list = NULL,
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
    const uint32_t rcp = codepoint - CDT_UNIFONT_RANGE_START;
    int lo = 0, hi = (int)(sizeof(unifont_unicode_list) / sizeof(unifont_unicode_list[0])) - 1;

    if (codepoint < CDT_UNIFONT_RANGE_START ||
        rcp >= (uint32_t)CDT_UNIFONT_RANGE_LENGTH) return false;
    while (lo <= hi) {{
        int mid = lo + (hi - lo) / 2;
        uint16_t v = unifont_unicode_list[mid];
        if (v == (uint16_t)rcp) return true;
        if (v < (uint16_t)rcp) lo = mid + 1;
        else hi = mid - 1;
    }}
    return false;
}}
"""

    h_src = f"""\
/*
 * {font["header_name"]}.h — GNU Unifont 1bpp 字体（生成文件，勿手改）
 * 真源、字符集与 fallback 链见 {font["header_name"]}.c 头注释；
 * 契约：docs/VERSIONS.md 字体行、tests/SCENARIOS.md §5.3（不静默缺字）。
 */
#ifndef CDT_UNIFONT_{font["symbol"].upper()}_H
#define CDT_UNIFONT_{font["symbol"].upper()}_H

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

#endif /* CDT_UNIFONT_{font["symbol"].upper()}_H */
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
        c_src, h_src, st = generate_font(font, bdf_path)
        targets.append((args.out_dir / f"{font['header_name']}.c", c_src))
        targets.append((args.out_dir / f"{font['header_name']}.h", h_src))
        print(
            f"[unifont] {font['bdf']} → {font['header_name']}.c：全量 {st['count']} 码点"
            f"（U+{st['cp_first']:04X}–U+{st['cp_last']:04X}，跨区 {st['range_len']}）；"
            f"1bpp 位图 {st['blob_bytes']} B（未含 dsc/cmap 表）；"
            f"line_height {st['line_height']} / base_line {st['base_line']}（归一）")

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
