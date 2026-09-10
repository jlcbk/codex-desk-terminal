#!/bin/sh
# vendor_waveshare.sh — P4.1a (A3)
# 克隆 Waveshare 官方仓库 ESP32-S3-RLCD-4.2 到 vendor/waveshare-rlcd/（vendor/ 已在 .gitignore）
# 并锁定到 P0.3 审计的 HEAD commit（docs/HARDWARE.md 记录）。
# 可重复执行：目标已存在且 HEAD 校验一致则跳过。
# 每次运行都打印 commit 校验，供 A0 记入 VERSIONS.md。
set -eu

REPO_URL="https://github.com/waveshareteam/ESP32-S3-RLCD-4.2"
EXPECT_COMMIT="eb1f63427d735a22b9c30e22fa63ebddae1834d3"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR_DIR="$ROOT/vendor/waveshare-rlcd"

log() { printf '[vendor_waveshare] %s\n' "$*"; }
fail() { printf '[vendor_waveshare] ERROR: %s\n' "$*" >&2; exit 1; }

# 已存在则校验 HEAD 与 remote；一致即跳过（幂等）
if [ -d "$VENDOR_DIR/.git" ]; then
  got="$(git -C "$VENDOR_DIR" rev-parse HEAD 2>/dev/null || echo unavailable)"
  remote="$(git -C "$VENDOR_DIR" remote get-url origin 2>/dev/null || echo none)"
  if [ "$got" = "$EXPECT_COMMIT" ] && [ "$remote" = "$REPO_URL" ]; then
    log "already present and verified — skip clone"
    log "remote: $remote"
    log "HEAD:   $got"
    git -C "$VENDOR_DIR" log -1 --format='[vendor_waveshare] HEAD subject: %s'
    log "OK — vendor locked at $EXPECT_COMMIT"
    exit 0
  fi
  log "existing vendor dir mismatch (HEAD=$got remote=$remote) — re-cloning"
  rm -rf "$VENDOR_DIR"
fi

mkdir -p "$ROOT/vendor"

# 优先按精确 commit 浅取（省流量）；失败则回退完整克隆 + checkout
fetch_by_commit() {
  rm -rf "$VENDOR_DIR"
  mkdir -p "$VENDOR_DIR"
  git -C "$VENDOR_DIR" init -q
  git -C "$VENDOR_DIR" remote add origin "$REPO_URL"
  git -C "$VENDOR_DIR" fetch --depth 1 origin "$EXPECT_COMMIT"
  git -C "$VENDOR_DIR" checkout -q FETCH_HEAD
}

fetch_full() {
  rm -rf "$VENDOR_DIR"
  git clone "$REPO_URL" "$VENDOR_DIR"
  git -C "$VENDOR_DIR" checkout -q "$EXPECT_COMMIT"
}

attempt=0
max=3
ok=0
while [ $attempt -lt $max ]; do
  attempt=$((attempt+1))
  log "fetch attempt $attempt of $max (shallow fetch by commit)"
  if fetch_by_commit; then ok=1; break; fi
  log "shallow fetch by commit failed, trying full clone fallback"
  if fetch_full; then ok=1; break; fi
  sleep 5
done
[ $ok -eq 1 ] || fail "clone failed after $max attempts"

# 终检：HEAD 必须精确等于锁定 commit，remote 必须是官方仓库
got="$(git -C "$VENDOR_DIR" rev-parse HEAD)"
remote="$(git -C "$VENDOR_DIR" remote get-url origin)"
[ "$got" = "$EXPECT_COMMIT" ] || fail "HEAD mismatch: got $got expected $EXPECT_COMMIT"
[ "$remote" = "$REPO_URL" ] || fail "remote mismatch: got $remote"

log "remote: $remote"
log "HEAD:   $got"
git -C "$VENDOR_DIR" log -1 --format='[vendor_waveshare] HEAD subject: %s'
log "README fingerprint:"
sed -n '1,6p' "$VENDOR_DIR/README.md" | sed 's/^/[vendor_waveshare]   /'
log "OK — vendor locked at $EXPECT_COMMIT"
