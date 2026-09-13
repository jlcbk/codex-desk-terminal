#!/bin/sh
# build_power_tests.sh — P5.3 电源执行器 + ZC9 空闲降档 主机端构建+测试（A3/A9）
#
# 独立脚本：不追加/不改 build_firmware_io_tests.sh（P4.4/P4.5 可能正在写该
# 文件，避免并行冲突）；本脚本只编译电源域文件。
#
# 编译并运行（cc 直接编译，零 cmake/IDF 依赖）：
#   变体 A  tests/firmware/test_power_exec_pure.c（KEY 深睡唤醒宏=头文件默认 0）
#           + firmware/components/power/cdt_power_exec.c 真实编译进宿主
#           （esp 头经 tests/firmware/power_exec_stubs/ 桩替换，链接桩计数断言：
#            宏关闭时全程零 esp_sleep 唤醒配置调用）
#   变体 B  同源码 -DCDT_CFG_KEY_DEEP_WAKE=1（仅测试变体；生产启用条件 =
#           P5.4 真机实测通过，docs/HARDWARE.md §4.2）
# 同时编入 shared/power/cdt_power.c（P5.1 冻结 FSM，CRITICAL 场景驱动）。
#   ZC9（P5.2 后半，2026-09-13）tests/firmware/test_power_idle_pure.c：
#           空闲动态降档纯决策（firmware/main/app_power_idle.c 单源编译），
#           覆盖未到/过阈值、活动立即回 MIN、迟滞窗、阈值边界、无效时间戳。
# 退出码 0 = 全部通过（含纯逻辑层平台头门禁）。
set -e
cd "$(dirname "$0")/.."

OUT=build/power_exec_tests
mkdir -p "$OUT"
STUB=tests/firmware/power_exec_stubs
INC="-Ishared/power -Ifirmware/components/power/include -I$STUB"
SRCS="firmware/components/power/cdt_power_exec_pure.c \
firmware/components/power/cdt_power_exec.c \
shared/power/cdt_power.c \
$STUB/esp_stub.c"

echo "[1/5] 编译变体 A：KEY 深睡唤醒宏=默认关闭（链接桩计数断言零唤醒配置调用）"
cc -std=c99 -Wall -Wextra -Werror -pedantic $INC \
   $SRCS tests/firmware/test_power_exec_pure.c \
   -o "$OUT/test_power_exec_pure"

echo "[2/5] 编译变体 B：-DCDT_CFG_KEY_DEEP_WAKE=1（P5.4 实测门禁通过前不得用于生产）"
cc -std=c99 -Wall -Wextra -Werror -pedantic $INC -DCDT_CFG_KEY_DEEP_WAKE=1 \
   $SRCS tests/firmware/test_power_exec_pure.c \
   -o "$OUT/test_power_exec_keywake"

echo "[3/5] 编译+运行 ZC9 空闲降档纯决策测试（app_power_idle.c 单源）"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ifirmware/main \
   firmware/main/app_power_idle.c \
   tests/firmware/test_power_idle_pure.c \
   -o "$OUT/test_power_idle_pure"

echo "[4/5] 运行用例（真实退出码）"
"$OUT/test_power_exec_pure"
"$OUT/test_power_exec_keywake"
"$OUT/test_power_idle_pure"

echo "[5/5] Gate 预检：电源纯逻辑层不得包含平台头"
# 分层门禁（同 build_shared.sh/build_firmware_io_tests.sh 裁决）：portable 层
# 禁 esp_/ESP_/SDL/lvgl/freertos/FreeRTOS/driver。必须 grep -E（BRE 下 | 是
# 字面量会静默空转）。只检查 #include 行（注释提及平台名合法）。
GATE_RE='#[[:space:]]*include[[:space:]]*[<"](esp_|ESP_|SDL|lvgl|freertos|FreeRTOS|driver/)'
if grep -rEn "$GATE_RE" \
    firmware/components/power/cdt_power_exec_pure.c \
    firmware/components/power/include/cdt_power_exec_pure.h \
    firmware/main/app_power_idle.c \
    firmware/main/app_power_idle.h; then
    echo "FAIL: 电源纯逻辑层发现平台头 include 泄漏" >&2
    exit 1
fi
echo "build_power_tests: 全部通过"
