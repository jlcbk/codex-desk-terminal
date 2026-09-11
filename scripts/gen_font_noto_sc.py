#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_font_noto_sc.py — Noto Sans SC 子集 → LVGL 9.3 C 字体（P2.4 集成，A6）。

生成 shared/ui/cdt_font_noto_sc.c/.h：14px 与 16px 两档，A4（4bpp）位图，
SPARSE_TINY cmap，另输出码点覆盖表 cdt_font_noto_sc_covers() 供
cdt_ui_ascii_safe 放行已覆盖码点（未覆盖仍折为可见 '?'，不静默缺字）。
两档字体只作 lv_font_t.fallback 使用（主字体 Montserrat 14/16，ASCII 渲染
逐像素不变）；若主字体缺字由 LVGL 逐字形回退到本字体。

生成期依赖（仅本脚本，一次性，不进构建；版本锁定保证再生成零漂移）：
    uv run --python 3.12 --with fonttools==4.64.0 --with pillow==12.3.0 \
        python scripts/gen_font_noto_sc.py
选 fonttools+Pillow 自研导出器而非 lv_font_conv：零 npm/node 依赖（任务红线
倾向），fontTools 解析字体度量/ cmap、Pillow(FreeType) 栅格化，两者均为
声明式纯转换，输出确定；生成产物（C 源码）入库，构建本身不依赖任何 Python。
（不锁版本时 uv 会拉最新 fonttools，度量/解析行为不保证逐位一致——禁止。）

字体真源（OFL，不入库）：third_party/dl/NotoSansSC-Regular.otf
    来源 github.com/notofonts/noto-cjk Sans/SubsetOTF/SC Regular v2.004
    sha256 faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9
子集范围：ASCII 0x20–0x7E + tests/SCENARIOS.md、tests/UI_CONTRACT.md、
tests/fixtures/scenarios/、bridge/ 文案中出现的全部非 ASCII 码点
（CJK 统一表意 + 中日韩标点/全角形式 + 文档用符号 → ∈ ≠ ≤ ≥ § × 等；
刻意不含 emoji——S17 要求未知字形以可见替代符呈现）。
wqy 点阵落地（A2，2026-09-11）后不再并入 GB2312 一级字表：GB2312 汉字由
cdt_font_wqy_16/14 原生覆盖，本字体降级为纯 fallback（项目文案口径）。

用法：
    python scripts/gen_font_noto_sc.py [--font PATH] [--out-dir shared/ui] [--check]
退出码 0 成功；--check 与已生成文件逐字节比对（无差异 0，有差异 1）。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FONT = REPO / "third_party" / "dl" / "NotoSansSC-Regular.otf"
OUT_DIR = REPO / "shared" / "ui"

SIZES = (14, 16)
EXPECTED_SHA256 = "faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9"

# 生成期工具版本锁（与首个入库产物一致）：栅格化/度量解析必须逐位可复现，
# 版本漂移可能改变已有字形 → golden 回归。--with 显式锁定，此处双保险硬校验。
PINNED_FONTTOOLS = "4.64.0"
PINNED_PILLOW = "12.3.0"

# 与锁定版本一致的确定性子集来源（charset 真源，勿加运行期随机内容）
CHARSET_SOURCES = [
    "tests/SCENARIOS.md",
    "tests/UI_CONTRACT.md",
    "tests/fixtures/scenarios/README.md",
    "tests/fixtures/scenarios/S_lifecycle.jsonl",
] + sorted(glob.glob("bridge/**/*.py", recursive=True))
CHARSET_SOURCES += sorted(glob.glob("tests/fixtures/bridge/*.jsonl"))

HEADER_NAME = "cdt_font_noto_sc"


def gb2312_level1() -> list[int]:
    """GB2312 一级汉字（3755 字）——程序化生成，不引入外部字表文件（确定性+无版权）。

    用 Python 标准编码 gb2312 遍历：区码 0xB0–0xD7 × 位码 0xA1–0xFE（EUC-CN 双
    字节）。一级字表止于 0xD7F9（区 0xD7 位 0xFA 起为空位/二级起点）；解码越界/
    异常的位跳过（容错）。期望恰返回 3755 个互异码点——数量异常立即报错，
    防上游编码表变动静默改变子集。
    """
    cps: list[int] = []
    for qu in range(0xB0, 0xD8):
        for wei in range(0xA1, 0xFF):
            if qu == 0xD7 and wei > 0xF9:
                break  # 一级字表止于 0xD7F9
            try:
                cps.append(ord(bytes((qu, wei)).decode("gb2312")))
            except UnicodeDecodeError:
                continue  # 空位/异常：跳过
    if len(cps) != 3755 or len(set(cps)) != 3755:
        raise SystemExit(
            f"GB2312 一级字表解码数异常：{len(cps)}（含去重 {len(set(cps))}），期望 3755")
    return cps


def collect_codepoints() -> list[int]:
    """字符集真源（确定性，来源分布见 charset_stats()）：ASCII + 项目文案全部
    非 ASCII，返回升序去重码点表。

    wqy 点阵落地（A2，2026-09-11）后本字体降级为纯 fallback：GB2312 汉字由
    cdt_font_wqy_16/14 原生覆盖，本字体回到项目文案口径（不再并入
    gb2312_level1()），为 1M factory 分区让位；fallback 链语义不变
    （wqy → 本字体 → 可见替代符）。历史口径（P2.4 扩充 GB2312 一级）见
    git log；如需恢复，在 collect_codepoints 里重新并集 gb2312_level1()。
    """
    cps = set(range(0x20, 0x7F))  # 常用 ASCII（可打印段）
    for rel in CHARSET_SOURCES:
        text = (REPO / rel).read_text(encoding="utf-8")
        for ch in text:
            if ord(ch) >= 0x80:
                cps.add(ord(ch))
    return sorted(cps)


def charset_stats(cps: list[int]) -> dict[str, int]:
    """来源分布统计（生成器报告打印用）。"""
    ascii_set = set(range(0x20, 0x7F))
    gb_set = set(gb2312_level1())
    proj = set()
    for rel in CHARSET_SOURCES:
        for ch in (REPO / rel).read_text(encoding="utf-8"):
            if ord(ch) >= 0x80:
                proj.add(ord(ch))
    return {
        "ascii": len(ascii_set & set(cps)),
        "gb2312": len(gb_set & set(cps)),
        "gb_new_vs_project": len(gb_set - proj - ascii_set),
        "project_nonascii": len(proj),
        "project_outside_gb": len(proj - gb_set),
        "total": len(cps),
    }


def rasterize_size(font_path: Path, size: int, cps: list[int]):
    """返回 (bitmaps: list[bytes], dscs: list[dict], line_height, base_line)。

    基线坐标约定：Pillow anchor="ls"（左-基线），y 向下为正、基线上方为负。
    LVGL fmt_txt 的 ofs_y 约定（见 lv_draw_label.c: glyph_top = baseline -
    box_h - ofs_y）=「字形底到基线的距离」，故 ofs_y = -y1（y1=墨迹底部，
    基线下方为正）。A4 量化 idx = 灰度 >> 4。
    """
    from PIL import Image, ImageDraw, ImageFont
    from fontTools.ttLib import TTFont

    tt = TTFont(str(font_path))
    upem = tt["head"].unitsPerEm
    os2 = tt["OS/2"]
    cmap = tt.getBestCmap()
    hmtx = tt["hmtx"]

    ascent, descent = os2.sTypoAscender, -os2.sTypoDescender  # 正值
    line_height = round((ascent + descent) * size / upem)
    base_line = max(1, round(descent * size / upem))

    pil = ImageFont.truetype(str(font_path), size)
    bitmaps: list[bytes] = []
    dscs: list[dict] = []
    for cp in cps:
        ch = chr(cp)
        gname = cmap.get(cp)
        if gname is None:
            raise SystemExit(f"字体缺字形 U+{cp:04X} {ch!r}——请收窄子集或换字体")
        adv_units = hmtx[gname][0]
        adv_w = round(adv_units * size * 16 / upem)  # 8.4 定点
        x0, y0, x1, y1 = pil.getbbox(ch, anchor="ls")
        if x1 <= x0 or y1 <= y0:
            # 空白字形（如 U+00A0 类）：一位图也不留，box 0×0
            bitmaps.append(b"")
            dscs.append({"adv_w": adv_w, "box_w": 0, "box_h": 0, "ofs_x": 0, "ofs_y": 0})
            continue
        w, h = x1 - x0, y1 - y0
        img = Image.new("L", (w, h), 0)
        ImageDraw.Draw(img).text((0 - x0, 0 - y0), ch, font=pil, fill=255, anchor="ls")
        assert img.getbbox() is not None, f"U+{cp:04X} 栅格化为空"
        bits = img.tobytes()
        bitmaps.append(bytes(v >> 4 for v in bits))  # A4 nibble/像素
        dscs.append({"adv_w": adv_w, "box_w": w, "box_h": h,
                     "ofs_x": x0, "ofs_y": -y1})
    return bitmaps, dscs, line_height, base_line


def pack_a4(bitmaps: list[bytes]) -> tuple[bytes, list[int]]:
    """半字节序列 → 字节流（偶数下标在高半字节），返回 (blob, 每字形起始字节偏移)。"""
    out = bytearray()
    offsets = []
    for bm in bitmaps:
        offsets.append(len(out))
        for i in range(0, len(bm) - 1, 2):
            out.append((bm[i] << 4) | bm[i + 1])
        if len(bm) & 1:
            out.append(bm[-1] << 4)
    return bytes(out), offsets


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


def generate(font_path: Path, out_dir: Path) -> tuple[str, str]:
    cps = collect_codepoints()
    import PIL
    import fontTools
    import PIL.features as feats

    if fontTools.version != PINNED_FONTTOOLS or PIL.__version__ != PINNED_PILLOW:
        raise SystemExit(
            f"生成期工具版本漂移：fonttools {fontTools.version}"
            f"（锁 {PINNED_FONTTOOLS}）/ Pillow {PIL.__version__}"
            f"（锁 {PINNED_PILLOW}）——栅格化不保证逐位一致，请按 docstring 锁版本再生成")

    per_size = {}
    for size in SIZES:
        per_size[size] = rasterize_size(font_path, size, cps)

    # 共享覆盖表：rcp = cp - range_start（uint16）
    range_start = cps[0]
    range_length = cps[-1] - cps[0] + 1
    if range_length > 65535:
        raise SystemExit(f"码点跨度过大 {range_length}（>65535，SPARSE_TINY 放不下）")
    rcp_list = [cp - range_start for cp in cps]

    stats = charset_stats(cps)
    provenance = f"""\
/*
 * {HEADER_NAME}.c — Noto Sans SC 子集字体（生成文件，勿手改；P2.4 集成，A6；
 *   wqy 点阵落地后收窄回项目文案口径，仅作 fallback，A2 2026-09-11）
 *
 * 生成器：scripts/gen_font_noto_sc.py（fonttools+Pillow 自研 LVGL fmt_txt 导出，
 *   零 npm 依赖）。再生成（版本锁定，禁止裸 --with fonttools）：
 *   uv run --python 3.12 --with fonttools==4.64.0 --with pillow==12.3.0 \\
 *   python scripts/gen_font_noto_sc.py
 * 字体真源：Noto Sans SC Regular v2.004（SIL OFL 1.1，授权允许嵌入分发）
 *   来源 github.com/notofonts/noto-cjk Sans/SubsetOTF/SC（下载留档 third_party/dl/）
 *   sha256 {EXPECTED_SHA256}
 * 生成工具版本：fonttools {fontTools.version} / Pillow {PIL.__version__}
 *   / FreeType {feats.version("freetype")}（栅格化仅在生成期，产物入库后固定；
 *   版本锁定见生成器 PINNED_*，漂移即报错）
 * 覆盖范围：U+{range_start:04X}–U+{cps[-1]:04X}，共 {len(cps)} 码点 =
 *   ASCII 0x20–0x7E（{stats["ascii"]}）
 *   + 项目文案全部非 ASCII（{stats["project_nonascii"]} 码点）。
 *   GB2312 汉字不再收录——wqy 点阵（cdt_font_wqy16/14）原生覆盖 GB2312 全部
 *   6763 汉字，本字体只兜 wqy 缺字形的符号码点（→ ∈ ≠ ≤ ≥ § × – — “ ” … 等）。
 * 边界（契约 §6「不静默缺字」）：未含字符——emoji、假名、生僻字等
 *   ——不以字形收录，cdt_ui_ascii_safe 折为可见 '?' 替代符呈现（S17 契约行为，
 *   非 bug；替代符本身不是本字体字形）。
 * 用法：仅作 cdt_font_wqy_16/14 的 lv_font_t.fallback（fallback 链
 *   wqy → 本字体 → 可见替代符，见 cdt_ui_internal.h F_*）。
 */
#include <stdint.h>

#include "lvgl.h"
#include "{HEADER_NAME}.h"
"""

    common_arrays = f"""\
/* ---- 覆盖表（两档字体共用；SPARSE_TINY unicode_list，rcp 升序）---- */
#define CDT_SC_RANGE_START ((uint32_t)0x{range_start:04X}u)
#define CDT_SC_RANGE_LENGTH ((uint16_t)0x{range_length:04X}u) /* = {range_length} */

static const uint16_t sc_unicode_list[] = {{
{fmt_u16(rcp_list)}
}};
"""

    def emit_font(size: int, tag: str) -> str:
        bitmaps, dscs, lh, bl = per_size[size]
        blob, offsets = pack_a4(bitmaps)
        parts = [f"""\
/* ---- {size}px（A4，line_height {lh}，base_line {bl}）---- */

static LV_ATTRIBUTE_LARGE_CONST const uint8_t glyph_bitmap_{tag}[] = {{
{fmt_bytes(blob)}
}};

/* glyph_dsc[0] 为保留占位（gid 0 = 未命中）；之后按码点升序一一对应 */
static const lv_font_fmt_txt_glyph_dsc_t glyph_dsc_{tag}[] = {{
    {{.bitmap_index = 0, .adv_w = 0, .box_w = 0, .box_h = 0, .ofs_x = 0, .ofs_y = 0}},"""]
        for cp, off, d in zip(cps, offsets, dscs):
            parts.append(
                "    /* U+%04X */ {.bitmap_index = %d, .adv_w = %u, .box_w = %u, "
                ".box_h = %u, .ofs_x = %d, .ofs_y = %d}," % (
                    cp, off, d["adv_w"], d["box_w"], d["box_h"], d["ofs_x"], d["ofs_y"]))
        parts.append(f"""\
}};

static const lv_font_fmt_txt_cmap_t cmaps_{tag}[] = {{
    {{
        .range_start = CDT_SC_RANGE_START, .range_length = CDT_SC_RANGE_LENGTH,
        .glyph_id_start = 1,
        .unicode_list = sc_unicode_list, .glyph_id_ofs_list = NULL,
        .list_length = {len(rcp_list)}, .type = LV_FONT_FMT_TXT_CMAP_SPARSE_TINY
    }},
}};

static const lv_font_fmt_txt_dsc_t font_dsc_{tag} = {{
    .glyph_bitmap = glyph_bitmap_{tag},
    .glyph_dsc = glyph_dsc_{tag},
    .cmaps = cmaps_{tag},
    .kern_dsc = NULL,
    .kern_scale = 0,
    .cmap_num = 1,
    .bpp = 4,
    .kern_classes = 0,
    .bitmap_format = 0,
    .stride = 0,
}};

const lv_font_t cdt_font_noto_sc_{size} = {{
    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,
    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,
    .line_height = {lh},
    .base_line = {bl},
    .subpx = LV_FONT_SUBPX_NONE,
    .kerning = LV_FONT_KERNING_NORMAL,
    .static_bitmap = 1,
    .underline_position = -1,
    .underline_thickness = 1,
    .dsc = &font_dsc_{tag},
    .fallback = NULL,
    .user_data = NULL,
}};
""")
        return "\n".join(parts)

    body = common_arrays + "".join(emit_font(s, f"{s}") for s in SIZES) + f"""\

/* ---- 码点覆盖查询（cdt_ui_ascii_safe 放行判断；二分） ---- */
bool {HEADER_NAME}_covers(uint32_t codepoint)
{{
    const uint32_t rcp = codepoint - CDT_SC_RANGE_START;
    int lo = 0, hi = (int)(sizeof(sc_unicode_list) / sizeof(sc_unicode_list[0])) - 1;

    if (codepoint < CDT_SC_RANGE_START ||
        rcp >= (uint32_t)CDT_SC_RANGE_LENGTH) return false;
    while (lo <= hi) {{
        int mid = lo + (hi - lo) / 2;
        uint16_t v = sc_unicode_list[mid];
        if (v == (uint16_t)rcp) return true;
        if (v < (uint16_t)rcp) lo = mid + 1;
        else hi = mid - 1;
    }}
    return false;
}}
"""

    header = f"""\
/*
 * {HEADER_NAME}.h — Noto Sans SC 子集字体（生成文件，勿手改）
 * 真源与范围见 {HEADER_NAME}.c 头注释；契约：docs/VERSIONS.md 字体行、
 * tests/SCENARIOS.md §5.3（固定字体并记录范围，不静默缺字）。
 */
#ifndef CDT_FONT_NOTO_SC_H
#define CDT_FONT_NOTO_SC_H

#include <stdint.h>

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {{
#endif

/* 14px / 16px 子集字体（A4；wqy 点阵 cdt_font_wqy_16/14 的行内回退备胎） */
extern const lv_font_t cdt_font_noto_sc_14;
extern const lv_font_t cdt_font_noto_sc_16;

/* 码点是否在子集覆盖范围内（cdt_ui_ascii_safe 放行 wqy 之外的符号码点） */
bool cdt_font_noto_sc_covers(uint32_t codepoint);

#ifdef __cplusplus
}}
#endif

#endif /* CDT_FONT_NOTO_SC_H */
"""

    print(
        f"[font] 字符集来源分布：ASCII {stats['ascii']} + 项目文案非 ASCII "
        f"{stats['project_nonascii']}（GB2312 一级 0——wqy 点阵落地后收窄为纯 fallback）")
    blob_bytes = {s: len(pack_a4(per_size[s][0])[0]) for s in SIZES}
    print(f"[font] 合计覆盖 {stats['total']} 码点（U+{range_start:04X}–U+{cps[-1]:04X}，"
          f"跨区 {range_length}）；A4 位图 14px {blob_bytes[14]} B / "
          f"16px {blob_bytes[16]} B（未含 dsc/cmap 表）")
    return provenance + body, header


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--font", type=Path, default=DEFAULT_FONT)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--check", action="store_true",
                    help="与已生成文件逐字节比对，不写入")
    args = ap.parse_args()

    data = args.font.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha != EXPECTED_SHA256:
        print(f"字体 sha256 不符：{sha}\n  期望 {EXPECTED_SHA256}", file=sys.stderr)
        return 2

    c_src, h_src = generate(args.font, args.out_dir)
    targets = [(args.out_dir / f"{HEADER_NAME}.c", c_src),
               (args.out_dir / f"{HEADER_NAME}.h", h_src)]
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
