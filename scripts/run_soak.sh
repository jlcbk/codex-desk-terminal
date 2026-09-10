#!/bin/sh
# run_soak.sh — P6.2 PC 前置压缩 soak 入口（A5）。
# 真实退出码：任一阶段失败 → 本脚本退出码 1；环境错误 → 2（soak_bridge.py 上抛）。
# 用法：sh scripts/run_soak.sh [soak_bridge.py 公共参数，如 --rounds 500 --sim-rounds 100]
# 预计总耗时：默认轮数下约 3–5 分钟（远小于 24h；确定性系统允许时钟压缩）。
set -u
cd "$(dirname "$0")/.." || exit 2

# 幂等预检：uv 不可用直接环境错误
command -v uv >/dev/null 2>&1 || { echo "环境错误: 需要 uv（https://docs.astral.sh/uv/）" >&2; exit 2; }

run_py() {
  uv run --python 3.12 --with psutil python "$@"
}

T0=$(date +%s)
RC=0
for PHASE in bridge bridge-inproc mock-c sim checkui; do
  echo "=============================================================="
  echo "SOAK 阶段: $PHASE"
  echo "=============================================================="
  run_py scripts/soak_bridge.py "$PHASE" "$@"
  RC_PHASE=$?
  echo "SOAK 阶段 $PHASE 退出码: $RC_PHASE"
  if [ "$RC_PHASE" -ne 0 ]; then
    RC=1
  fi
done

echo "=============================================================="
echo "SOAK 汇总报告"
echo "=============================================================="
run_py scripts/soak_bridge.py report
RC_PHASE=$?
echo "SOAK 阶段 report 退出码: $RC_PHASE"
if [ "$RC_PHASE" -ne 0 ]; then
  RC=1
fi

T1=$(date +%s)
echo "SOAK 总耗时: $((T1 - T0))s；最终退出码: $RC"
exit "$RC"
