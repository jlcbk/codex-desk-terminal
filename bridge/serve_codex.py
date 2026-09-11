"""bridge_serve_codex 服务核心（R2，角色 A1）。

把 P3.1 真实 codex adapter（bridge-owned ephemeral 受控会话）接成持续 WSS
显示服务的业务层（ARCHITECTURE_REVIEW_2026-09-11 R2：真实 adapter 不能只是
批式收集器）。与 scripts/bridge_serve_mock.py 的接线方式一致：
adapter 快照流 → 本模块盖章 wire epoch/seq → WssServer 推送。

本模块只含可离线单测的纯逻辑/文件队列/协议编解码；asyncio 接线在
scripts/bridge_serve_codex.py。不改 StateEngine（不复刻第二套引擎）。

epoch/seq 规则（docs/INTERFACES.md §3/§4，同 mock 服务语义）：
- bridge_epoch：服务启动生成 base（codex-serve-<wallms>，每次启动新 ID）；
  每个提交 = 新会话新 epoch ``<base>-t<N>``，设备端按新 epoch 全量替换。
- seq：同一 epoch 内严格递增（存活快照也占 seq；keepalive 重发末份内容但
  seq+1，字节必变 → 每 15s 真实下发一份全量存活快照，§4 设备新鲜度）。

提交入口（两种都实现，汇聚同一有界串行队列）：
- unix socket：``{"cmd":"submit","prompt":"..."}`` → 应答 JSON 一行；
- 目录投递：tasks 目录出现 ``*.prompt`` 即原子认领（rename 成 .claiming），
  处理后归档到 archive/（附 result.json；prompt 内容是用户自己的文件，
  只移动不复制，报告/日志只记长度不记内容）。

空闲语义（如实映射，不虚构状态）：无会话时空闲快照 threads=[]（纯 IDLE）；
排队中的任务尚无上游线程，保持当前画面（日志记队列深度），绝不伪造
working/线程记录。任务完成后末帧保持（mock hold 语义）直至下一会话新 epoch。

长驻有界（R2 审核要求）：hub 只保留最近一份快照；adapter 快照经 sink 即时
交付不累积；原始 IO 日志有界 deque；任务队列有界（满则拒绝提交）。
"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .redact import CODEX_HOME
from .state import reducer as rd
from .state import render as render_mod
from .state.engine import SOURCE_BRIDGE_OWNED

# ---- 默认值 / 上限 ----------------------------------------------------------

DEFAULT_SOCKET_PATH = "config/local/bridge_serve_codex.sock"
DEFAULT_TASKS_DIR = "config/local/tasks"
DEFAULT_EVIDENCE_DIR = "artifacts/serve_codex"

MAX_PROMPT_BYTES = 64 * 1024          # 单条提交 prompt 上限（UTF-8 字节）
DEFAULT_QUEUE_MAX = 16                # 有界任务队列（R2：长驻内存有界）
RAW_LOG_MAX = 400                     # 每任务原始 IO 日志保留条数（已脱敏）
SESSION_EPOCH_SUFFIX_MAX = 8          # "-t9999" 预留（base 截断预算）

SUBMIT_CMD = "submit"
STATUS_CMD = "status"
PROTOCOL_VERSION = 1

# ---- 脱敏自检（与 scripts/codex_live_smoke.py 同阈值）-----------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|Bearer\s+\S+|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{16,}|api[_-]?key\s*[=:]\s*\S+|token\s*[=:]\s*\S+",
    re.IGNORECASE,
)


def redaction_selfcheck(paths) -> list:
    """扫描落盘产物；返回违规列表（空 = 通过；凭证/邮箱/home 路径不得出现）。"""
    violations = []
    for path in paths:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if CODEX_HOME and CODEX_HOME in text:
            violations.append("%s: contains home path" % p.name)
        if _EMAIL_RE.search(text):
            violations.append("%s: contains email-like string" % p.name)
        if _SECRET_RE.search(text):
            violations.append("%s: contains key-like string" % p.name)
    return violations


# ---- ServeHub：wire epoch/seq 盖章层（业务层职责，Transport 只搬字节）--------

def _encode_snapshot(snap: dict) -> bytes:
    return json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def initial_snapshot(epoch: str, anchor_ms: int, now_mono: int = 0) -> dict:
    """空闲初始快照：threads 空（纯 IDLE）、usage 未可用、source connected。

    用空内部状态直接渲染（不经过事件：空闲没有任何上游事实可报）。
    """
    return render_mod.render(
        rd.new_internal_state(),
        bridge_epoch=epoch, seq=0, source_kind=SOURCE_BRIDGE_OWNED,
        now_mono=int(now_mono), anchor_ms=int(anchor_ms))


class ServeHub:
    """快照盖章层：写入方（worker 线程）与 keepalive（事件循环线程）并发安全。

    - publish(snapshot)：盖上当前 epoch + 递增 seq，编码并保存为当前字节；
      返回 bytes 由调用方交给 WssServer.send()。
    - keepalive()：内容取最近一份快照、seq 递增（§4 存活快照也占 seq）；
      无任何快照时返回 None（不编造）。
    - 线程安全：全部状态变更在锁内（seq 不重复、不回卷）。
    """

    def __init__(self, base_epoch: str) -> None:
        base = str(base_epoch)
        if not base:
            raise ValueError("base_epoch must be non-empty")
        budget = render_mod.MAX_EPOCH_BYTES - SESSION_EPOCH_SUFFIX_MAX
        raw = base.encode("utf-8")
        if len(raw) > budget:
            base = raw[:budget].decode("utf-8", errors="ignore")
        self._base = base
        self._lock = threading.Lock()
        self._epoch = base
        self._seq = 0
        self._last: Optional[dict] = None
        self._last_bytes: Optional[bytes] = None
        self.published = 0
        self.sessions = 0

    @property
    def base_epoch(self) -> str:
        return self._base

    @property
    def epoch(self) -> str:
        with self._lock:
            return self._epoch

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def last_bytes(self) -> Optional[bytes]:
        with self._lock:
            return self._last_bytes

    def session_epoch(self, job_id: str) -> str:
        """切换到新会话 epoch（``<base>-<job_id>``），seq 归零（新会话全量替换）。

        返回新 epoch（供 adapter 构造使用；hub 与 adapter 用同一字符串）。
        """
        candidate = "%s-%s" % (self._base, job_id)
        raw = candidate.encode("utf-8")
        if len(raw) > render_mod.MAX_EPOCH_BYTES:
            candidate = raw[:render_mod.MAX_EPOCH_BYTES].decode("utf-8", errors="ignore")
        with self._lock:
            self._epoch = candidate
            self._seq = 0
            self.sessions += 1
        return candidate

    def publish(self, snapshot: dict) -> bytes:
        """worker 线程：快照盖章并保存为当前下发字节（返回 bytes）。"""
        with self._lock:
            self._seq += 1
            snap = dict(snapshot)
            snap["bridge_epoch"] = self._epoch
            snap["seq"] = self._seq
            data = _encode_snapshot(snap)
            self._last = snap
            self._last_bytes = data
            self.published += 1
        return data

    def keepalive(self) -> Optional[bytes]:
        """事件循环线程：存活快照（末份内容 + 新 seq；无快照返回 None）。"""
        with self._lock:
            if self._last is None:
                return None
            self._seq += 1
            snap = dict(self._last)
            snap["seq"] = self._seq
            data = _encode_snapshot(snap)
            self._last = snap
            self._last_bytes = data
            return data


# ---- 有界串行任务队列 --------------------------------------------------------

@dataclass
class Job:
    job_id: str                    # "t<N>"（也是 epoch 后缀）
    prompt: str
    origin: str                    # "socket" | "file:<原文件名>"
    claimed_path: Optional[str] = None  # 目录投递的 .claiming 文件（完成后归档）
    enqueued_at: float = field(default_factory=time.time)


class SubmissionQueue:
    """有界 FIFO（一次一个 ephemeral 会话，串行消费；满则拒绝新提交）。"""

    def __init__(self, maxsize: int = DEFAULT_QUEUE_MAX) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._next = 1
        self.maxsize = maxsize

    def submit(self, prompt: str, origin: str,
               claimed_path: Optional[str] = None):
        """入队；返回 (ok, job_id 或错误, 当前队列深度)。"""
        if not isinstance(prompt, str) or not prompt.strip():
            return False, "prompt must be a non-empty string", self.depth
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            return False, "prompt exceeds %d bytes" % MAX_PROMPT_BYTES, self.depth
        with self._lock:
            job_id = "t%d" % self._next
            self._next += 1
        try:
            self._q.put_nowait(Job(job_id=job_id, prompt=prompt, origin=origin,
                                   claimed_path=claimed_path))
        except queue.Full:
            with self._lock:
                return False, "queue full (max %d)" % self.maxsize, self.maxsize
        return True, job_id, self.depth

    def get(self, timeout: float) -> Optional[Job]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def depth(self) -> int:
        return self._q.qsize()

    def task_done(self) -> None:
        try:
            self._q.task_done()
        except ValueError:
            pass


# ---- 目录投递（config/local/tasks/*.prompt → 认领 → 归档）--------------------

class FileTaskSource:
    """tasks 目录投递源：*.prompt 出现即认领（原子 rename），处理后归档。

    - 认领：``<name>.prompt`` → ``<name>.prompt.claiming``（同目录 rename 原子，
      多进程/重复扫描不会重复消费）。
    - 归档：处理后 move 到 ``<tasks>/archive/<UTC>-<job_id>-<name>.prompt``，
      旁边写 ``.result.json``（只含结果事实，不含 prompt 内容——内容是用户
      自己的文件，只移动不复制；报告与日志只记长度）。
    - 启动时遗留 ``*.claiming``（上次进程中断残留）→ 归档为 interrupted，
      不自动重跑（R2：恢复只重同步，不自动重复业务任务）。
    """

    def __init__(self, tasks_dir: str, jobs: SubmissionQueue) -> None:
        self.dir = Path(tasks_dir)
        self.archive_dir = self.dir / "archive"
        self.jobs = jobs

    def prepare(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.dir.glob("*.claiming")):
            self._archive(path, job_id="interrupted", status="interrupted",
                          note="stale .claiming from previous process; not re-run")

    def poll_once(self) -> list:
        """扫描并认领目录里的 *.prompt；返回入队 job_id 列表。"""
        claimed = []
        try:
            entries = sorted(self.dir.glob("*.prompt"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return claimed
        for path in entries:
            target = path.parent / (path.name + ".claiming")
            try:
                os.rename(path, target)  # 原子认领
            except OSError:
                continue  # 已被认领/消失
            try:
                prompt = target.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                self._archive(target, job_id="rejected", status="rejected",
                              note="unreadable: %s" % type(exc).__name__)
                continue
            ok, job_id_or_err, _ = self.jobs.submit(
                prompt, origin="file:%s" % path.name, claimed_path=str(target))
            if ok:
                claimed.append(job_id_or_err)
            else:
                self._archive(target, job_id="rejected", status="rejected",
                              note=job_id_or_err)
        return claimed

    def archive_result(self, job: Job, summary: dict) -> Optional[Path]:
        """任务终态后归档 .claiming 文件并写 result.json；返回归档路径。"""
        if not job.claimed_path:
            return None
        path = self._archive(Path(job.claimed_path), job_id=job.job_id,
                             status="done", note=None)
        result_path = path.with_suffix(path.suffix + ".result.json")
        try:
            result_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        except OSError:
            pass
        return path

    def _archive(self, claimed: Path, *, job_id: str, status: str,
                 note: Optional[str]) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        dest = self.archive_dir / ("%s-%s-%s" % (stamp, job_id, claimed.name))
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            os.rename(claimed, dest)
        except OSError:
            return claimed  # 归档失败不阻塞任务（文件原地保留供人工处理）
        if status != "done":
            try:
                (dest.with_suffix(dest.suffix + ".result.json")).write_text(
                    json.dumps({"status": status, "note": note},
                               ensure_ascii=False) + "\n", encoding="utf-8")
            except OSError:
                pass
        return dest


# ---- unix socket 提交协议（服务端编解码 + 同步客户端）------------------------

def encode_request(cmd: str, prompt: Optional[str] = None) -> bytes:
    req = {"v": PROTOCOL_VERSION, "cmd": cmd}
    if prompt is not None:
        req["prompt"] = prompt
    return (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")


def decode_request(line: bytes) -> dict:
    """解析一行请求；非法输入抛 ValueError（服务端回 ok=false 应答）。"""
    msg = json.loads(line.decode("utf-8"))
    if not isinstance(msg, dict) or msg.get("cmd") not in (SUBMIT_CMD, STATUS_CMD):
        raise ValueError("cmd must be 'submit' or 'status'")
    if msg.get("cmd") == SUBMIT_CMD and not isinstance(msg.get("prompt"), str):
        raise ValueError("submit requires string prompt")
    return msg


def encode_response(*, ok: bool, **fields) -> bytes:
    resp = {"ok": ok}
    resp.update(fields)
    return (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")


async def handle_socket_client(reader, writer, *, jobs: SubmissionQueue,
                               hub: ServeHub, state=None) -> None:
    """unix socket 控制连接：一行请求一行应答（stdlib asyncio，可离线单测）。

    - submit：入队应答 ``{"ok":true,"job_id":...,"queued_depth":N}``；
      队列满/非法 prompt → ``{"ok":false,"error":...}``（prompt 不回显）。
    - status：``{"ok":true,"epoch","seq","queued","running","jobs_done",...}``。
    - state：可选对象，提供 running_job / jobs_done / dropped_frames 属性。
    """
    def _attr(name, default=None):
        value = getattr(state, name, default) if state is not None else default
        return default if value is None else value

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = decode_request(line)
            except (ValueError, UnicodeDecodeError) as exc:
                writer.write(encode_response(ok=False, error=str(exc)))
                await writer.drain()
                continue
            if req["cmd"] == SUBMIT_CMD:
                ok, job_id_or_err, depth = jobs.submit(req["prompt"], origin="socket")
                if ok:
                    writer.write(encode_response(ok=True, job_id=job_id_or_err,
                                                 queued_depth=depth))
                else:
                    writer.write(encode_response(ok=False, error=job_id_or_err))
            else:  # STATUS_CMD（decode_request 已保证取值）
                writer.write(encode_response(
                    ok=True,
                    base_epoch=hub.base_epoch,
                    epoch=hub.epoch,
                    seq=hub.seq,
                    queued=jobs.depth,
                    running=_attr("running_job"),
                    jobs_done=_attr("jobs_done", 0),
                    sessions=hub.sessions,
                    dropped_frames=_attr("dropped_frames", 0),
                ))
            await writer.drain()
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    finally:
        try:
            writer.close()
        except (OSError, RuntimeError):
            pass


def submit_via_socket(socket_path: str, prompt: str, timeout: float = 10.0):
    """同步提交客户端；返回 (ok, response dict)。服务端不可达时 ok=False。"""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(socket_path)
        try:
            sock.sendall(encode_request(SUBMIT_CMD, prompt))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
    except (OSError, ValueError) as exc:
        return False, {"ok": False, "error": "submit failed: %s" % type(exc).__name__}
    if not buf.strip():
        return False, {"ok": False, "error": "empty response from serve"}
    try:
        resp = json.loads(buf.decode("utf-8"))
    except ValueError:
        return False, {"ok": False, "error": "malformed response from serve"}
    return bool(resp.get("ok")), resp
