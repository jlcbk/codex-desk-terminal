#!/bin/sh
# build_shared.sh — P1.4 共享模块主机端构建+测试（A0）
# 只依赖 Apple cc；产物 build/shared/test_shared。
# 退出码 0 = 全部通过。
set -e
cd "$(dirname "$0")/.."

OUT=build/shared
mkdir -p "$OUT"

echo "[1/3] 编译共享模块与测试"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ishared/state -Ishared/display -Ishared/presenter \
   shared/state/cdt_json.c shared/state/cdt_parser.c shared/state/cdt_store.c \
   shared/display/cdt_frame.c \
   tests/shared/test_main.c \
   -o "$OUT/test_shared"

echo "[2/3] 运行内置用例 + 协议 fixtures（F01–F15 复用 P0.4 产物）"
FIXTURES=""
for f in tests/fixtures/protocol/valid_*.json tests/fixtures/protocol/invalid_*.json tests/fixtures/protocol/*.bin; do
    [ -f "$f" ] && FIXTURES="$FIXTURES $f"
done
# shellcheck disable=SC2086
"$OUT/test_shared" $FIXTURES

echo "[3/3] P1 Gate 预检：共享模块不得包含 ESP-IDF/SDL 头"
if grep -rn "esp_|ESP_|SDL2/SDL|lvgl" shared/ --include='*.c' --include='*.h'; then
    echo "FAIL: 共享模块发现平台头泄漏" >&2
    exit 1
fi
echo "build_shared: 全部通过"
