#!/usr/bin/env python3
"""Extract redacted protocol samples from probe evidence into docs/proto-samples/.

Reads artifacts/probe/*.json (written by probe_02..08 scripts, already
redacted via redact.scrub) and writes one JSON file per protocol message.
Re-run after re-running probes to refresh the samples.

Usage: python3 scripts/probe/make_samples.py [--evidence DIR] [--out DIR]
"""

import argparse
import json
import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPTS_DIR))


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def call_result(evidence, method):
    for c in evidence.get("calls", []):
        if c.get("method") == method:
            return c.get("params"), c.get("result")
    return None, None


def dump(out_dir, relpath, obj):
    path = os.path.join(out_dir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("wrote", os.path.relpath(path, REPO_ROOT))


def find_event(evidence, method, nth=0):
    hits = [n["notification"] for n in evidence.get("notifications", [])
            if (n.get("notification") or {}).get("method") == method]
    return hits[nth] if len(hits) > nth else None


def main():
    ap = argparse.ArgumentParser(description="extract redacted protocol samples")
    ap.add_argument("--evidence", default=os.path.join(REPO_ROOT, "artifacts", "probe"))
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "docs", "proto-samples"))
    args = ap.parse_args()
    ev = args.evidence

    p02 = load(os.path.join(ev, "02-initialize.json"))
    p03 = load(os.path.join(ev, "03-thread-list.json"))
    p04 = load(os.path.join(ev, "04-rate-limits.json"))
    p06b = load(os.path.join(ev, "06b-approval-observable.json"))
    p07 = load(os.path.join(ev, "07-bridge-owned-session.json"))

    # ---- requests (client -> server) ---------------------------------
    dump(args.out, "requests/initialize.request.json",
         {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": p02["request"]})
    lp, lr = call_result(p03, "thread/list")
    dump(args.out, "requests/thread.list.request.json",
         {"jsonrpc": "2.0", "id": 2, "method": "thread/list", "params": lp or {"limit": 5}})
    rp, rr = call_result(p04, "account/rateLimits/read")
    dump(args.out, "requests/account.rateLimits.read.request.json",
         {"jsonrpc": "2.0", "id": 3, "method": "account/rateLimits/read", "params": None})
    dump(args.out, "requests/thread.start.request.json",
         {"jsonrpc": "2.0", "id": 4, "method": "thread/start",
          "params": p07.get("thread_start_params")})
    dump(args.out, "requests/turn.start.request.json",
         {"jsonrpc": "2.0", "id": 5, "method": "turn/start",
          "params": {"threadId": p07.get("thread_id"),
                     "input": [{"type": "text", "text": "只回复 ok"}]}})
    dump(args.out, "requests/turn.interrupt.request.json",
         {"jsonrpc": "2.0", "id": 6, "method": "turn/interrupt",
          "params": {"threadId": p06b.get("thread_id"), "turnId": p06b.get("turn_id")}})
    # server -> client REQUEST (approval): recorded unanswered by probe 06b
    if p06b.get("approval_request"):
        dump(args.out, "requests/server.item.commandExecution.requestApproval.unanswered.json",
             p06b["approval_request"])

    # ---- responses (server -> client results) -------------------------
    dump(args.out, "responses/initialize.response.json", p02.get("response"))
    dump(args.out, "responses/thread.list.response.json", call_result(p03, "thread/list")[1])
    dump(args.out, "responses/thread.loaded-list.response.json",
         call_result(p03, "thread/loaded/list")[1])
    dump(args.out, "responses/account.rateLimits.read.response.json",
         call_result(p04, "account/rateLimits/read")[1])
    dump(args.out, "responses/account.read.response.json",
         call_result(p04, "account/read")[1])
    dump(args.out, "responses/thread.start.response.json", p07.get("thread_start_response"))
    dump(args.out, "responses/turn.start.response.json", p07.get("turn_start_response"))

    # ---- events (server -> client notifications) ----------------------
    wanted = [
        ("thread.started", "thread/started", 0),
        ("thread.status.changed.active", "thread/status/changed", 0),
        ("turn.started", "turn/started", 0),
        ("item.started.agentMessage", "item/started", 1),
        ("item.completed.agentMessage", "item/completed", 1),
        ("item.agentMessage.delta", "item/agentMessage/delta", 0),
        ("thread.tokenUsage.updated", "thread/tokenUsage/updated", 0),
        ("account.rateLimits.updated", "account/rateLimits/updated", 0),
        ("turn.completed", "turn/completed", 0),
    ]
    for name, method, nth in wanted:
        ev_obj = find_event(p07, method, nth)
        if ev_obj is not None:
            dump(args.out, "events/%s.json" % name, ev_obj)
        else:
            print("missing event sample:", method)

    # approval-waiting status flag (from probe 06b, bridge-owned)
    waiting = [n["notification"] for n in p06b.get("notifications", [])
               if (n.get("notification") or {}).get("method") == "thread/status/changed"
               and ((n["notification"].get("params") or {}).get("status") or {}).get("activeFlags")]
    if waiting:
        dump(args.out, "events/thread.status.changed.waitingOnApproval.json", waiting[0])
    resolved = find_event(p06b, "serverRequest/resolved", 0)
    if resolved is not None:
        dump(args.out, "events/serverRequest.resolved.json", resolved)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
