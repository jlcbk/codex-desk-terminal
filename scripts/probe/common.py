#!/usr/bin/env python3
"""Shared helpers for the P0.2 probe scripts (python3.9 stdlib only)."""

import argparse
import datetime
import json
import os
import sys
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPTS_DIR))
DEFAULT_OUT = os.path.join(REPO_ROOT, "artifacts", "probe")

try:  # re-export so probe scripts can call common.scrub / common.scrub_path
    from redact import scrub, scrub_path  # noqa: F401,E402
except ImportError:  # pragma: no cover - direct script execution fallback
    sys.path.insert(0, SCRIPTS_DIR)
    from redact import scrub, scrub_path  # noqa: F401,E402

CLIENT_INFO = {
    "name": "codex-desk-terminal-probe",
    "title": "P0.2 app-server capability probe",
    "version": "0.1.0",
}

INITIALIZE_PARAMS = {
    "clientInfo": CLIENT_INFO,
    "capabilities": {"experimentalApi": True},
}


def default_out_dir():
    os.makedirs(DEFAULT_OUT, exist_ok=True)
    return DEFAULT_OUT


def arg_parser(description):
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--out", default=DEFAULT_OUT, help="evidence output directory")
    p.add_argument("--codex", default="codex", help="codex CLI binary to use")
    return p


def write_evidence(out_dir, filename, payload):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path


def now_ms():
    return int(datetime.datetime.now().timestamp() * 1000)


def summarize(name, ok, detail):
    print("[%s] %s — %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh_environ():
    """Environment for the child process: inherit as-is (probe never mutates ~/.codex)."""
    return dict(os.environ)
