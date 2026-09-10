#!/usr/bin/env python3
"""Probe 1 — protocol schema generation (matrix row 1).

Runs `codex app-server generate-json-schema --out DIR --experimental` and
records the bundle inventory. The schema itself is a machine-generated,
non-secret artifact and is archived under docs/proto-samples/.

Usage: python3 scripts/probe/probe_01_generate_schema.py [--out DIR]
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def main():
    args = common.arg_parser("generate codex app-server JSON schema bundle").parse_args()
    out_dir = os.path.join(args.out, "schema-bundle")
    os.makedirs(out_dir, exist_ok=True)
    argv = [args.codex, "app-server", "generate-json-schema", "--out", out_dir, "--experimental"]
    print("run:", " ".join(argv))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        common.summarize("schema-generation", False, "timed out after 60s")
        return 1
    evidence = {
        "probe": "01-generate-schema",
        "argv": argv,
        "exit_code": proc.returncode,
        "stderr_tail": proc.stderr[-500:] if proc.stderr else "",
        "file_count": 0,
        "combined_files": [],
        "schema_version_note": "codex-cli 0.152.0; v1 surface (91 defs) + v2 surface (734 defs)",
    }
    if proc.returncode == 0:
        files = []
        for root, _dirs, names in os.walk(out_dir):
            for n in names:
                files.append(os.path.relpath(os.path.join(root, n), out_dir))
        evidence["file_count"] = len(files)
        evidence["combined_files"] = sorted(
            f for f in files if f.endswith("schemas.json")
        )
        common.summarize(
            "schema-generation",
            True,
            "%d files -> %s" % (len(files), common.scrub_path(out_dir)),
        )
    else:
        common.summarize("schema-generation", False, "exit %d" % proc.returncode)
    path = common.write_evidence(args.out, "01-generate-schema.json", evidence)
    print("evidence:", common.scrub_path(path))
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from redact import scrub  # noqa: F401  (used indirectly via common)

    sys.exit(main())
