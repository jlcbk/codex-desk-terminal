#!/bin/sh
# gen_dev_certs.sh — 生成开发用自签证书（P3.3 WSS loopback/LAN 小样）
#
# 产出（写入 OUT_DIR，默认 config/local/dev-certs/，已被 .gitignore 覆盖）：
#   ca.pem / ca.key          自签开发 CA（10 年）
#   server.crt / server.key  Bridge 服务器证书（SAN: localhost / 127.0.0.1 / ::1，1 年）
#   server_spki_sha256.txt   叶子证书 SPKI 的 SHA-256 指纹（设备 pinning 配置用，非机密）
#
# 依据 protocol/transport.md §5.5：设备侧 ①用预置 CA 验链 ②校验叶子证书 SPKI
# SHA-256 pinning；私钥只存 Bridge 本地（*.pem/*.key、config/local/ 均不入 Git）。
# 本脚本只在开发环境使用；生产证书/指纹经首次烧录阶段的配置工具供应。
#
# 用法：sh scripts/gen_dev_certs.sh [OUT_DIR] [--force]
set -eu

OUT_DIR="${1:-config/local/dev-certs}"
FORCE="${2:-}"

command -v openssl >/dev/null 2>&1 || { echo "ERROR: 未找到 openssl" >&2; exit 2; }

if [ -e "$OUT_DIR" ] && [ "$FORCE" != "--force" ]; then
    echo "ERROR: $OUT_DIR 已存在；如需重建请追加 --force" >&2
    exit 3
fi

mkdir -p "$OUT_DIR"
CNF="$(mktemp)"
trap 'rm -f "$CNF"' EXIT

cat > "$CNF" <<'CNF'
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

[alt_names]
DNS.1 = localhost
IP.1 = 127.0.0.1
IP.2 = ::1
CNF

echo "==> 生成开发 CA（自签 RSA-2048，有效期 10 年）"
openssl req -newkey rsa:2048 -nodes \
    -keyout "$OUT_DIR/ca.key" -x509 -days 3650 \
    -out "$OUT_DIR/ca.pem" -config "$CNF" -extensions ext_ca

echo "==> 生成 Bridge 服务器证书（SAN: localhost/127.0.0.1/::1，有效期 1 年）"
openssl req -newkey rsa:2048 -nodes \
    -keyout "$OUT_DIR/server.key" -out "$OUT_DIR/server.csr" -config "$CNF"
openssl x509 -req -in "$OUT_DIR/server.csr" -out "$OUT_DIR/server.crt" \
    -days 365 -sha256 \
    -CA "$OUT_DIR/ca.pem" -CAkey "$OUT_DIR/ca.key" -CAcreateserial \
    -extfile "$CNF" -extensions ext_server
rm -f "$OUT_DIR/server.csr"

chmod 600 "$OUT_DIR/ca.key" "$OUT_DIR/server.key"

# SPKI SHA-256：与设备端 pinning 校验、bridge/transports/wss/tlsutil.py 计算口径一致
SPKI=$(openssl x509 -in "$OUT_DIR/server.crt" -pubkey -noout \
    | openssl pkey -pubin -outform DER 2>/dev/null \
    | openssl dgst -sha256 | sed 's/^.*= //')
printf '%s\n' "$SPKI" > "$OUT_DIR/server_spki_sha256.txt"

echo "==> 完成，输出目录：$OUT_DIR"
ls -l "$OUT_DIR" | sed 's/^/    /'
echo ""
echo "服务器叶子证书 SPKI SHA-256（填入客户端配置 tls.spki_sha256_hex）："
echo "    $SPKI"
echo "凭证红线：ca.key / server.key 为私钥，只留在本地（.gitignore 已覆盖 *.key/*.pem 与 config/local/）；token 与私钥不进日志、不入 Git。"
