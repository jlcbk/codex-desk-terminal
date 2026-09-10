#!/bin/sh
# build_simulator.sh — P1.3 (A2)
# 一键构建模拟器：vendor LVGL -> 构建/安装 SDL2 -> cmake configure -> build。
# 产物：build/simulator/codex-display-sim（与 docs/DEVELOPMENT_PLAN.md §8 命令接口一致）。
# 构建日志写入 artifacts/sim/build_simulator.log。
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$ROOT/build/simulator"
ART="$ROOT/artifacts/sim"

log() { printf '[build_sim] %s\n' "$*"; }

ensure_cmake() {
  if command -v cmake >/dev/null 2>&1; then return; fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
  if command -v cmake >/dev/null 2>&1; then return; fi
  if command -v uv >/dev/null 2>&1; then
    log "installing cmake 3.31.6 via uv tool"
    uv tool install "cmake==3.31.6" || uv tool upgrade "cmake==3.31.6"
    PATH="$HOME/.local/bin:$PATH"
    export PATH
  else
    echo "[build_sim] ERROR: cmake missing and uv not found" >&2
    exit 1
  fi
}

ensure_cmake
log "cmake: $(cmake --version | head -n 1)"

log "step 1/3: vendor LVGL (locked commit)"
sh "$ROOT/scripts/vendor_lvgl.sh"

log "step 2/3: build/install SDL2 (locked tag)"
sh "$ROOT/scripts/build_sdl2.sh"

log "step 3/3: cmake configure + build"
mkdir -p "$ART"
BUILD_LOG="$ART/build_simulator.log"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"

set +e
{
  cmake -S "$ROOT/simulator" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release &&
  cmake --build "$BUILD_DIR" --parallel "$JOBS"
} >"$BUILD_LOG" 2>&1
rc=$?
set -e
cat "$BUILD_LOG"
if [ "$rc" -ne 0 ]; then
  echo "[build_sim] ERROR: build failed (exit $rc), log: $BUILD_LOG" >&2
  exit "$rc"
fi

log "OK: $BUILD_DIR/codex-display-sim"
