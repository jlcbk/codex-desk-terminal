#!/usr/bin/env python3
"""Probe 6b — is a pending approval observable WITHOUT resolving it?

Bridge-owned variant (writes happen ONLY inside a fresh isolated temp cwd,
new ephemeral thread, exactly like probe 7):

1. start thread with approvalPolicy="on-request", sandbox readOnly
2. turn/start asking to run a harmless command -> the server sends a
   server->client REQUEST (item/commandExecution/requestApproval)
3. we DO NOT approve/reject. Instead we:
   a. record the pending server request (the approval-waiting observable)
   b. call thread/list + thread/loaded/list (read-only) and check
      Thread.status.activeFlags for "waitingOnApproval"
4. end the turn with turn/interrupt (never resolves the approval)
5. confirm the waiting flag clears via thread/status/changed or thread/list

Usage: python3 scripts/probe/probe_06b_approval_observable.py [--out DIR]
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from appserver_client import AppServerClient, RpcError, RpcTimeout  # noqa: E402
from redact import scrub  # noqa: E402


def collect(client, seconds, store, methods_seen, stop_on=("item/commandExecution/requestApproval",)):
    """Collect notifications AND unanswered server requests for up to seconds."""
    deadline = time.monotonic() + seconds
    hit = None
    while time.monotonic() < deadline:
        got = client.wait_notification(timeout=0.2)
        if got is None:
            got = client.wait_server_request(timeout=0.2)
        if got is None:
            if time.monotonic() >= deadline:
                break
            continue
        store.append({"at": common.now_ms(), "notification": scrub(got)})
        m = got.get("method")
        if m and m not in methods_seen:
            methods_seen.append(m)
            print("  event:", m)
        if m in stop_on:
            hit = got
            break
    return hit


def status_from_store(store, thread_id):
    """Extract the latest ThreadStatus for thread_id from collected
    thread/status/changed notifications (read-only push channel)."""
    latest = None
    for entry in store:
        n = entry.get("notification") or {}
        if n.get("method") == "thread/status/changed":
            p = n.get("params") or {}
            if p.get("threadId") == thread_id:
                latest = p.get("status")
    if latest is None:
        return None
    return {"source": "thread/status/changed notification",
            "status_type": latest.get("type"),
            "activeFlags": latest.get("activeFlags")}


def observe_thread_status(client, thread_id, store=None):
    """Read-only status lookup: thread/list (index; ephemeral threads absent),
    falling back to collected thread/status/changed notifications."""
    try:
        listed = client.request("thread/list", {"limit": 20}, timeout=15.0)
        for t in listed.get("data", []) if isinstance(listed, dict) else []:
            if t.get("id") == thread_id:
                st = t.get("status") or {}
                return {"source": "thread/list",
                        "status_type": st.get("type"),
                        "activeFlags": st.get("activeFlags")}
    except (RpcError, RpcTimeout):
        pass
    if store is not None:
        return status_from_store(store, thread_id)
    return None


def main():
    args = common.arg_parser("read-only pending approval observability probe").parse_args()
    tmpdir = tempfile.mkdtemp(prefix="codex-probe-approval-")
    evidence = {
        "probe": "06b-approval-observable",
        "temp_cwd": common.scrub_path(tmpdir),
        "notifications": [],
        "approval_request_seen": False,
        "approval_request": None,
        "waiting_flags_while_pending": None,
        "waiting_flags_after_interrupt": None,
        "interrupted": False,
        "ok": False,
    }
    client = AppServerClient(
        [args.codex, "app-server"], cwd=tmpdir, env=common.fresh_environ()
    )
    try:
        client.start()
        client.request("initialize", common.INITIALIZE_PARAMS, timeout=15.0)
        started = client.request(
            "thread/start",
            {
                "cwd": tmpdir,
                "ephemeral": True,
                "approvalPolicy": "untrusted",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "model": "gpt-5.6-sol",
            },
            timeout=30.0,
        )
        thread_id = started.get("thread", {}).get("id") if isinstance(started, dict) else None
        evidence["thread_id"] = thread_id
        print("thread:", thread_id)
        methods_seen = []
        turn = client.request(
            "turn/start",
            {"threadId": thread_id,
             "input": [{"type": "text",
                        "text": "运行 `uname -a` 并原样告诉我输出。"}]},
            timeout=30.0,
        )
        turn_id = (turn.get("turn", {}) or {}).get("id") if isinstance(turn, dict) else None
        evidence["turn_id"] = turn_id
        approval = collect(client, 90.0, evidence["notifications"], methods_seen,
                           stop_on=("item/commandExecution/requestApproval",))
        if approval is not None and approval.get("method") == "item/commandExecution/requestApproval":
            evidence["approval_request_seen"] = True
            evidence["approval_request"] = scrub(approval)
            print("approval request received (NOT answered)")
            # keep collecting for a few seconds: the thread/status/changed
            # notification carries the waitingOnApproval flag
            collect(client, 8.0, evidence["notifications"], methods_seen, stop_on=())
            # read-only observation of the waiting state while it pends
            evidence["waiting_flags_while_pending"] = observe_thread_status(
                client, thread_id, store=evidence["notifications"])
            print("status while pending:", evidence["waiting_flags_while_pending"])
        # NEVER answer the approval. Cancel the turn instead.
        try:
            client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                           timeout=15.0)
            evidence["interrupted"] = True
            print("turn interrupted (approval left unresolved)")
        except (RpcError, RpcTimeout) as exc:
            evidence["interrupt_error"] = str(exc)
        collect(client, 10.0, evidence["notifications"], methods_seen, stop_on=())
        evidence["waiting_flags_after_interrupt"] = observe_thread_status(
            client, thread_id, store=evidence["notifications"])
        print("status after interrupt:", evidence["waiting_flags_after_interrupt"])
        evidence["methods_seen"] = methods_seen
        evidence["ok"] = evidence["approval_request_seen"] and evidence["interrupted"]
        common.summarize(
            "approval-observable", evidence["ok"],
            "approval seen=%s waiting_flags=%s" % (
                evidence["approval_request_seen"], evidence["waiting_flags_while_pending"]),
        )
    except (RpcError, RpcTimeout, RuntimeError) as exc:
        evidence["error"] = str(exc)
        common.summarize("approval-observable", False, str(exc)[:200])
    finally:
        evidence["stderr_tail"] = list(client.stderr_tail)
        client.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)
    path = common.write_evidence(args.out, "06b-approval-observable.json", scrub(evidence))
    print("evidence:", common.scrub_path(path))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
