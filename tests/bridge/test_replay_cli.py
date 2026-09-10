"""`python -m bridge` CLI 与 JSONL 回放端到端测试（命令名对齐 DEVELOPMENT_PLAN §8）。"""

import json
import os
import subprocess
import sys

import pytest

from conftest import REPO_ROOT, assert_invariants


def run_cli(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "bridge", *args],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
    )


def test_cli_mock_lifecycle_stdout_jsonl(validator):
    proc = run_cli("--source", "mock", "--scenario", "lifecycle")
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) == 10
    snaps = [json.loads(l) for l in lines]
    assert snaps[0]["seq"] == 1 and snaps[-1]["seq"] == 10
    for snap in snaps:
        errors = list(validator.iter_errors(snap))
        assert not errors, errors[0].message
        assert_invariants(snap)
    assert snaps[-1]["threads"][0]["state"] == "done"


def test_cli_mock_default_scenario_is_lifecycle():
    a = run_cli("--source", "mock")
    b = run_cli("--source", "mock", "--scenario", "lifecycle")
    assert a.returncode == b.returncode == 0
    assert a.stdout == b.stdout  # 确定性：两次运行字节相同


def test_cli_out_writes_increasing_snapshot_files():
    import pathlib

    out = REPO_ROOT / "artifacts" / "bridge" / "cli-test"
    proc = run_cli("--source", "mock", "--scenario", "lifecycle", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    files = sorted(pathlib.Path(out).glob("snapshot_*.json"))
    assert [f.name for f in files] == [f"snapshot_{i:04d}.json" for i in range(1, 11)]
    snap = json.loads(files[-1].read_text(encoding="utf-8"))
    assert snap["threads"][0]["state"] == "done"
    assert snap["bridge_epoch"] == "mock-run-001"


def test_cli_replay_fixture(validator):
    fixture = REPO_ROOT / "tests" / "fixtures" / "bridge" / "lifecycle_events.jsonl"
    proc = run_cli("--source", "replay", "--file", str(fixture), "--epoch", "replay-001")
    assert proc.returncode == 0, proc.stderr
    snaps = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    assert len(snaps) == 10
    for snap in snaps:
        assert not list(validator.iter_errors(snap))


def test_cli_replay_with_mock_anchor_is_byte_identical():
    import pathlib

    fixture = REPO_ROOT / "tests" / "fixtures" / "bridge" / "lifecycle_events.jsonl"
    out = REPO_ROOT / "artifacts" / "bridge" / "cli-replay-anchor"
    proc = run_cli("--source", "replay", "--file", str(fixture),
                   "--epoch", "mock-run-001", "--anchor-ms", "1789002000000",
                   "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    mock_out = REPO_ROOT / "artifacts" / "bridge" / "cli-test"
    run_cli("--source", "mock", "--scenario", "lifecycle", "--out", str(mock_out))
    for i in range(1, 11):
        a = (mock_out / f"snapshot_{i:04d}.json").read_bytes()
        b = (out / f"snapshot_{i:04d}.json").read_bytes()
        assert a == b  # 同事件序列 + 同 anchor ⇒ 字节相同


def test_cli_usage_errors_exit_2():
    assert run_cli("--source", "mock", "--scenario", "bogus").returncode == 2
    assert run_cli("--source", "replay").returncode == 2
    assert run_cli("--source", "replay", "--file", "/no/such/file.jsonl").returncode == 2


def test_replay_bad_json_line_has_lineno(tmp_path):
    from bridge.sources import replay

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type":"thread_started"}\nnot-json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="bad.jsonl:2"):
        replay.load_events(str(bad))


def test_replay_missing_at_ms_reuses_last_clock(tmp_path):
    from bridge.sources import replay

    f = tmp_path / "no_clock.jsonl"
    f.write_text(
        '{"type":"thread_started","thread_id":"t1","at_ms":5}\n'
        '{"type":"turn_started","thread_id":"t1","turn_id":"u1"}\n',
        encoding="utf-8",
    )
    snaps = replay.run_file(str(f), utc_anchor_ms=0)
    assert snaps[-1]["generated_at_ms"] == 5  # 缺 at_ms 沿用上一时间点
