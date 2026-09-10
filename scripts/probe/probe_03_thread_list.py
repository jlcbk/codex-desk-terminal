#!/usr/bin/env python3
"""Probe 3 — read-only thread listing (matrix row 3).

Initializes a fresh stdio app-server, then calls ONLY read-only methods:
- thread/list        (persisted thread index, paginated)
- thread/loaded/list (threads currently loaded in this server instance)

Usage: python3 scripts/probe/probe_03_thread_list.py [--out DIR] [--limit 5]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from appserver_client import AppServerClient, RpcError, RpcTimeout  # noqa: E402
from redact import scrub  # noqa: E402


def main():
    ap = common.arg_parser("read-only thread list probe")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    evidence = {
        "probe": "03-thread-list",
        "calls": [],
        "thread_count": None,
        "loaded_thread_count": None,
        "ok": False,
    }
    client = AppServerClient(
        [args.codex, "app-server"], cwd=common.REPO_ROOT, env=common.fresh_environ()
    )
    try:
        client.start()
        init = client.request("initialize", common.INITIALIZE_PARAMS, timeout=15.0)
        evidence["calls"].append({"method": "initialize", "result": scrub(init)})

        listed = client.request("thread/list", {"limit": args.limit}, timeout=20.0)
        data = listed.get("data", []) if isinstance(listed, dict) else []
        evidence["thread_count"] = len(data)
        evidence["calls"].append({"method": "thread/list", "params": {"limit": args.limit}, "result": scrub(listed)})

        loaded = client.request("thread/loaded/list", {"limit": args.limit}, timeout=15.0)
        ldata = loaded.get("data", loaded) if isinstance(loaded, dict) else loaded
        if isinstance(ldata, dict) and "data" in ldata:
            ldata = ldata["data"]
        evidence["loaded_thread_count"] = len(ldata) if isinstance(ldata, list) else None
        evidence["calls"].append({"method": "thread/loaded/list", "params": {"limit": args.limit}, "result": scrub(loaded)})

        # statuses observed in the index (read-only facts for the matrix)
        statuses = []
        for t in data:
            st = t.get("status") or {}
            statuses.append({"id": t.get("id"), "status_type": st.get("type"), "activeFlags": st.get("activeFlags")})
        evidence["thread_statuses"] = statuses
        evidence["ok"] = True
        common.summarize(
            "thread-list",
            True,
            "thread/list -> %d items; loaded -> %s" % (len(data), evidence["loaded_thread_count"]),
        )
    except (RpcError, RpcTimeout, RuntimeError) as exc:
        evidence["error"] = str(exc)
        common.summarize("thread-list", False, str(exc)[:200])
    finally:
        evidence["stderr_tail"] = list(client.stderr_tail)
        client.stop()
    path = common.write_evidence(args.out, "03-thread-list.json", scrub(evidence))
    print("evidence:", common.scrub_path(path))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
