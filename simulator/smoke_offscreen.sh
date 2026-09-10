#!/bin/sh
# smoke_offscreen.sh — P1.3 (A2)
# 离屏冒烟：SDL dummy 视频驱动运行模拟器 N 毫秒后自动退出，验证 CI 宿主可无头执行。
# 验收：退出码 0；日志写入 artifacts/sim/smoke_offscreen.log。
#
# 用法：simulator/smoke_offscreen.sh
# 可覆盖：SIM_AUTO_QUIT_MS（默认 2000）、SIM_CAPTURE_PATH（默认 artifacts/sim/p1.3_offscreen_frame.bmp）
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/build/simulator/codex-display-sim"
ART="$ROOT/artifacts/sim"
LOG="$ART/smoke_offscreen.log"

if [ ! -x "$BIN" ]; then
  echo "[smoke] ERROR: $BIN 不存在，请先运行 scripts/build_simulator.sh" >&2
  exit 1
fi
mkdir -p "$ART"

AUTO_MS="${SIM_AUTO_QUIT_MS:-2000}"
CAP="${SIM_CAPTURE_PATH:-$ART/p1.3_offscreen_frame.bmp}"

echo "[smoke] SDL_VIDEODRIVER=dummy SIM_AUTO_QUIT_MS=${AUTO_MS} $BIN"

set +e
SDL_VIDEODRIVER=dummy SIM_AUTO_QUIT_MS="$AUTO_MS" SIM_CAPTURE_PATH="$CAP" "$BIN" >"$LOG" 2>&1
rc=$?
set -e

cat "$LOG"
echo "[smoke] exit code: $rc"
exit "$rc"
