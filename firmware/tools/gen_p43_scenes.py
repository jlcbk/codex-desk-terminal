#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_p43_scenes.py — P4.3 固件内嵌场景 AppState 的 C 头文件生成（A2+A3）。

真源只读：tests/fixtures/scenarios/S*.jsonl（golden 同源回放）与
tests/fixtures/protocol/valid_full.json（协议 fixture）。本脚本不改任何 fixture；
只把 payload 原样重序列化为紧凑单行 JSON（语义等价，shared/state 解析结果
与模拟器 Mac 侧对齐脚本读到的完全一致），转义后编成 C 字符串。

用法：python3 firmware/tools/gen_p43_scenes.py     # 写 firmware/main/p43_scenes.h
退出码：0 成功；1 输入缺失/不合法。
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCEN_DIR = REPO / "tests" / "fixtures" / "scenarios"
PROTO_DIR = REPO / "tests" / "fixtures" / "protocol"
OUT = REPO / "firmware" / "main" / "p43_scenes.h"


def payload_of(jsonl_name: str, tag: str) -> dict:
    path = SCEN_DIR / jsonl_name
    if not path.is_file():
        raise SystemExit(f"{path} 不存在")
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        obj = json.loads(line)
        if obj.get("tag") == tag and obj.get("action") == "app_state":
            return obj["payload"]
    raise SystemExit(f"{jsonl_name} 中没有 tag={tag} 的 app_state 行")


def compact(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def c_literal(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


SCENES = [
    ("P43_SCENE_IDLE_JSON", payload_of("S01_idle.jsonl", "idle")),
    ("P43_SCENE_WORKING_JSON", payload_of("S03_working.jsonl", "working")),
    ("P43_SCENE_NEEDS_YOU_JSON", payload_of("S05_needs_you.jsonl", "needs_you")),
    ("P43_SCENE_USAGE0_JSON", payload_of("S19_usage_0.jsonl", "online")),
    ("P43_SCENE_USAGE100_JSON", payload_of("S20_usage_100.jsonl", "online")),
    ("P43_SCENE_VALID_FULL_JSON",
     json.loads((PROTO_DIR / "valid_full.json").read_text(encoding="utf-8"))),
]


def main() -> int:
    lines = [
        "/*",
        " * p43_scenes.h — P4.3 固件内嵌场景 AppState（由 firmware/tools/gen_p43_scenes.py",
        " * 生成，勿手改）。来源（只读真源）：",
        " *   S01_idle/S03_working/S05_needs_you/S19_usage_0/S20_usage_100 的 app_state",
        " *   payload（tests/fixtures/scenarios，与 golden 同源）+",
        " *   tests/fixtures/protocol/valid_full.json（协议 fixture，覆盖 CJK 与",
        " *   needs_you+plan 混合形态）。",
        " * 重序列化仅做紧凑化（语义等价）；两端（固件/模拟器对齐脚本）解析同一文本。",
        " */",
        "#ifndef P43_SCENES_H",
        "#define P43_SCENES_H",
        "",
    ]
    for name, payload in SCENES:
        text = compact(payload)
        if len(text.encode("utf-8")) > 16384:
            raise SystemExit(f"{name} 超过 CDT_STATE_JSON_MAX_BYTES")
        lines.append(f"/* {len(text.encode('utf-8'))} bytes UTF-8 */")
        lines.append(f"static const char {name}[] = {c_literal(text)};")
        lines.append("")
    lines.append("#endif /* P43_SCENES_H */")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, _ in SCENES:
        print(f"[gen] {name}")
    print(f"[gen] wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
