#!/bin/sh
# build_shared.sh — 共享模块主机端构建+测试
# P1.4 共享模块（A0）；P5.1 追加 Power FSM 测试（A3，仅追加不改动原步骤）。
# 只依赖 Apple cc；产物 build/shared/test_shared、build/shared/test_power。
# 退出码 0 = 全部通过。
set -e
cd "$(dirname "$0")/.."

OUT=build/shared
mkdir -p "$OUT"

echo "[1/5] 编译共享模块与测试"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ishared/state -Ishared/display -Ishared/presenter \
   shared/state/cdt_json.c shared/state/cdt_parser.c shared/state/cdt_store.c \
   shared/display/cdt_frame.c \
   tests/shared/test_main.c \
   -o "$OUT/test_shared"

echo "[2/5] 运行内置用例 + 协议 fixtures（F01–F15 复用 P0.4 产物）"
FIXTURES=""
for f in tests/fixtures/protocol/valid_*.json tests/fixtures/protocol/invalid_*.json tests/fixtures/protocol/*.bin; do
    [ -f "$f" ] && FIXTURES="$FIXTURES $f"
done
# shellcheck disable=SC2086
"$OUT/test_shared" $FIXTURES

echo "[3/5] 编译 Power FSM 测试（P5.1：shared/power 纯逻辑，复用 presenter 枚举）"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ishared/power -Ishared/presenter \
   shared/power/cdt_power.c \
   tests/shared/test_power.c \
   -o "$OUT/test_power"

echo "[4/5] 运行 Power FSM 用例（§7 边界/持续低压/反弹/缺测/故障）"
"$OUT/test_power"

echo "[5/5] Gate 预检：共享模块不得包含平台头"
# A0 分层门禁裁决（2026-09-10）：
#   portable 层（state/display/presenter/power）：禁 esp_/ESP_/SDL/lvgl（不得 include LVGL）；
#   UI 层（shared/ui）：允许 lvgl（页面层职责），但仍禁 esp_/SDL（保持 PC 可编译）。
# 注意必须用 grep -E——BRE 下 | 是字面量，检查会静默空转（曾发生两次，勿再犯）。
# 只检查 #include 行（注释中提及平台名是合法的文档说明）。
GATE_RE='#[[:space:]]*include[[:space:]]*[<"](esp_|ESP_|SDL|lvgl|freertos|FreeRTOS)'
if grep -rEn "$GATE_RE" shared/state shared/display shared/presenter shared/power --include='*.c' --include='*.h'; then
    echo "FAIL: portable 共享模块发现平台头/LVGL include 泄漏" >&2
    exit 1
fi
if grep -rEn "$GATE_RE" shared/ui --include='*.c' --include='*.h' | grep -Ev 'include[[:space:]]*[<"]lvgl'; then
    echo "FAIL: shared/ui 发现 ESP-IDF/SDL/FreeRTOS include 泄漏（仅 lvgl 允许）" >&2
    exit 1
fi
echo "build_shared: 全部通过"
