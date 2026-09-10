#!/bin/sh
# build_sdl2.sh — P1.3 (A2)
# 从源码构建 docs/VERSIONS.md 锁定的 SDL2 release-2.30.12，安装到 third_party/sdl2-install/。
# 无 Homebrew：cmake 缺失时用 `uv tool install cmake`（固定 3.31.6，由 A0 记入 VERSIONS.md）。
# 可重复执行：tarball 缓存于 third_party/dl/，已安装则校验后跳过构建。
# 每次运行都打印 tarball sha256。
set -eu

SDL_TAG="release-2.30.12"
SDL_COMMIT="8236e01a9f758d15927624925c6043f84d8a261f"
URL="https://github.com/libsdl-org/SDL/archive/refs/tags/${SDL_TAG}.tar.gz"
CMAKE_PIN="3.31.6"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DL_DIR="$ROOT/third_party/dl"
SRC_DIR="$ROOT/third_party/SDL-${SDL_TAG}-src"
BUILD_DIR="$ROOT/third_party/SDL-${SDL_TAG}-build"
PREFIX="$ROOT/third_party/sdl2-install"
TARBALL="$DL_DIR/SDL-${SDL_TAG}.tar.gz"

log() { printf '[build_sdl2] %s\n' "$*"; }
fail() { printf '[build_sdl2] ERROR: %s\n' "$*" >&2; exit 1; }

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    fail "need shasum or sha256sum"
  fi
}

nproc_of() {
  if command -v sysctl >/dev/null 2>&1; then sysctl -n hw.ncpu; else getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2; fi
}

ensure_cmake() {
  if command -v cmake >/dev/null 2>&1; then
    log "cmake found: $(cmake --version | head -n 1)"
    return
  fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
  if command -v cmake >/dev/null 2>&1; then
    log "cmake found (uv): $(cmake --version | head -n 1)"
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    log "installing cmake ${CMAKE_PIN} via uv tool (no Homebrew)"
    uv tool install "cmake==${CMAKE_PIN}" || uv tool upgrade "cmake==${CMAKE_PIN}" || fail "uv tool install cmake failed"
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    command -v cmake >/dev/null 2>&1 || fail "cmake not on PATH after uv install"
    log "cmake installed: $(cmake --version | head -n 1)"
  else
    fail "cmake missing and uv not found (looked for \$HOME/.local/bin/uv)"
  fi
}

mkdir -p "$DL_DIR"

# 1) tarball（防重复下载）
if [ ! -f "$TARBALL" ]; then
  log "downloading $URL"
  curl -fSL --retry 3 --retry-delay 2 -o "$TARBALL.part" "$URL" || fail "download failed"
  mv "$TARBALL.part" "$TARBALL"
else
  log "tarball cached: $TARBALL"
fi
SDL_SHA256="$(sha256_of "$TARBALL")"
log "SDL2 tarball sha256: $SDL_SHA256"

# 2) 已安装则跳过构建
if [ -f "$PREFIX/lib/cmake/SDL2/SDL2Config.cmake" ]; then
  log "SDL2 already installed at $PREFIX — skip build (delete it to force rebuild)"
  log "OK — use sha256 above for docs/VERSIONS.md"
  exit 0
fi

# 3) 解压（防重复；与锁定 commit 的 tag tarball 一致即可复用）
if [ ! -f "$SRC_DIR/CMakeLists.txt" ]; then
  if [ -d "$SRC_DIR" ]; then rm -rf "$SRC_DIR"; fi
  TMP="$SRC_DIR.extract.$$"
  rm -rf "$TMP"
  mkdir -p "$TMP"
  tar -xzf "$TARBALL" -C "$TMP"
  SUB="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d -name 'SDL-*' | head -n 1)"
  [ -n "$SUB" ] || fail "unexpected SDL tarball layout"
  mkdir -p "$ROOT/third_party"
  mv "$SUB" "$SRC_DIR"
  rm -rf "$TMP"
  log "extracted SDL2 to $SRC_DIR"
else
  log "source already extracted: $SRC_DIR"
fi

ensure_cmake

# 4) 配置 + 构建 + 安装（静态库，避免运行期 dylib 路径问题；软件渲染足够本项目使用）
log "configuring (Unix Makefiles, static)"
cmake -S "$SRC_DIR" -B "$BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DSDL_SHARED=OFF \
  -DSDL_STATIC=ON \
  -DSDL_TEST_LIBRARY=OFF \
  -DSDL_TESTS=OFF \
  -DSDL_EXAMPLES=OFF \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" || fail "cmake configure failed"

log "building ($(nproc_of) jobs)"
cmake --build "$BUILD_DIR" --parallel "$(nproc_of)" || fail "cmake build failed"

log "installing to $PREFIX"
cmake --install "$BUILD_DIR" || fail "cmake install failed"

[ -f "$PREFIX/lib/cmake/SDL2/SDL2Config.cmake" ] || fail "SDL2Config.cmake missing after install"
printf '%s\n' "$SDL_COMMIT" > "$PREFIX/.sdl_commit"
log "OK — SDL2 ${SDL_TAG} installed; use sha256 above for docs/VERSIONS.md"
