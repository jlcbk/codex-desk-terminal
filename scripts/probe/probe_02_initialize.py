#!/usr/bin/env python3
"""Probe 2 — initialize handshake over stdio JSON-RPC (matrix row 2).

Starts `codex app-server`, sends `initialize` (method name verified in the
generated schema), records the response (protocol/user-agent facts), then
shuts down cleanly.

Usage: python3 scripts/probe/probe_02_initialize.py [--out DIR]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from appserver_client import AppServerClient, RpcError, RpcTimeout  # noqa: E402
from redact import scrub  # noqa: E402


def main():
    args = common.arg_parser("initialize handshake probe").parse_args()
    evidence = {
        "probe": "02-initialize",
        "request": common.INITIALIZE_PARAMS,
        "response": None,
        "notifications_on_startup": [],
        "stderr_tail": [],
        "ok": False,
    }
    client = AppServerClient(
        [args.codex, "app-server"], cwd=common.REPO_ROOT, env=common.fresh_environ()
    )
    try:
        client.start()
        # a couple of notifications can arrive right after startup
        note = client.wait_notification(5.0)
        while note is not None:
            evidence["notifications_on_startup"].append(scrub(note))
            note = client.wait_notification(1.0)
        result = client.request("initialize", common.INITIALIZE_PARAMS, timeout=15.0)
        evidence["response"] = scrub(result)
        evidence["ok"] = True
        common.summarize("initialize", True, "result keys: %s" % sorted(result.keys()) if isinstance(result, dict) else repr(result)[:120])
        print(json_pretty(result))
    except (RpcError, RpcTimeout, RuntimeError) as exc:
        evidence["error"] = str(exc)
        common.summarize("initialize", False, str(exc)[:200])
    finally:
        evidence["stderr_tail"] = list(client.stderr_tail)
        client.stop()
    path = common.write_evidence(args.out, "02-initialize.json", scrub(evidence))
    print("evidence:", common.scrub_path(path))
    return 0 if evidence["ok"] else 1


def json_pretty(obj):
    import json

    return json.dumps(scrub(obj), indent=2, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
