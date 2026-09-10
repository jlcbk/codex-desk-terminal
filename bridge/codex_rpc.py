"""stdio JSON-RPC client for the locked `codex app-server`（P3.1）。

改造自 scripts/probe/appserver_client.py（P0.2 探针客户端；探针目录保持不动）。
照搬的部分：按行分隔 JSON-RPC 帧（codex-cli 0.152.0 实测）、每个阻塞调用显式超时、
子进程独立进程组（start_new_session）且 stop() 必定 SIGTERM→SIGKILL 收割、
stderr 只保留环形尾部。

与探针版的差异：
- server 通知与 server→client 请求统一进入单一 inbox 队列（带 kind 标签），
  新增 wait_any(timeout) 供 adapter 泵循环同时消费两类消息；
- 客户端永不自动应答 server→client 请求（审批只接收不回应，安全红线）。

仅依赖 Python 标准库。
"""

from __future__ import annotations

import collections
import json
import os
import queue
import signal
import subprocess
import threading
import time

# inbox 消息种类。
KIND_NOTIFICATION = "notification"      # server->client 通知（无 id）
KIND_SERVER_REQUEST = "server_request"  # server->client 请求（审批等；只接收不回应）


class RpcError(Exception):
    """Server returned a JSON-RPC error response."""

    def __init__(self, method, code, message, data=None):
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        super().__init__("RPC error on %s: %s (%s)" % (method, message, code))


class RpcTimeout(Exception):
    pass


class AppServerClient:
    """Spawn an app-server process (argv is the full command line) and speak JSON-RPC."""

    def __init__(self, argv, cwd=None, env=None):
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.proc = None
        self.inbox = queue.Queue()  # (kind, msg) tuples
        self.parse_errors = collections.deque(maxlen=20)
        self.stderr_tail = collections.deque(maxlen=40)
        self._pending = {}
        self._cond = threading.Condition()
        self._next_id = 1
        self._threads = []
        self._stopped = threading.Event()

    # -- lifecycle -------------------------------------------------------
    def start(self):
        if self.proc is not None:
            raise RuntimeError("already started")
        self.proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            text=True,
            bufsize=1,
            start_new_session=True,  # own process group -> clean kill
        )
        t_out = threading.Thread(target=self._reader_loop, daemon=True)
        t_err = threading.Thread(target=self._stderr_loop, daemon=True)
        t_out.start()
        t_err.start()
        self._threads = [t_out, t_err]
        return self

    def stop(self, grace_seconds=3.0):
        """Terminate the child and reap it. Safe to call more than once."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self.proc.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    pass
        self._stopped.set()
        with self._cond:
            self._cond.notify_all()
        self.proc = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # -- requests --------------------------------------------------------
    def request(self, method, params=None, timeout=15.0):
        """Send a JSON-RPC request; return the `result` or raise RpcError/RpcTimeout."""
        if not self.alive():
            raise RuntimeError("app-server process is not running")
        with self._cond:
            req_id = self._next_id
            self._next_id += 1
            entry = {"done": False, "result": None, "error": None}
            self._pending[req_id] = entry
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        msg["params"] = params  # codex tolerates explicit null params
        self._send(msg)
        deadline = time.monotonic() + timeout
        with self._cond:
            while not entry["done"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pending.pop(req_id, None)
                    raise RpcTimeout("%s timed out after %.1fs" % (method, timeout))
                if not self.alive() and not entry["done"]:
                    self._pending.pop(req_id, None)
                    raise RuntimeError(
                        "app-server exited before replying to %s; stderr tail: %s"
                        % (method, list(self.stderr_tail))
                    )
                self._cond.wait(remaining)
            self._pending.pop(req_id, None)
        if entry["error"] is not None:
            err = entry["error"]
            raise RpcError(method, err.get("code"), err.get("message"), err.get("data"))
        return entry["result"]

    def _send(self, msg):
        line = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise RuntimeError("cannot write to app-server stdin (process dead?)")

    # -- inbound messages --------------------------------------------------
    def wait_any(self, timeout):
        """Block up to timeout seconds for the next inbound message.

        Returns (kind, msg) with kind in (KIND_NOTIFICATION, KIND_SERVER_REQUEST),
        or None on timeout/stop. Server requests are NEVER answered here —
        approvals stay read-only by construction.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._stopped.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return self.inbox.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue

    # -- internals -------------------------------------------------------
    def _reader_loop(self):
        proc = self.proc
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.parse_errors.append(line[:200])
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" not in msg:
                self.inbox.put((KIND_NOTIFICATION, msg))
                continue
            if "method" in msg and "id" in msg:
                # server->client REQUEST (e.g. approval). We never auto-answer;
                # the adapter consumes it via wait_any() and only records it.
                self.inbox.put((KIND_SERVER_REQUEST, msg))
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._cond:
                    entry = self._pending.get(msg["id"])
                    if entry is not None:
                        entry["result"] = msg.get("result")
                        entry["error"] = msg.get("error")
                        entry["done"] = True
                        self._cond.notify_all()
                continue
            self.parse_errors.append(json.dumps(msg)[:200])

    def _stderr_loop(self):
        for raw in self.proc.stderr:
            self.stderr_tail.append(raw.rstrip()[:300])
