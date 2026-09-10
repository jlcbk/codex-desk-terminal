#!/bin/sh
# run_p35.sh — P3.5 共同故障集成测试入口（A4+A5，host，无真机）
#
# 一次跑全部集成用例（tests/transport/integration/）：
#   [1/2] cc 编译设备侧终点 harness（真实 cdt_reassembler + cdt_state_store +
#         cdt_present；与 scripts/build_transport_tests.sh 同警告参数）
#   [2/2] uv 运行故障矩阵 pytest（重复/乱序/超时/Bridge 重启/切换 transport/
#         链路可观测；WSS 用例走 127.0.0.1 真实 socket，websockets==17.1 锁定）
#
# 真实退出码汇总：各步骤输出经中间文件落盘后取真实退出码，末尾打印汇总；
# 任一步失败则脚本退出码 1。证据写入 artifacts/transport/p35/（逐用例子目录
# + run_log.txt）。依赖：Apple cc、uv（自动拉取 python3.12 + websockets==17.1
# + pytest）。
set -u
cd "$(dirname "$0")/.."

mkdir -p artifacts/transport/p35
LOG=artifacts/transport/p35/run_log.txt
OUT=artifacts/transport/p35/.step_output.txt
: > "$LOG"

fail=0

log() { echo "$@" | tee -a "$LOG"; }

log "== P3.5 共同故障集成测试（host；真机项未验证，见任务报告） =="

log "[1/2] cc 编译终点 harness -> build/transport/p35/test_p35_harness"
mkdir -p build/transport/p35
if cc -std=c99 -Wall -Wextra -Werror -pedantic \
      -Ishared/transport -Ishared/state -Ishared/presenter \
      shared/transport/cdt_crc32.c \
      shared/transport/cdt_fragmenter.c \
      shared/transport/cdt_reassembler.c \
      shared/state/cdt_json.c \
      shared/state/cdt_parser.c \
      shared/state/cdt_store.c \
      shared/presenter/cdt_presenter.c \
      tests/transport/integration/test_p35_harness.c \
      -o build/transport/p35/test_p35_harness > "$OUT" 2>&1 \
   && [ -x build/transport/p35/test_p35_harness ]; then
    log "[P35][OK] harness 编译（exit 0）"
else
    cat "$OUT" | tee -a "$LOG"
    log "[P35][FAIL] harness 编译（编译器退出码非 0 或产物缺失）"
    fail=1
fi

log "[2/2] uv 运行故障矩阵 pytest（tests/transport/integration）"
if uv run --python 3.12 --with 'websockets==17.1' --with pytest \
       python -m pytest tests/transport/integration -v > "$OUT" 2>&1; then
    cat "$OUT" | tee -a "$LOG"
    log "[P35][OK] 故障矩阵 pytest（exit 0）"
else
    rc=$?
    cat "$OUT" | tee -a "$LOG"
    # 注意：本机 /bin/sh 为 bash 3.2，$var 后紧邻多字节 UTF-8 字符会被并入
    # 变量名（unbound variable）；凡与全角标点相邻的变量一律加花括号。
    log "[P35][FAIL] 故障矩阵 pytest（exit ${rc}）"
    fail=1
fi

log "== P3.5 汇总 =="
if [ "$fail" -eq 0 ]; then
    log "全部通过（harness 编译 + 故障矩阵 pytest，退出码 0）"
    exit 0
fi
log "存在失败步骤（见上方 [P35][FAIL] 行与 ${LOG}）"
exit 1
