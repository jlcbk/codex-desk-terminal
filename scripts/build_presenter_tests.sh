#!/bin/sh
# build_presenter_tests.sh — P2.1 (A2) Presenter 主机端测试
# 只依赖 Apple cc；产物 build/presenter_tests/test_presenter。
# 退出码 0 = 全部通过。
# （P2.1 单元测试独立入口；共享模块整体构建+协议 fixtures 仍走 A0 的 build_shared.sh）
set -e
cd "$(dirname "$0")/.."

OUT=build/presenter_tests
mkdir -p "$OUT"

echo "[1/3] 编译 presenter 纯转换测试"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ishared/state -Ishared/presenter \
   shared/presenter/cdt_presenter.c \
   tests/shared/test_presenter.c \
   -o "$OUT/test_presenter"

echo "[2/3] 运行 presenter 用例（优先级 / -- 显示 / fresh-frozen 计时 / 截断）"
"$OUT/test_presenter"

echo "[3/3] 门禁：presenter 不得 include LVGL/SDL/ESP 头（注释中提及允许）"
if grep -rn "esp_|ESP_" shared/presenter/ --include='*.c' --include='*.h' \
   || grep -rn "#include" shared/presenter/ --include='*.c' --include='*.h' \
      | grep -Ei "lvgl|SDL|esp"; then
    echo "FAIL: presenter 发现平台头泄漏" >&2
    exit 1
fi
echo "build_presenter_tests: 全部通过"
