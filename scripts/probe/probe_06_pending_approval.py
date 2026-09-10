#!/usr/bin/env python3
"""Probe 6 — read-only visibility of pending approvals (matrix row 6).

NEVER resolves, approves or rejects anything. Only:
- thread/list / thread/loaded/list -> inspect Thread.status.activeFlags for
  "waitingOnApproval" / "waitingOnUserInput" (schema: ThreadActiveFlag).
- optionally listens briefly for thread/status/changed notifications.

Conclusion about observability (does a status expose pending approvals?) is
derived from schema + live data; see docs/CODEX_CAPABILITIES.md.

Usage: python3 scripts/probe/probe_06_pending_approval.py [--out DIR] [--proxy]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from appserver_client import AppServerClient, RpcError, RpcTimeout  # noqa: E402
from redact import scrub  # noqa: E402

WAIT_FLAGS = ("waitingOnApproval", "waitingOnUserInput")


def summarize_threads(tag, data):
    rows = []
    for t in data:
        st = t.get("status") or {}
        rows.append({
            "id": t.get("id"),
            "status_type": st.get("type"),
            "activeFlags": st.get("activeFlags"),
        })
        if st.get("type") == "active" and any(f in WAIT_FLAGS for f in (st.get("activeFlags") or [])):
            print("  [%s] WAITING thread %s flags=%s" % (tag, t.get("id"), st.get("activeFlags")))
    return rows


def main():
    ap = common.arg_parser("read-only pending approval visibility probe")
    ap.add_argument("--proxy", action="store_true", help="scan through the running daemon proxy")
    ap.add_argument("--listen", type=float, default=0.0, help="extra seconds to listen for thread/status/changed")
    args = ap.parse_args()

    argv = [args.codex, "app-server", "proxy"] if args.proxy else [args.codex, "app-server"]
    evidence = {
        "probe": "06-pending-approval",
        "mode": "proxy" if args.proxy else "stdio",
        "schema_fact": "ThreadActiveFlag enum = ['waitingOnApproval','waitingOnUserInput']; "
                       "Thread.status = ActiveThreadStatus{activeFlags[]} for type=active",
        "threads_seen": [],
        "waiting_threads": [],
        "status_notifications": [],
        "ok": False,
    }
    client = AppServerClient(argv, cwd=common.REPO_ROOT, env=common.fresh_environ())
    try:
        client.start()
        client.request("initialize", common.INITIALIZE_PARAMS, timeout=15.0)
        listed = client.request("thread/list", {"limit": 50}, timeout=20.0)
        data = listed.get("data", []) if isinstance(listed, dict) else []
        rows = summarize_threads("thread/list", data)
        evidence["threads_seen"].extend(rows)
        loaded = client.request("thread/loaded/list", {"limit": 50}, timeout=15.0)
        ldata = loaded.get("data", []) if isinstance(loaded, dict) else []
        rows_l = summarize_threads("thread/loaded/list", ldata)
        evidence["threads_seen"].extend(rows_l)
        for r in rows + rows_l:
            st = r.get("status_type")
            flags = r.get("activeFlags") or []
            if st == "active" and any(f in WAIT_FLAGS for f in flags):
                evidence["waiting_threads"].append(r)
        if args.listen > 0:
            print("listening %.0fs for thread/status/changed ..." % args.listen)
            deadline = time.monotonic() + args.listen
            while time.monotonic() < deadline:
                note = client.wait_notification(timeout=2.0)
                if note is not None and note.get("method") == "thread/status/changed":
                    evidence["status_notifications"].append(scrub(note))
                    print("  thread/status/changed:", scrub(note).get("params", {}).get("threadId"))
        evidence["ok"] = True
        common.summarize(
            "pending-approval-scan(%s)" % evidence["mode"], True,
            "%d threads scanned, %d waiting" % (len(evidence["threads_seen"]), len(evidence["waiting_threads"])),
        )
    except (RpcError, RpcTimeout, RuntimeError) as exc:
        evidence["error"] = str(exc)
        common.summarize("pending-approval-scan", False, str(exc)[:200])
    finally:
        evidence["stderr_tail"] = list(client.stderr_tail)
        client.stop()
    fname = "06-pending-approval-%s.json" % evidence["mode"]
    path = common.write_evidence(args.out, fname, scrub(evidence))
    print("evidence:", common.scrub_path(path))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
