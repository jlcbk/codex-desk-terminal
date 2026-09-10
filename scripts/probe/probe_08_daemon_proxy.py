#!/usr/bin/env python3
"""Probe 8 — daemon status and read-only proxy attach (matrix row 8).

- `codex app-server daemon version` (read-only): reports whether a local
  app-server daemon is already running, plus CLI/daemon versions.
- If a daemon IS running: attach via `codex app-server proxy`, initialize,
  and call ONLY read-only methods (thread/list, thread/loaded/list), then
  optionally listen for notifications (--listen seconds).
- If no daemon is running: record that fact. This probe never starts,
  restarts or stops the daemon (that would change user-visible state).

Usage: python3 scripts/probe/probe_08_daemon_proxy.py [--out DIR] [--listen 15]
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from appserver_client import AppServerClient, RpcError, RpcTimeout  # noqa: E402
from redact import scrub  # noqa: E402


def main():
    ap = common.arg_parser("daemon status + read-only proxy probe")
    ap.add_argument("--listen", type=float, default=0.0, help="seconds to passively listen after list calls")
    args = ap.parse_args()
    evidence = {
        "probe": "08-daemon-proxy",
        "daemon_version_exit": None,
        "daemon_version_output": None,
        "daemon_running": False,
        "proxy_attached": False,
        "note": "0.152.0 has no `daemon status` subcommand; `daemon version` is the read-only liveness check",
    }

    try:
        proc = subprocess.run(
            [args.codex, "app-server", "daemon", "version"],
            capture_output=True, text=True, timeout=15,
        )
        evidence["daemon_version_exit"] = proc.returncode
        evidence["daemon_version_output"] = (proc.stdout or "")[:800] or (proc.stderr or "")[:800]
        evidence["daemon_running"] = (proc.returncode == 0)
    except subprocess.TimeoutExpired:
        evidence["daemon_version_output"] = "timeout"

    print("daemon version exit=%s running=%s" % (evidence["daemon_version_exit"], evidence["daemon_running"]))

    if evidence["daemon_running"]:
        client = AppServerClient(
            [args.codex, "app-server", "proxy"],
            cwd=common.REPO_ROOT, env=common.fresh_environ(),
        )
        try:
            client.start()
            init = client.request("initialize", common.INITIALIZE_PARAMS, timeout=15.0)
            evidence["initialize_result"] = scrub(init)
            listed = client.request("thread/list", {"limit": 5}, timeout=20.0)
            evidence["thread_list_count"] = len(listed.get("data", [])) if isinstance(listed, dict) else None
            loaded = client.request("thread/loaded/list", {"limit": 50}, timeout=15.0)
            ldata = loaded.get("data", []) if isinstance(loaded, dict) else []
            evidence["loaded_list_count"] = len(ldata)
            evidence["loaded_threads"] = scrub(ldata)
            evidence["proxy_attached"] = True
            if args.listen > 0:
                import time
                print("listening %.0fs on the daemon connection ..." % args.listen)
                seen = []
                deadline = time.monotonic() + args.listen
                while time.monotonic() < deadline:
                    note = client.wait_notification(timeout=2.0)
                    if note is not None:
                        seen.append({"at": common.now_ms(), "notification": scrub(note)})
                        print("  notification:", note.get("method"))
                evidence["notifications"] = seen
                evidence["methods_seen"] = sorted({n["notification"].get("method") for n in seen})
            common.summarize(
                "daemon-proxy", True,
                "attached; thread/list=%s loaded=%s methods=%s" % (
                    evidence.get("thread_list_count"), evidence.get("loaded_list_count"),
                    evidence.get("methods_seen", [])),
            )
        except (RpcError, RpcTimeout, RuntimeError) as exc:
            evidence["proxy_error"] = str(exc)
            common.summarize("daemon-proxy", False, str(exc)[:200])
        finally:
            evidence["stderr_tail"] = list(client.stderr_tail)
            client.stop()
    else:
        common.summarize(
            "daemon-proxy", True,
            "no running daemon detected (exit=%s); desktop-observed channel via proxy unavailable" % evidence["daemon_version_exit"],
        )

    path = common.write_evidence(args.out, "08-daemon-proxy.json", scrub(evidence))
    print("evidence:", common.scrub_path(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
