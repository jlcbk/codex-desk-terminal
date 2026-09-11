#!/bin/sh
# run_integration_fix_tests.sh — 审核修复（R3a/R3b/R4）主机端构建+测试
#
# 审核真源：docs/ARCHITECTURE_REVIEW_2026-09-11.md R3a/R3b/R4。
# 独立脚本（A3+A4 集成缺陷修复轮）：不追加/不改既有域脚本，避免并行冲突。
#
# 编译并运行（cc 直接编译，零 cmake/IDF 依赖；退出码 0 = 全部通过）：
#   [1/4] R3a/R3b 策略模型：真实 shared/power FSM + main 电源策略单源
#         （firmware/main/app_power_policy.h，main.c 与测试同时 include）
#         — 低压冷启动睡眠恰执行一次、DEEP_SLEEP_READY 掩码、ADC 整批失败
#         invalid@now 进 FSM → 3 销 BATTERY_FAULT → 受控休眠真实执行。
#   [2/4] R4 收件槽（真实 firmware/main/app_inbox.c，-DCDT_APP_INBOX_HOST
#         用 pthread 锁编译同一交接逻辑）+ 真实 shared/state 解析链：
#         优先提醒槽语义（attention→done 不丢提醒、重复 seq 不顶掉提醒、
#         呈现后槽让位、dropped 计数）+ 分类器 + 守恒。
#   [3/4] 同测试 TSan 变体：并发压力（生产者线程 + 延迟消费者）数据竞争检测。
#   [4/4] Gate 预检：main.c 必须接入 app_inbox/app_power_policy（单源不漂移）；
#         app_inbox.c 已列入固件构建（firmware/main/CMakeLists.txt）。
set -e
cd "$(dirname "$0")/../.."

OUT=build/integration_fix_tests
mkdir -p "$OUT"

echo "[1/4] 编译 R3a/R3b 电源策略模型测试（真实 FSM + app_power_policy.h 单源）"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ishared/power -Ishared/presenter -Ifirmware/main \
   shared/power/cdt_power.c \
   tests/firmware/test_main_power_policy.c \
   -o "$OUT/test_main_power_policy"

echo "[2/4] 编译+运行 R4 收件槽交接/优先槽测试（真实 app_inbox.c + shared/state）"
cc -std=c99 -Wall -Wextra -Werror -pedantic -DCDT_APP_INBOX_HOST \
   -Ishared/state -Ifirmware/main \
   firmware/main/app_inbox.c \
   shared/state/cdt_json.c shared/state/cdt_parser.c shared/state/cdt_store.c \
   tests/firmware/test_app_inbox.c \
   -o "$OUT/test_app_inbox"

echo "[3/4] R4 并发压力 TSan 变体（数据竞争门禁）"
cc -std=c99 -Wall -Wextra -Werror -pedantic -fsanitize=thread -g -O1 \
   -DCDT_APP_INBOX_HOST \
   -Ishared/state -Ifirmware/main \
   firmware/main/app_inbox.c \
   shared/state/cdt_json.c shared/state/cdt_parser.c shared/state/cdt_store.c \
   tests/firmware/test_app_inbox.c \
   -o "$OUT/test_app_inbox_tsan"

"$OUT/test_main_power_policy"
"$OUT/test_app_inbox"
"$OUT/test_app_inbox_tsan"

echo "[4/4] Gate 预检：main 接线单源与固件构建清单"
# main.c 必须直接 include 两份单源头/模块头（防止测试钉住的策略与固件脱钩）：
grep -q '#include "app_power_policy.h"' firmware/main/main.c ||
    { echo "FAIL: main.c 未接入 app_power_policy.h（R3a/R3b 策略单源被绕开）" >&2; exit 1; }
grep -q '#include "app_inbox.h"' firmware/main/main.c ||
    { echo "FAIL: main.c 未接入 app_inbox.h（R4 收件槽被绕开）" >&2; exit 1; }
grep -q '"app_inbox.c"' firmware/main/CMakeLists.txt ||
    { echo "FAIL: app_inbox.c 未列入固件构建" >&2; exit 1; }
echo "run_integration_fix_tests: 全部通过"
