#!/usr/bin/env python3
"""Minimal JSON-RPC-over-stdio client for `codex app-server` capability probes.

Python 3.9+ stdlib only. Every blocking call takes an explicit timeout.
The child process is started in its own process group and `stop()` always
terminates/kills it, so probes never leave background processes behind.

Framing: newline-delimited JSON (verified against codex-cli 0.152.0).
"""

import collections
import json
import os
import queue
import signal
import subprocess
import threading
import time


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
    """Spawn `codex app-server` (or `codex app-server proxy`) and speak JSON-RPC."""

    def __init__(self, argv, cwd=None, env=None):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.proc = None
        self.notifications = queue.Queue()
        self.server_requests = queue.Queue()  # server->client requests (approvals etc.)
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
        if params is not None or True:
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

    # -- notifications ---------------------------------------------------
    def wait_notification(self, timeout):
        """Block up to timeout seconds for one server notification (dict) or None."""
        try:
            return self.notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_server_request(self, timeout):
        """Block up to timeout seconds for one server->client request or None.

        The request is NOT answered automatically; leaving it unanswered is
        the point of the read-only approval probe.
        """
        try:
            return self.server_requests.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_notifications(self):
        out = []
        while True:
            try:
                out.append(self.notifications.get_nowait())
            except queue.Empty:
                return out

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
                self.notifications.put(msg)
                continue
            if "method" in msg and "id" in msg:
                # server->client REQUEST (e.g. approval). We never auto-answer;
                # probes consume it via wait_server_request() and decide.
                self.server_requests.put(msg)
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
