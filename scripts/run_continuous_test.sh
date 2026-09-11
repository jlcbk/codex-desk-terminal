#!/bin/sh
# run_continuous_test.sh — R6 同实例持续编排测试入口（A5，PC 级，纯软件，不碰板）。
#
# 运行 tests/integration/test_continuous_orchestration.py：同一 StateEngine
# （不重建）+ 同一 WSS server（真实现，临时端口，**不用 8765**——P5.2 域在用）
# + 同一订户（client_mock 真实现）连续经历故障时间线（桥重启×3、断连退避重连×5、
# 重复 seq 洪泛、乱序、审批 pending→resolve、上游断连、S09 类电池 trace 独立
# 上行、usage 更新），断言状态收敛 / seq/epoch 无回退 / 计数有界 / 字节对账 /
# RSS（复用 soak 判据口径）。
#
# 真实退出码：0 全部通过；1 断言失败（pytest 原样上抛）；2 环境错误。
# 产物：artifacts/continuous/{timeline.json, report.md, run_log.txt}。
# 依赖：uv（自动拉取 python3.12 + websockets==17.1 + pytest + jsonschema + psutil，
# 与 docs/VERSIONS.md 锁定一致）。
#
# 用法：sh scripts/run_continuous_test.sh [pytest 额外参数，如 -k 名称]
# 预计耗时：约 30–90 s（虚拟时钟 ≥10 分钟等效，墙钟压缩运行）。
set -u
cd "$(dirname "$0")/.." || exit 2

command -v uv >/dev/null 2>&1 || {
  echo "环境错误: 需要 uv（https://docs.astral.sh/uv/）" >&2
  exit 2
}

mkdir -p artifacts/continuous
LOG=artifacts/continuous/run_log.txt
OUT=artifacts/continuous/.step_output.txt
: > "$LOG"

log() { echo "$@" | tee -a "$LOG"; }

log "== R6 同实例持续编排测试（PC 级；固件 main 同实例持续验证归 P6.2 24h 真机 soak） =="

T0=$(date +%s)
if uv run --python 3.12 \
       --with 'websockets==17.1' --with pytest --with jsonschema --with psutil \
       python -m pytest tests/integration/test_continuous_orchestration.py -v "$@" \
       > "$OUT" 2>&1; then
  RC=0
else
  RC=$?
fi
T1=$(date +%s)

cat "$OUT" | tee -a "$LOG"

log "== R6 汇总 =="
log "pytest 退出码: ${RC}；墙钟耗时: $((T1 - T0))s"
log "证据: artifacts/continuous/timeline.json + report.md（覆盖矩阵/内存/对账）"
if [ "$RC" -eq 0 ]; then
  log "[R6][OK] 同实例持续编排测试通过（exit 0）"
  exit 0
fi
log "[R6][FAIL] 同实例持续编排测试失败（exit ${RC}；见上方失败断言与 timeline.json）"
exit "$RC"
