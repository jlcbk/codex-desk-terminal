"""TLS 工具（A4-W，P3.3 loopback；protocol/transport.md §5.5 冻结规则）。

- SPKI SHA-256：最小 DER TLV 读取器定位叶子证书的 SubjectPublicKeyInfo，
  对其原始 DER 字节（tag+length+content）取 SHA-256，零第三方依赖。
  计算口径与 `openssl x509 -pubkey -noout | openssl pkey -pubin -outform DER
  | openssl dgst -sha256` 一致（tests/transport/wss 有交叉验证）。
- ssl 上下文：服务端加载自签证书链；客户端 fail-closed——必须提供 CA，
  不提供任何"关闭验证"的选项（生产禁止，transport.md §5.5）。

本模块不解析任何业务 JSON。
"""

from __future__ import annotations

import hashlib
import ssl
from typing import List, Optional, Tuple

from .link import TlsVerificationError, TransportConfigError

# ---------------------------------------------------------------------------
# 最小 DER TLV 读取（只用于定位 SPKI；不做通用 ASN.1 语义校验）
# ---------------------------------------------------------------------------


def _read_tlv(data: bytes, off: int) -> Tuple[int, bytes, bytes, int]:
    """读取一个 DER TLV，返回 (tag, 原始编码字节, content, 下一偏移)。支持长格式长度。"""
    if off + 2 > len(data):
        raise ValueError("DER truncated (header)")
    tag = data[off]
    pos = off + 1
    first = data[pos]
    pos += 1
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if n == 0 or n > 4:
            raise ValueError(f"unsupported DER length form (n={n})")
        if pos + n > len(data):
            raise ValueError("DER truncated (length)")
        length = int.from_bytes(data[pos : pos + n], "big")
        pos += n
    end = pos + length
    if end > len(data):
        raise ValueError("DER truncated (content)")
    return tag, data[off:end], data[pos:end], end


def _children(content: bytes) -> List[bytes]:
    """枚举一个 constructed 元素 content 里的直接子元素（原始 TLV 字节）。"""
    out: List[bytes] = []
    off = 0
    while off < len(content):
        _, raw, _, off = _read_tlv(content, off)
        out.append(raw)
    return out


def _spki_der(cert_der: bytes) -> bytes:
    """定位 Certificate → tbsCertificate → subjectPublicKeyInfo 的原始 DER。"""
    tag, cert_raw, cert_content, _ = _read_tlv(cert_der, 0)
    if tag != 0x30:
        raise ValueError("not a DER Certificate (expected SEQUENCE)")
    # Certificate ::= SEQUENCE { tbsCertificate, signatureAlgorithm, signatureValue }
    cert_children = _children(cert_content)
    if not cert_children:
        raise ValueError("empty Certificate")
    tbs_raw = cert_children[0]
    _, _, tbs_content, _ = _read_tlv(tbs_raw, 0)
    tbs_children = _children(tbs_content)
    # v3 证书首个子元素是显式 version [0]（0xA0）；v1 无此字段。
    idx = 6 if (tbs_children and tbs_children[0][0] == 0xA0) else 5
    if idx >= len(tbs_children):
        raise ValueError("tbsCertificate too short to contain subjectPublicKeyInfo")
    spki_raw = tbs_children[idx]
    spki_tag, _, _, _ = _read_tlv(spki_raw, 0)
    if spki_tag != 0x30:
        raise ValueError("subjectPublicKeyInfo not found at expected position")
    return spki_raw


# ---------------------------------------------------------------------------
# SPKI SHA-256（叶子证书 pinning，transport.md §5.5 ②）
# ---------------------------------------------------------------------------


def spki_sha256_hex(cert_der: bytes) -> str:
    """计算 X.509 证书（DER）叶子公钥 SubjectPublicKeyInfo 的 SHA-256 hex。"""
    return hashlib.sha256(_spki_der(cert_der)).hexdigest()


def verify_spki_sha256_hex(cert_der: bytes, expected_hex: str) -> None:
    """校验 SPKI pinning；不匹配 → TlsVerificationError（CONFIG_ERROR 终态）。"""
    actual = spki_sha256_hex(cert_der)
    if actual.lower() != expected_hex.strip().lower():
        raise TlsVerificationError(
            f"SPKI SHA-256 pinning mismatch: got {actual}, expected {expected_hex.strip().lower()}"
        )


def pem_to_der(pem_data: bytes) -> bytes:
    """PEM → DER（证书文件读取用；与 ssl.PEM_cert_to_DER_cert 等价但接受 bytes）。"""
    return ssl.PEM_cert_to_DER_cert(pem_data.decode("ascii"))


def spki_sha256_of_file(cert_file) -> str:
    """从 PEM 证书文件计算 SPKI SHA-256（配置工具/自检用）。"""
    with open(cert_file, "rb") as fh:
        return spki_sha256_hex(pem_to_der(fh.read()))


# ---------------------------------------------------------------------------
# ssl 上下文构建（fail-closed）
# ---------------------------------------------------------------------------


def build_server_ssl_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    """Bridge 服务端上下文：TLS 1.2+，加载证书链与私钥（§5.2 步 1）。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
    except (OSError, ssl.SSLError) as exc:
        raise TransportConfigError(f"无法加载 TLS 证书/私钥（路径与口令检查）: {type(exc).__name__}") from exc
    return ctx


def build_client_ssl_context(*, ca_file: str, check_hostname: bool = True) -> ssl.SSLContext:
    """设备端上下文：必须以预置 CA 验链（§5.5 ①），不允许关闭验证。

    check_hostname 仅在 URL 主机名与证书 SAN 不一致的开发场景（如用 IP 之外的
    别名直连）由配置显式放宽；证书链验证永不关闭（fail-closed）。
    """
    if not ca_file:
        raise TransportConfigError("wss 客户端必须配置 ca_file（生产禁止关闭证书验证）")
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = check_hostname
    if not check_hostname:
        # 关主机名检查时仍要求完整链验证
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def peer_cert_der_from_connection(conn) -> Optional[bytes]:
    """从已建立的 websockets 连接取对端叶子证书 DER（客户端 pinning 用）。

    明文 ws 连接无 TLS 层，返回 None。
    """
    transport = getattr(conn, "transport", None)
    if transport is None:
        return None
    ssl_obj = transport.get_extra_info("ssl_object")
    if ssl_obj is None:
        return None
    der: Optional[bytes] = ssl_obj.getpeercert(binary_form=True)
    return der
