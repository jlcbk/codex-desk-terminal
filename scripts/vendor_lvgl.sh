#!/bin/sh
# vendor_lvgl.sh — P1.3 (A2)
# 下载 docs/VERSIONS.md 锁定的 LVGL commit tarball 并解压到 vendor/lvgl/。
# 可重复执行：tarball 缓存于 third_party/dl/，vendor/lvgl 已存在且校验一致则跳过。
# 每次运行都打印 tarball sha256，供 A0 记入 VERSIONS.md。
set -eu

LVGL_COMMIT="c033a98afddd65aaafeebea625382a94020fe4a7"
LVGL_VERSION_EXPECT="9.3.0"
URL="https://github.com/lvgl/lvgl/archive/${LVGL_COMMIT}.tar.gz"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DL_DIR="$ROOT/third_party/dl"
VENDOR_DIR="$ROOT/vendor/lvgl"
TARBALL="$DL_DIR/lvgl-${LVGL_COMMIT}.tar.gz"

log() { printf '[vendor_lvgl] %s\n' "$*"; }
fail() { printf '[vendor_lvgl] ERROR: %s\n' "$*" >&2; exit 1; }

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    fail "need shasum or sha256sum"
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
LVGL_SHA256="$(sha256_of "$TARBALL")"
log "LVGL tarball sha256: $LVGL_SHA256"

# 2) 解压（已存在则校验版本与 commit 后跳过；不一致则重新解压）
version_matches() {
  # lv_version.h 用三个独立宏声明版本（无 "9.3.0" 字符串）
  grep -q '#define LVGL_VERSION_MAJOR 9'  "$VENDOR_DIR/lv_version.h" && \
  grep -q '#define LVGL_VERSION_MINOR 3'  "$VENDOR_DIR/lv_version.h" && \
  grep -q '#define LVGL_VERSION_PATCH 0'  "$VENDOR_DIR/lv_version.h"
}

STAMP_OK=0
if [ -f "$VENDOR_DIR/lv_version.h" ] && [ -f "$VENDOR_DIR/lvgl.h" ]; then
  if grep -q "$LVGL_COMMIT" "$VENDOR_DIR/.lvgl_commit" 2>/dev/null; then
    if version_matches; then
      STAMP_OK=1
      log "vendor/lvgl already present and verified (commit $LVGL_COMMIT, $LVGL_VERSION_EXPECT) — skip extract"
    fi
  fi
fi

if [ "$STAMP_OK" -eq 0 ]; then
  if [ -d "$VENDOR_DIR" ]; then
    log "removing stale/invalid $VENDOR_DIR"
    rm -rf "$VENDOR_DIR"
  fi
  TMP="$VENDOR_DIR.extract.$$"
  rm -rf "$TMP"
  mkdir -p "$TMP"
  tar -xzf "$TARBALL" -C "$TMP"
  SRC_SUBDIR="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d -name 'lvgl-*' | head -n 1)"
  [ -n "$SRC_SUBDIR" ] || fail "unexpected tarball layout"
  mkdir -p "$ROOT/vendor"
  mv "$SRC_SUBDIR" "$VENDOR_DIR"
  rm -rf "$TMP"
  version_matches || fail "extracted tree is not LVGL $LVGL_VERSION_EXPECT"
  printf '%s\n' "$LVGL_COMMIT" > "$VENDOR_DIR/.lvgl_commit"
  log "extracted LVGL $LVGL_VERSION_EXPECT (commit $LVGL_COMMIT) to $VENDOR_DIR"
fi

log "OK — use sha256 above for docs/VERSIONS.md"
