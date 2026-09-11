#!/bin/sh
# build_firmware_io_tests.sh — P4.4/P4.5 固件 IO 组件纯逻辑主机端构建+测试（A3）
#
# 编译并运行两组件的纯逻辑层单测（零 esp 头，cc 直接编译）：
#   tests/firmware/test_battery_pure.c — 电池：中位数/分压/节奏/范围/校准同源/
#                                        批次→FSM 样本（含"校准不预乘"契约）
#   tests/firmware/test_key_pure.c     — 按键：去抖 30ms/长按 800ms 边界/
#                                        松开时判定/长按不补短按/按住不重复
# 依赖 shared/power/cdt_power.c（FSM 同源校准公式 cdt_power_effective_mv——
# 由构造保证单一公式真源，不靠注释约定）。
# 只依赖 Apple cc；产物 build/firmware_io/test_*。
# 退出码 0 = 全部通过（含纯逻辑层平台头门禁）。
set -e
cd "$(dirname "$0")/.."

OUT=build/firmware_io
mkdir -p "$OUT"

echo "[1/4] 编译电池纯逻辑测试（+ shared/power FSM 同源校准）"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ishared/power -Ifirmware/components/battery/include \
   firmware/components/battery/cdt_battery_pure.c \
   shared/power/cdt_power.c \
   tests/firmware/test_battery_pure.c \
   -o "$OUT/test_battery_pure"

echo "[2/4] 编译按键纯逻辑测试"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ifirmware/components/input/include \
   firmware/components/input/cdt_key_pure.c \
   tests/firmware/test_key_pure.c \
   -o "$OUT/test_key_pure"

echo "[3/4] 运行用例（真实退出码）"
"$OUT/test_battery_pure"
"$OUT/test_key_pure"

echo "[4/4] Gate 预检：固件纯逻辑层不得包含平台头"
# 分层门禁（同 build_shared.sh/build_transport_tests.sh 裁决）：portable 层
# 禁 esp_/ESP_/SDL/lvgl/freertos/FreeRTOS/driver。注意必须用 grep -E——BRE 下
# | 是字面量，检查会静默空转。只检查 #include 行（注释提及平台名合法）。
GATE_RE='#[[:space:]]*include[[:space:]]*[<"](esp_|ESP_|SDL|lvgl|freertos|FreeRTOS|driver/)'
if grep -rEn "$GATE_RE" \
    firmware/components/battery/cdt_battery_pure.c \
    firmware/components/battery/include/cdt_battery_pure.h \
    firmware/components/input/cdt_key_pure.c \
    firmware/components/input/include/cdt_key_pure.h; then
    echo "FAIL: 固件纯逻辑层发现平台头 include 泄漏" >&2
    exit 1
fi
echo "build_firmware_io_tests: 全部通过"
