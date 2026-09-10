#!/usr/bin/env python3
"""Probe 4 — read-only account/rate-limits read (matrix row 4).

Calls ONLY read-only methods:
- account/rateLimits/read (params: null)
- account/read            (params: {"refreshToken": false} — no token refresh)

Usage: python3 scripts/probe/probe_04_rate_limits.py [--out DIR]
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from appserver_client import AppServerClient, RpcError, RpcTimeout  # noqa: E402
from redact import scrub  # noqa: E402


def main():
    args = common.arg_parser("read-only rate limits probe").parse_args()
    evidence = {"probe": "04-rate-limits", "calls": [], "ok": False,
                "rate_limit_windows": None}
    client = AppServerClient(
        [args.codex, "app-server"], cwd=common.REPO_ROOT, env=common.fresh_environ()
    )
    try:
        client.start()
        init = client.request("initialize", common.INITIALIZE_PARAMS, timeout=15.0)
        evidence["calls"].append({"method": "initialize", "result": scrub(init)})

        rl = client.request("account/rateLimits/read", None, timeout=15.0)
        evidence["calls"].append({"method": "account/rateLimits/read", "result": scrub(rl)})
        if isinstance(rl, dict):
            snap = rl.get("rateLimits") or {}
            primary = snap.get("primary") or {}
            secondary = snap.get("secondary") or {}
            evidence["rate_limit_windows"] = {
                "primary_used_percent": primary.get("usedPercent"),
                "primary_window_minutes": primary.get("windowDurationMins"),
                "primary_resets_in_seconds": primary.get("resetsAt"),
                "secondary_used_percent": secondary.get("usedPercent"),
                "secondary_window_minutes": secondary.get("windowDurationMins"),
            }

        acct = client.request("account/read", {"refreshToken": False}, timeout=15.0)
        evidence["calls"].append({"method": "account/read", "params": {"refreshToken": False}, "result": scrub(acct)})

        evidence["ok"] = True
        common.summarize("rate-limits", True, json.dumps(evidence["rate_limit_windows"]))
    except (RpcError, RpcTimeout, RuntimeError) as exc:
        evidence["error"] = str(exc)
        common.summarize("rate-limits", False, str(exc)[:200])
    finally:
        evidence["stderr_tail"] = list(client.stderr_tail)
        client.stop()
    path = common.write_evidence(args.out, "04-rate-limits.json", scrub(evidence))
    print("evidence:", common.scrub_path(path))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
