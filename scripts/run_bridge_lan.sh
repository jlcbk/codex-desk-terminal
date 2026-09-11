#!/bin/sh
# run_bridge_lan.sh — LAN 模式 Bridge 一键启动（整机集成 v1，A3+A4 合并）
#
# 职责（任务书 §1）：
#   1. 自动取 Mac LAN IP（ipconfig getifaddr，默认 en0；CDT_IFACE/CDT_LAN_IP 可覆盖）
#   2. 证书：config/local/dev-certs 的 server.crt SAN 不含该 LAN IP 时重建
#      （SAN = loopback + LAN IP + 额外 --san 项）。注：scripts/gen_dev_certs.sh
#      目前只生成固定 loopback SAN、无 SAN 参数（A4 脚本限制，已在报告列出），
#      故本脚本内联 openssl 按 gen_dev_certs.sh 相同结构重建，产物文件集一致
#      （ca.pem/ca.key/server.crt/server.key/server_spki_sha256.txt）。
#   3. 设备 token：config/local/device_token 存在则复用（跑两端的凭据一致性
#      由使用方保证），缺失则生成 48 hex 随机 token（600 权限；config/local/
#      已 gitignore，绝不入库）。
#   4. 起服务：bridge_serve_mock.py（mock lifecycle 场景循环 + 15s 存活快照，
#      snapshot_provider 经 server 推送，契约 INTERFACES §4）。
#
# 用法：
#   sh scripts/run_bridge_lan.sh [--setup-only] [--san IP-or-HOST]... [serve 参数透传]
#     --setup-only  只做 1–3（证书/token/设备配置提示），不起服务
#     --san X       追加 SAN（点分十进制按 IP，否则按 DNS）
#   环境变量：CDT_IFACE（默认 en0）、CDT_LAN_IP（跳过自动探测）、CDT_PORT（默认 8765）
#
# 凭证红线：token/私钥不进日志（只打 SHA-256 指纹前 8 hex）、不入 Git。
set -eu

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd -P)"

IFACE="${CDT_IFACE:-en0 en1}"  # 依次探测（本机默认路由若走 en1 则自动取到；CDT_IFACE=enN 钉死）
PORT="${CDT_PORT:-8765}"
CERT_DIR="config/local/dev-certs"
TOKEN_FILE="config/local/device_token"
SETUP_ONLY=0
EXTRA_SANS=""

# ---- 参数解析（--setup-only / --san X；其余透传给 bridge_serve_mock.py）----
PASS_THROUGH=""
while [ $# -gt 0 ]; do
    case "$1" in
        --setup-only) SETUP_ONLY=1 ;;
        --san) shift; EXTRA_SANS="$EXTRA_SANS $1" ;;
        *) PASS_THROUGH="$PASS_THROUGH $1" ;;
    esac
    shift
done

echo "==> 1/4 取 Mac LAN IP（iface=${IFACE}）"
if [ -n "${CDT_LAN_IP:-}" ]; then
    LAN_IP="$CDT_LAN_IP"
else
    LAN_IP=""
    for i in $IFACE; do
        LAN_IP="$(ipconfig getifaddr "$i" || true)"
        if [ -n "$LAN_IP" ]; then
            IFACE="$i"
            break
        fi
    done
fi
if [ -z "$LAN_IP" ]; then
    echo "ERROR: 未能取得 LAN IP（ipconfig getifaddr $IFACE 为空）。" >&2
    echo "       请确认 Wi-Fi 已连接，或用 CDT_LAN_IP=x.x.x.x / CDT_IFACE=enN 指定。" >&2
    exit 2
fi
echo "    LAN IP = $LAN_IP"

# ---- SAN 列表（用于证书重建）：loopback + LAN IP + --san 追加 ----
san_block() {
    n=1
    echo "DNS.${n} = localhost"; n=$((n+1))
    echo "IP.${n} = 127.0.0.1"; n=$((n+1))
    echo "IP.${n} = ::1"; n=$((n+1))
    echo "IP.${n} = ${LAN_IP}"; n=$((n+1))
    for s in $EXTRA_SANS; do
        case "$s" in
            *[!0-9.]*) echo "DNS.${n} = ${s}" ;;
            *)         echo "IP.${n} = ${s}" ;;
        esac
        n=$((n+1))
    done
}

regen_certs() {
    echo "==> 重建开发证书（SAN 含 LAN IP ${LAN_IP}）"
    mkdir -p "$CERT_DIR"
    CNF="$(mktemp)"
    trap 'rm -f "$CNF"' EXIT
    cat > "$CNF" <<CNF
[req]
distinguished_name = dn
prompt = no
default_md = sha256

[dn]
CN = CodexDT Dev Bridge

[ext_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash

[ext_server]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
CNF
    # SAN 段（loopback + LAN IP + --san 追加；@alt_names 多行形式，openssl 1.x/3.x 兼容）
    cat >> "$CNF" <<CNF

[alt_names]
$(san_block)
CNF
    openssl req -newkey rsa:2048 -nodes \
        -keyout "$CERT_DIR/ca.key" -x509 -days 3650 \
        -out "$CERT_DIR/ca.pem" -config "$CNF" -extensions ext_ca
    openssl req -newkey rsa:2048 -nodes \
        -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.csr" -config "$CNF"
    openssl x509 -req -in "$CERT_DIR/server.csr" -out "$CERT_DIR/server.crt" \
        -days 365 -sha256 \
        -CA "$CERT_DIR/ca.pem" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
        -extfile "$CNF" -extensions ext_server
    rm -f "$CERT_DIR/server.csr"
    chmod 600 "$CERT_DIR/ca.key" "$CERT_DIR/server.key"
    SPKI=$(openssl x509 -in "$CERT_DIR/server.crt" -pubkey -noout \
        | openssl pkey -pubin -outform DER 2>/dev/null \
        | openssl dgst -sha256 | sed 's/^.*= //')
    printf '%s\n' "$SPKI" > "$CERT_DIR/server_spki_sha256.txt"
}

echo "==> 2/4 证书检查（${CERT_DIR}）"
if [ ! -f "$CERT_DIR/server.crt" ] || [ ! -f "$CERT_DIR/ca.pem" ] || [ ! -f "$CERT_DIR/server.key" ]; then
    # 基线缺失：先用项目脚本生成 loopback 基线，再按需重建含 LAN IP 的 SAN
    echo "    证书缺失，先跑 scripts/gen_dev_certs.sh（loopback 基线）"
    sh scripts/gen_dev_certs.sh "$CERT_DIR" 2>/dev/null || regen_certs
fi
if ! openssl x509 -in "$CERT_DIR/server.crt" -noout -text 2>/dev/null \
        | grep -q "IP Address:${LAN_IP}\b"; then
    echo "    SAN 不含 $LAN_IP → 重建（gen_dev_certs.sh 无 SAN 参数，本脚本内联重建，报告已列）"
    regen_certs
else
    echo "    SAN 已含 ${LAN_IP}，复用现有证书"
fi

echo "==> 3/4 设备 token（${TOKEN_FILE}）"
if [ -f "$TOKEN_FILE" ]; then
    echo "    已存在 → 复用（指纹 $(python3 -c "import hashlib,sys;print(hashlib.sha256(sys.stdin.read().strip().encode()).hexdigest()[:8])" < "$TOKEN_FILE")…）"
else
    mkdir -p "$(dirname "$TOKEN_FILE")"
    ( umask 177 && openssl rand -hex 24 > "$TOKEN_FILE" )
    echo "    已生成 48 hex 随机 token（600 权限；指纹 $(python3 -c "import hashlib,sys;print(hashlib.sha256(sys.stdin.read().strip().encode()).hexdigest()[:8])" < "$TOKEN_FILE")…）"
fi

CA_PEM_PATH="$REPO_ROOT/$CERT_DIR/ca.pem"
SPKI_HEX="$(cat "$CERT_DIR/server_spki_sha256.txt")"

# 设备端配置提示（值可复制进 firmware/main/dev_net_config.h；该文件已 gitignore）
echo ""
echo "---- 设备侧固件配置提示（firmware/main/dev_net_config.h，gitignore 文件）----"
echo "DEV_BRIDGE_HOST   \"$LAN_IP\""
echo "DEV_BRIDGE_PORT   $PORT"
echo "DEV_DEVICE_TOKEN  $(cat "$TOKEN_FILE")"
echo "DEV_BRIDGE_SPKI_SHA256_HEX  $SPKI_HEX"
echo "DEV_BRIDGE_CA_PEM ← 逐行加引号粘贴 ${CA_PEM_PATH}（见模板 firmware/main/dev_net_config.h.template）"
echo "--------------------------------------------------------------------------"
echo ""
[ "$SETUP_ONLY" = "1" ] && { echo "==> --setup-only：完成，不起服务"; exit 0; }

echo "==> 4/4 启动 Bridge WSS server（mock lifecycle 循环，keepalive 15s）"
PY=python3
if command -v uv >/dev/null 2>&1 && ! $PY -c "import websockets" >/dev/null 2>&1; then
    echo "    系统 python 无 websockets → uv run --python 3.12 --with 'websockets==17.1'"
    exec uv run --python 3.12 --with 'websockets==17.1' \
        python3 scripts/bridge_serve_mock.py \
        --host "$LAN_IP" --port "$PORT" \
        --cert "$CERT_DIR/server.crt" --key "$CERT_DIR/server.key" \
        --token-file "$TOKEN_FILE" $PASS_THROUGH
fi
exec $PY scripts/bridge_serve_mock.py \
    --host "$LAN_IP" --port "$PORT" \
    --cert "$CERT_DIR/server.crt" --key "$CERT_DIR/server.key" \
    --token-file "$TOKEN_FILE" $PASS_THROUGH
