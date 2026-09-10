#!/bin/sh
# build_transport_tests.sh — BLE 传输内核主机端构建+测试（P3.4，A4-B host 阶段）
# 编译 shared/transport（cdt_crc32/cdt_fragmenter/cdt_reassembler，均不改动
# P0.5 冻结的 cdt_frame.h）+ shared/state store 集成用例并运行。
# 只依赖 Apple cc 与 python3；产物 build/transport/test_transport。
# 退出码 0 = 全部通过（含平台头门禁）。
set -e
cd "$(dirname "$0")/.."

OUT=build/transport
mkdir -p "$OUT"

echo "[1/4] 编译传输内核 + C 测试（含 shared/state store 集成）"
cc -std=c99 -Wall -Wextra -Werror -pedantic \
   -Ishared/transport -Ishared/state \
   shared/transport/cdt_crc32.c \
   shared/transport/cdt_fragmenter.c \
   shared/transport/cdt_reassembler.c \
   shared/state/cdt_json.c \
   shared/state/cdt_parser.c \
   shared/state/cdt_store.c \
   tests/transport/ble/test_transport.c \
   -o "$OUT/test_transport"

echo "[2/4] 运行内置用例（CRC 4 冻结向量/MTU23|53|247 矩阵/16KiB 逐字节/拒绝路径/断连/虚拟时钟超时/ACK-NACK/store 集成）"
"$OUT/test_transport"

echo "[3/4] CRC 冻结向量自验（protocol/transport.md §4：gen_crc_vectors.py --verify 须退出码 0）"
python3 scripts/gen_crc_vectors.py --verify

echo "[4/4] Gate 预检：shared/transport 不得包含平台头"
# A0 分层门禁（同 build_shared.sh 裁决）：portable 层禁 esp_/ESP_/SDL/lvgl/
# freertos。注意必须用 grep -E——BRE 下 | 是字面量，检查会静默空转。
# 只检查 #include 行（注释中提及平台名是合法的文档说明）。
GATE_RE='#[[:space:]]*include[[:space:]]*[<"](esp_|ESP_|SDL|lvgl|freertos|FreeRTOS)'
if grep -rEn "$GATE_RE" shared/transport --include='*.c' --include='*.h'; then
    echo "FAIL: shared/transport 发现平台头 include 泄漏" >&2
    exit 1
fi
echo "build_transport_tests: 全部通过"
