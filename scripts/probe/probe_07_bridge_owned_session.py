#!/usr/bin/env python3
"""Probe 7 — bridge-owned session lifecycle (matrix row 7).

The ONLY probe with a write action, and it writes nothing outside a fresh
isolated temp directory:

1. mkdtemp a brand-new cwd (/tmp/codex-probe-<rand>)
2. start our own stdio app-server, initialize
3. thread/start with cwd=<tmpdir>, ephemeral=true (not persisted in user
   history), approvalPolicy="never", sandboxPolicy={"type":"readOnly"},
   networkAccess=false  -> the model literally cannot touch anything else
4. turn/start with input "只回复 ok" and collect the full notification
   lifecycle until turn/completed (or timeout)
5. stop the server, delete the temp dir

Never touches pre-existing threads.

Usage: python3 scripts/probe/probe_07_bridge_owned_session.py [--out DIR]
       [--seconds 90] [--keep-server-seconds 5]
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

PROMPT = "只回复 ok"


def main():
    ap = common.arg_parser("bridge-owned session lifecycle probe")
    ap.add_argument("--seconds", type=float, default=90.0, help="max wait for turn completion")
    ap.add_argument("--model", default="gpt-5.6-sol",
                    help="model slug to pin for the probe turn (0.152.0 supports gpt-5.6-sol/"
                         "gpt-5.6-terra/gpt-5.6-luna/gpt-5.5; the account default gpt-6-astra "
                         "requires a newer CLI and fails with a 400 error)")
    ap.add_argument("--with-plan-turn", action="store_true",
                    help="after the minimal turn, send one more turn in the SAME isolated "
                         "thread asking for a tiny plan, to capture turn/plan/updated naming")
    args = ap.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="codex-probe-")
    evidence = {
        "probe": "07-bridge-owned-session",
        "temp_cwd": common.scrub_path(tmpdir),
        "thread_start_params": None,
        "notifications": [],
        "methods_seen": [],
        "turn_status": None,
        "ok": False,
    }
    client = AppServerClient(
        [args.codex, "app-server"], cwd=tmpdir, env=common.fresh_environ()
    )
    try:
        client.start()
        client.request("initialize", common.INITIALIZE_PARAMS, timeout=15.0)
        print("initialized; starting ephemeral thread in", common.scrub_path(tmpdir))

        start_params = {
            "cwd": tmpdir,
            "ephemeral": True,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "model": args.model,
        }
        started = client.request("thread/start", start_params, timeout=30.0)
        thread_id = started.get("thread", {}).get("id") if isinstance(started, dict) else None
        if not thread_id and isinstance(started, dict):
            thread_id = started.get("id")
        evidence["thread_start_params"] = scrub(start_params)
        evidence["thread_start_response"] = scrub(started)
        evidence["thread_id"] = thread_id
        print("thread/start ok -> thread", thread_id)

        turn = client.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": PROMPT}]},
            timeout=30.0,
        )
        turn_obj = turn.get("turn", {}) if isinstance(turn, dict) else {}
        turn_id = turn_obj.get("id")
        evidence["turn_id"] = turn_id
        evidence["turn_start_response"] = scrub(turn)
        print("turn/start ok -> turn", turn_id)

        deadline = time.monotonic() + args.seconds
        completed = False
        while time.monotonic() < deadline:
            note = client.wait_notification(timeout=min(5.0, max(0.2, deadline - time.monotonic())))
            if note is None:
                continue
            evidence["notifications"].append({"at": common.now_ms(), "notification": scrub(note)})
            m = note.get("method")
            if m and m not in evidence["methods_seen"]:
                evidence["methods_seen"].append(m)
                print("  event:", m)
            if m in ("error", "warning", "turn/completed"):
                # live console preview (not stored): helps diagnose failures
                print("    live params:", json.dumps(note.get("params"), ensure_ascii=False)[:400])
            if m == "turn/completed":
                evidence["turn_status"] = (note.get("params") or {}).get("turn", {}).get("status")
                completed = True
                break
            if m == "error":
                break

        if completed and args.with_plan_turn:
            print("sending second (plan) turn in the same isolated thread ...")
            turn2 = client.request(
                "turn/start",
                {"threadId": thread_id,
                 "input": [{"type": "text",
                            "text": "先给出不超过两步的简短计划，然后只回复 ok"}]},
                timeout=30.0,
            )
            evidence["turn2_id"] = (turn2.get("turn", {}) or {}).get("id") if isinstance(turn2, dict) else None
            deadline2 = time.monotonic() + args.seconds
            while time.monotonic() < deadline2:
                note = client.wait_notification(timeout=min(5.0, max(0.2, deadline2 - time.monotonic())))
                if note is None:
                    continue
                evidence["notifications"].append({"at": common.now_ms(), "notification": scrub(note)})
                m = note.get("method")
                if m and m not in evidence["methods_seen"]:
                    evidence["methods_seen"].append(m)
                    print("  event:", m)
                if m == "turn/completed":
                    evidence["turn2_status"] = (note.get("params") or {}).get("turn", {}).get("status")
                    break
                if m == "error":
                    break
        evidence["completed"] = completed
        evidence["ok"] = bool(thread_id) and completed
        common.summarize(
            "bridge-owned-session", evidence["ok"],
            "thread=%s events=%s turn_status=%s" % (thread_id, evidence["methods_seen"], evidence["turn_status"]),
        )
    except (RpcError, RpcTimeout, RuntimeError) as exc:
        evidence["error"] = str(exc)
        common.summarize("bridge-owned-session", False, str(exc)[:200])
    finally:
        evidence["stderr_tail"] = list(client.stderr_tail)
        client.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)
    path = common.write_evidence(args.out, "07-bridge-owned-session.json", scrub(evidence))
    print("evidence:", common.scrub_path(path))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
