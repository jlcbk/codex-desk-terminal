"""fragmenter.py — BLE 二进制帧内核：发送侧分片 + 接收侧重组（P3.4，A4-B）。

真源（全部冻结，不得在此改动）：protocol/transport.md §3/§4、
docs/INTERFACES.md §7、shared/transport/cdt_frame.h（C 镜像定义）。

与 C 实现（shared/transport/cdt_fragmenter.c / cdt_reassembler.c）逐规则
互为镜像；loopback 互验由 tests/transport/ble/run_loopback.py 驱动：
  Python 分片 → C 重组 → cmp 逐字节一致；
  C 分片     → Python 重组 → 逐字节一致。

帧头 16 字节小端（§3.1 冻结偏移）：version=0、type=1、message_id=2..5、
fragment_index=6..7、fragment_count=8..9、total_len=10..11、crc32=12..15。
crc32 覆盖完整 payload（重组后字节），与 zlib.crc32 逐位一致（§4 冻结）。

红线：只搬字节——不解释 payload 内容；凭证不进入帧或日志。
纯标准库、无平台依赖；兼容 Python ≥3.9（系统 python3 与 Bridge 3.12 均可运行）。
"""

from __future__ import annotations

import struct
import sys
import zlib

# --- 冻结常量（与 shared/transport/cdt_frame.h 一致，勿改） ----------------

FRAME_VERSION = 1
FRAME_SIZE = 16
TYPE_INVALID = 0
TYPE_DATA = 1
TYPE_ACK = 2
TYPE_NACK = 3

MAX_MESSAGE_LEN = 16384
MAX_FRAGMENTS = 4096
ATT_OVERHEAD = 3
PROGRESS_TIMEOUT_MS = 3000
TOTAL_TIMEOUT_MS = 30000

# version, type, message_id, fragment_index, fragment_count, total_len, crc32
_HEADER = struct.Struct("<BBIHHHI")
assert _HEADER.size == FRAME_SIZE


def chunk_capacity(mtu: int) -> int:
    """片容量 = ATT_MTU − 3（ATT 写头）− 16（帧头）；MTU23 时为 4。"""
    return mtu - ATT_OVERHEAD - FRAME_SIZE


def fragment_count(total_len: int, chunk: int) -> int:
    """一消息总片数 = ceil(total_len / chunk)（chunk 必须 >0）。"""
    if chunk <= 0:
        raise ValueError("chunk capacity must be > 0 (mtu too small)")
    return (total_len + chunk - 1) // chunk


# --- 发送侧分片 -----------------------------------------------------------

class Fragmenter:
    """一次一条消息的分片器（§3.3 发送方 1–3；上限校验在构造时完成）。"""

    def __init__(self, payload: bytes, message_id: int, mtu: int):
        if not payload:
            raise ValueError("total_len=0 does not exist (AppState/Telemetry non-empty)")
        if len(payload) > MAX_MESSAGE_LEN:
            raise ValueError("payload exceeds 16384-byte aggregate limit")
        self.chunk = chunk_capacity(mtu)
        if self.chunk <= 0:
            raise ValueError("ATT_MTU too small: chunk capacity must be > 0")
        self.count = fragment_count(len(payload), self.chunk)
        if self.count > MAX_FRAGMENTS:
            raise ValueError(
                "fragment_count %d exceeds %d (MTU23 floor is 4 bytes/chunk)"
                % (self.count, MAX_FRAGMENTS))
        self.payload = payload
        self.message_id = message_id
        self.total_len = len(payload)
        self.crc32 = zlib.crc32(payload) & 0xFFFFFFFF

    @property
    def mtu_needed(self) -> int:
        return self.chunk + ATT_OVERHEAD + FRAME_SIZE

    def header(self, index: int) -> bytes:
        """第 index 片的 16 字节帧头（各片除 index 外逐字段一致）。"""
        return _HEADER.pack(FRAME_VERSION, TYPE_DATA, self.message_id,
                            index, self.count, self.total_len, self.crc32)

    def frame(self, index: int) -> bytes:
        """第 index 片整帧（帧头+载荷）；只有最后一片允许短片（§3.3）。"""
        start = index * self.chunk
        piece = self.payload[start:start + self.chunk]
        if len(piece) == 0:
            raise IndexError("fragment index out of range")
        return self.header(index) + piece

    def frames(self):
        """按序产出全部片帧。"""
        for i in range(self.count):
            yield self.frame(i)


def encode_ack(confirmed: bytes) -> bytes:
    """ACK 帧（16B，无 payload）：回填被确认 DATA 的元数据，index=0（§3.2）。"""
    if len(confirmed) != FRAME_SIZE:
        raise ValueError("confirmed must be a 16-byte DATA header")
    (_version, _type, msg_id, _idx, cnt, total, crc) = _HEADER.unpack(confirmed)
    return _HEADER.pack(FRAME_VERSION, TYPE_ACK, msg_id, 0, cnt, total, crc)


def encode_nack(confirmed: bytes) -> bytes:
    """NACK 帧：同 ACK 填充规则，type=3，表示需重发全包（§3.2/§3.4）。"""
    if len(confirmed) != FRAME_SIZE:
        raise ValueError("confirmed must be a 16-byte DATA header")
    (_version, _type, msg_id, _idx, cnt, total, crc) = _HEADER.unpack(confirmed)
    return _HEADER.pack(FRAME_VERSION, TYPE_NACK, msg_id, 0, cnt, total, crc)


def encode_ack_nack(confirmed: bytes, applied_result: str) -> bytes:
    """依上层三值结果决策（§3.4）：applied/duplicate→ACK；rejected→NACK。"""
    if applied_result == "rejected":
        return encode_nack(confirmed)
    if applied_result in ("applied", "duplicate"):
        return encode_ack(confirmed)
    raise ValueError("apply result must be applied/duplicate/rejected")


# --- 接收侧重组（与 C 引擎同规则的独立 Python 实现，供互验/mock 设备） -----

class Reject(Exception):
    """整包拒绝（含原因与被拒/在途帧头，可据此回 NACK，§3.3 接收方）。"""

    def __init__(self, reason: str, header: bytes = b""):
        super().__init__(reason)
        self.reason = reason
        self.header = header


class Reassembler:
    """一次仅 1 条消息；16KiB 缓冲 + 位图；超时由调用方喂 now_ms 判定。

    规则（§3.3 接收方 1–6，与 C 实现一致，含"异 message_id 中途到达视为
    矛盾头"与"无上下文时 index>0 为孤儿片"两个实现裁决）：
      1. 首片（index=0）锁定上下文；矛盾头拒绝整包并清空。
      2. 同 index 同数据忽略（不计进展）；异数据拒绝整包。
      3. 16KiB / 4096 片上限与几何（count == ceil(total/chunk)）校验。
      4. 3s 无进展或 30s 未完成 → 丢弃（poll/feed 均判定）。
      5. 齐片 → CRC → on_complete（由调用方接 JSON/store 与三值 ACK）。
      6. reset() 立刻清空；残留不复活。
    """

    def __init__(self, mtu: int):
        self.chunk = chunk_capacity(mtu)
        if self.chunk <= 0:
            raise ValueError("ATT_MTU too small: chunk capacity must be > 0")
        self.reset()

    def reset(self) -> None:
        self._active = False
        self._buf = bytearray()
        self._got = set()

    @property
    def active(self) -> bool:
        return self._active

    # -- 内部校验（对每片执行；任何拒绝即清上下文，§3.3"拒绝整包"） --

    def _check_common(self, hdr: tuple):
        (version, ftype, _msg_id, index, count, total_len, _crc) = hdr
        if version != FRAME_VERSION or ftype != TYPE_DATA:
            raise Reject("bad_header")
        if total_len == 0:
            raise Reject("total_len_zero")
        if total_len > MAX_MESSAGE_LEN:
            raise Reject("total_len_over")
        if count < 1 or count > MAX_FRAGMENTS:
            raise Reject("count_over")
        if index >= count:
            raise Reject("index_oob")
        if count != fragment_count(total_len, self.chunk):
            raise Reject("geometry")

    def _expect_len(self, index: int, count: int, total_len: int) -> int:
        if index + 1 < count:
            return self.chunk
        return total_len - self.chunk * (count - 1)

    def _timeout(self, now_ms: int) -> None:
        """超时判定（达到阈值即超时；§3.3 接收方 4）。只抛出不重置状态。"""
        if not self._active:
            return
        progress_gap = now_ms - self._last_progress
        total_gap = now_ms - self._first
        if progress_gap >= PROGRESS_TIMEOUT_MS or total_gap >= TOTAL_TIMEOUT_MS:
            hdr = self._ctx_header()
            reason = ("progress_timeout" if progress_gap >= PROGRESS_TIMEOUT_MS
                      else "total_timeout")
            raise Reject(reason, hdr)

    def _ctx_header(self) -> bytes:
        return _HEADER.pack(FRAME_VERSION, TYPE_DATA, self._msg_id, 0,
                            self._count, self._total_len, self._crc)

    def feed(self, frame: bytes, now_ms: int) -> str:
        """喂入一整帧（16B 头+载荷）。返回 'ok'/'completed'；拒绝抛 Reject。"""
        if len(frame) < FRAME_SIZE:
            raise Reject("bad_header")
        hdr = _HEADER.unpack(frame[:FRAME_SIZE])
        payload = frame[FRAME_SIZE:]
        try:
            self._timeout(now_ms)
            self._check_common(hdr)
            (_v, _t, msg_id, index, count, total_len, crc) = hdr
            expect = self._expect_len(index, count, total_len)
            if len(payload) != expect:
                raise Reject("bad_fragment_size")
            if not self._active:
                if index != 0:
                    raise Reject("orphan")
                self._active = True
                self._msg_id, self._count = msg_id, count
                self._total_len, self._crc = total_len, crc
                self._buf = bytearray(total_len)
                self._got = set()
                self._first = self._last_progress = now_ms
            elif (msg_id != self._msg_id or count != self._count
                    or total_len != self._total_len or crc != self._crc):
                raise Reject("context_mismatch")
            if index in self._got:
                start = index * self.chunk
                if bytes(self._buf[start:start + len(payload)]) == payload:
                    return "ok"  # 重复片忽略，不计进展
                raise Reject("data_mismatch")
            start = index * self.chunk
            self._buf[start:start + len(payload)] = payload
            self._got.add(index)
            self._last_progress = now_ms
            if len(self._got) == self._count:
                data = bytes(self._buf)
                ctx_hdr = self._ctx_header()
                self.reset()
                if (zlib.crc32(data) & 0xFFFFFFFF) != self._crc:
                    raise Reject("crc_mismatch", ctx_hdr)
                self._complete = data
                return "completed"
            return "ok"
        except Reject:
            self.reset()
            raise

    def poll(self, now_ms: int) -> bool:
        """显式超时巡检；因超时丢弃上下文时返回 True（异常存
        last_poll_reject 供回 NACK）。"""
        try:
            self._timeout(now_ms)
        except Reject as exc:
            self.reset()
            self.last_poll_reject = exc
            return True
        return False

    def take(self) -> bytes:
        """取回最近一次 completed 的完整字节（交付后请尽快拷走）。"""
        data = getattr(self, "_complete", None)
        if data is None:
            raise RuntimeError("no completed message")
        return data


# --- CLI：分片/重组二进制容器（[u32le 帧长][帧]），与 C 测试程序共用 -------

def write_frames_file(path: str, frames) -> int:
    n = 0
    with open(path, "wb") as fh:
        for frame in frames:
            fh.write(struct.pack("<I", len(frame)))
            fh.write(frame)
            n += 1
    return n


def read_frames_file(path: str):
    with open(path, "rb") as fh:
        data = fh.read()
    cursor = 0
    while cursor < len(data):
        if cursor + 4 > len(data):
            raise ValueError("truncated frame prefix at %d" % cursor)
        (flen,) = struct.unpack_from("<I", data, cursor)
        cursor += 4
        if flen < FRAME_SIZE or cursor + flen > len(data):
            raise ValueError("bad frame length %d at %d" % (flen, cursor))
        yield data[cursor:cursor + flen]
        cursor += flen


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="BLE frame fragmenter/reassembler "
                                            "(frozen 16B header; transport.md §3)")
    p.add_argument("--mode", choices=("fragment", "reassemble"), default="fragment")
    p.add_argument("--in", dest="src", required=True, help="input file "
                   "(fragment: raw payload; reassemble: frames container)")
    p.add_argument("--out", dest="out", required=True,
                   help="output file (fragment: frames container; reassemble: payload)")
    p.add_argument("--mtu", type=int, default=23, help="negotiated ATT MTU (fragment)")
    p.add_argument("--message-id", type=int, default=1, help="connection-scoped id")
    args = p.parse_args(argv)

    if args.mode == "fragment":
        with open(args.src, "rb") as fh:
            payload = fh.read()
        fg = Fragmenter(payload, args.message_id, args.mtu)
        n = write_frames_file(args.out, fg.frames())
        print("fragmenter: %d bytes -> %d frames (mtu=%d chunk=%d crc=0x%08X) -> %s"
              % (len(payload), n, args.mtu, fg.chunk, fg.crc32, args.out))
        return 0

    rs = Reassembler(args.mtu)
    completed_frame = None
    for i, frame in enumerate(read_frames_file(args.src)):
        if rs.feed(frame, i * 5) == "completed":
            completed_frame = i
            break
    if completed_frame is None:
        print("reassembler: FAIL — message never completed", file=sys.stderr)
        return 1
    data = rs.take()
    with open(args.out, "wb") as fh:
        fh.write(data)
    print("reassembler: %d frames -> %d bytes -> %s" % (completed_frame + 1, len(data), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
