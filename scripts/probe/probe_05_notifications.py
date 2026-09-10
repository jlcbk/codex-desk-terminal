#!/usr/bin/env python3
"""Probe 5 — passive notification listening (matrix row 5).

Two modes (both strictly read-only after `initialize`):

- stdio (default): start our OWN `codex app-server` instance and listen for
  server notifications for --seconds. Any turn/* events here can only come
  from threads created in this same process (bridge-owned attribution).
- --proxy: attach to the RUNNING app-server daemon control socket via
  `codex app-server proxy` (never starts or stops the daemon) and listen.
  Notifications arriving here would come from other clients of the same
  daemon — i.e. desktop-observed territory.

Usage: python3 scripts/probe/probe_05_notifications.py [--out DIR]
       [--seconds 30] [--proxy] [--with-thread-list]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from appserver_client import AppServerClient, RpcError, RpcTimeout  # noqa: E402
from redact import scrub  # noqa: E402


def listen_loop(client, seconds):
    seen = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        note = client.wait_notification(timeout=min(5.0, max(0.2, deadline - time.monotonic())))
        if note is not None:
            seen.append({"at": common.now_ms(), "notification": scrub(note)})
            print("  notification:", note.get("method"))
    return seen


def main():
    ap = common.arg_parser("passive notification listening probe")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--proxy", action="store_true",
                    help="listen through `codex app-server proxy` (requires a running daemon)")
    ap.add_argument("--with-thread-list", action="store_true",
                    help="call thread/list + thread/loaded/list once before listening (read-only)")
    args = ap.parse_args()

    mode = "proxy" if args.proxy else "stdio"
    argv = [args.codex, "app-server", "proxy"] if args.proxy else [args.codex, "app-server"]
    evidence = {
        "probe": "05-notifications",
        "mode": mode,
        "argv": argv,
        "seconds": args.seconds,
        "notifications": [],
        "methods_seen": [],
        "ok": False,
    }
    client = AppServerClient(argv, cwd=common.REPO_ROOT, env=common.fresh_environ())
    try:
        client.start()
        init = client.request("initialize", common.INITIALIZE_PARAMS, timeout=15.0)
        print("initialize ok")
        if args.with_thread_list:
            try:
                listed = client.request("thread/list", {"limit": 5}, timeout=20.0)
                n = len(listed.get("data", [])) if isinstance(listed, dict) else None
                print("thread/list ->", n, "items")
                loaded = client.request("thread/loaded/list", {"limit": 20}, timeout=15.0)
                ldata = loaded.get("data", []) if isinstance(loaded, dict) else []
                print("thread/loaded/list ->", len(ldata), "items")
                evidence["thread_list_count"] = n
                evidence["loaded_list_count"] = len(ldata)
                evidence["loaded_threads"] = scrub(ldata)
            except (RpcError, RpcTimeout) as exc:
                evidence["list_error"] = str(exc)
                print("  list calls failed:", str(exc)[:120])
        print("listening for %ss ..." % args.seconds)
        evidence["notifications"] = listen_loop(client, args.seconds)
        evidence["methods_seen"] = sorted({n["notification"].get("method") for n in evidence["notifications"]})
        evidence["ok"] = True
        common.summarize(
            "notifications(%s)" % mode, True,
            "%d notifications in %ss: %s" % (len(evidence["notifications"]), args.seconds, evidence["methods_seen"]),
        )
    except (RpcError, RpcTimeout, RuntimeError) as exc:
        evidence["error"] = str(exc)
        common.summarize("notifications(%s)" % mode, False, str(exc)[:200])
    finally:
        evidence["stderr_tail"] = list(client.stderr_tail)
        client.stop()
    fname = "05-notifications-%s.json" % mode
    path = common.write_evidence(args.out, fname, scrub(evidence))
    print("evidence:", common.scrub_path(path))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
