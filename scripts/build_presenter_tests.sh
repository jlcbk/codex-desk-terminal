#!/bin/sh
# build_presenter_tests.sh — P2.1 (A2) Presenter 主机端测试 + P2.2/P2.3 页面/导航测试
# 只依赖 Apple cc；产物 build/presenter_tests/test_presenter、build/presenter_tests/test_pages。
# 退出码 0 = 全部通过。
# （单元测试独立入口；共享模块整体构建+协议 fixtures 仍走 A0 的 build_shared.sh）
set -e
cd "$(dirname "$0")/.."

OUT=build/presenter_tests
mkdir -p "$OUT"

echo "[1/4] 编译 presenter 纯转换测试（P2.1 基线，不改）"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ishared/state -Ishared/presenter \
   shared/presenter/cdt_presenter.c \
   tests/shared/test_presenter.c \
   -o "$OUT/test_presenter"

echo "[2/4] 运行 presenter 用例（优先级 / -- 显示 / fresh-frozen 计时 / 截断）"
"$OUT/test_presenter"

echo "[3/4] 编译并运行 P2.2/P2.3 页面与导航测试（AGENTS 排序分页 / PLAN 计数 / \
USAGE 窗口与倒计时 / 低压强制页 / 恢复顺序 / KEY 导航纯逻辑）"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ishared/state -Ishared/presenter -Ishared/ui \
   shared/presenter/cdt_presenter.c \
   shared/ui/cdt_nav.c \
   tests/shared/test_pages.c \
   -o "$OUT/test_pages"
"$OUT/test_pages"

echo "[4/4] 门禁：presenter 与 cdt_nav 不得 include LVGL/SDL/ESP 头（注释中提及允许）"
if grep -rn "esp_|ESP_" shared/presenter/ --include='*.c' --include='*.h' \
   || grep -rn "#include" shared/presenter/ --include='*.c' --include='*.h' \
      | grep -Ei "lvgl|SDL|esp"; then
    echo "FAIL: presenter 发现平台头泄漏" >&2
    exit 1
fi
# cdt_nav 是 shared/ui 中的纯逻辑模块（portable 测试直接编译），同样禁止平台头；
# 其余 shared/ui 页面模块允许 lvgl（build_shared.sh 的分层门禁覆盖）。
if grep -rEn '#[[:space:]]*include' shared/ui/cdt_nav.c shared/ui/cdt_nav.h \
      | grep -Ei "lvgl|SDL|esp|freertos"; then
    echo "FAIL: cdt_nav 发现平台头泄漏" >&2
    exit 1
fi
echo "build_presenter_tests: 全部通过"
