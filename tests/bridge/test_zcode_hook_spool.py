"""ZC2 离线单测：scripts/zcode_hook_spool.py（hook 写入端，生产脚本）。

覆盖（任务书第 1 节单测清单）：
- 正常追加：冻结四字段行、逐次调用追加不覆盖；
- 坏 stdin（非 JSON）容错：照样落行（session_id/tool_name=null）、exit 0、无 stdout；
- CDT_HOOK_SPOOL 覆盖默认路径；
- 并发追加：两次快速调用不互删（flock 覆盖查大小→轮转→写全程）；
- 轮转：spool >1MB 时写入端 truncate（读取端按 offset>size 重读语义容错）；
- 防御：未注册事件名不落盘仍 exit 0；缺目录自动创建。

策略：行为测试走真实子进程（exit code / stdout / 文件内容全链路）；
纯函数（build_record / resolve_spool_path）直接 importlib 加载断言。
红线自检：记录里绝不出现 prompt 内容 / tool_input / 环境变量。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT

SCRIPT_PATH = REPO_ROOT / "scripts" / "zcode_hook_spool.py"

FIELDS = {"event", "session_id", "tool_name", "received_at"}


def load_module():
    spec = importlib.util.spec_from_file_location("zcode_hook_spool_mod",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_hook(event: str, stdin_text: str, spool: Path | None,
             timeout: float = 10.0) -> subprocess.CompletedProcess:
    """真实子进程跑一次 hook（exit code/stdout 可观测）；CDT_HOOK_SPOOL 注入。"""
    env = dict(os.environ)
    env.pop("CDT_HOOK_SPOOL", None)
    if spool is not None:
        env["CDT_HOOK_SPOOL"] = str(spool)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), event],
        input=stdin_text, capture_output=True, text=True, env=env,
        timeout=timeout,
    )


def read_lines(spool: Path) -> list[dict]:
    return [json.loads(line) for line in
            spool.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 正常追加
# ---------------------------------------------------------------------------

def test_append_normal(tmp_path):
    spool = tmp_path / "spool.jsonl"
    proc = run_hook("PreToolUse",
                    '{"session_id":"sess-1","tool_name":"Read","tool_input":{"x":1}}',
                    spool)
    assert proc.returncode == 0
    assert proc.stdout == "", "红线：hook 写入端无 stdout"
    lines = read_lines(spool)
    assert len(lines) == 1
    rec = lines[0]
    assert set(rec) == FIELDS, "冻结四字段行"
    assert rec["event"] == "PreToolUse"
    assert rec["session_id"] == "sess-1"
    assert rec["tool_name"] == "Read"
    assert isinstance(rec["received_at"], str) and rec["received_at"]
    # 红线：prompt/tool_input/环境变量绝不落盘
    raw = spool.read_text(encoding="utf-8")
    assert "tool_input" not in raw and '"x":1' not in raw


def test_append_accumulates(tmp_path):
    """两次调用追加不覆盖（逐行累积）。"""
    spool = tmp_path / "spool.jsonl"
    run_hook("SessionStart", '{"session_id":"a"}', spool)
    run_hook("Stop", '{"session_id":"a"}', spool)
    lines = read_lines(spool)
    assert [r["event"] for r in lines] == ["SessionStart", "Stop"]


def test_seven_events_accepted(tmp_path):
    spool = tmp_path / "spool.jsonl"
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse",
                  "PermissionRequest", "PostToolUse", "PostToolUseFailure",
                  "Stop"):
        assert run_hook(event, "{}", spool).returncode == 0
    assert [r["event"] for r in read_lines(spool)] == [
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
        "PostToolUse", "PostToolUseFailure", "Stop"]


# ---------------------------------------------------------------------------
# 坏 stdin 容错
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_stdin", ["", "not-json-at-all", "[1,2,3]", '"str"'])
def test_bad_stdin_tolerated(tmp_path, bad_stdin):
    """非 JSON / 非 object 的 stdin 不致命：落行（session/tool=null）、exit 0。"""
    spool = tmp_path / "spool.jsonl"
    proc = run_hook("UserPromptSubmit", bad_stdin, spool)
    assert proc.returncode == 0
    assert proc.stdout == ""
    rec = read_lines(spool)[0]
    assert rec["event"] == "UserPromptSubmit"
    assert rec["session_id"] is None
    assert rec["tool_name"] is None


def test_missing_event_arg_no_write(tmp_path):
    """未注册/缺失事件名：不落盘（防污染 spool），仍 exit 0。"""
    spool = tmp_path / "spool.jsonl"
    assert run_hook("BogusEvent", '{"session_id":"x"}', spool).returncode == 0
    env = dict(os.environ)
    env["CDT_HOOK_SPOOL"] = str(spool)
    proc = subprocess.run([sys.executable, str(SCRIPT_PATH)],
                          input="{}", capture_output=True, text=True,
                          env=env, timeout=10)
    assert proc.returncode == 0
    assert not spool.exists()


# ---------------------------------------------------------------------------
# CDT_HOOK_SPOOL 覆盖
# ---------------------------------------------------------------------------

def test_env_override(tmp_path):
    """CDT_HOOK_SPOOL 指定路径生效；默认路径不被触碰。"""
    custom = tmp_path / "custom" / "hook.jsonl"
    proc = run_hook("PostToolUseFailure", '{"session_id":"e1"}', custom)
    assert proc.returncode == 0
    assert custom.exists(), "CDT_HOOK_SPOOL 覆盖路径被写入"
    assert read_lines(custom)[0]["event"] == "PostToolUseFailure"


def test_resolve_spool_path_default_and_env(tmp_path):
    mod = load_module()
    assert str(mod.resolve_spool_path({})).endswith(
        ".zcode/cli/cdt-hook-spool.jsonl")
    overridden = mod.resolve_spool_path({"CDT_HOOK_SPOOL": "/tmp/x.jsonl"})
    assert str(overridden) == "/tmp/x.jsonl"


def test_creates_missing_parent_dirs(tmp_path):
    spool = tmp_path / "a" / "b" / "c" / "spool.jsonl"
    run_hook("Stop", '{"session_id":"d1"}', spool)
    assert spool.is_file()


# ---------------------------------------------------------------------------
# 并发追加（两次快速调用不互删）
# ---------------------------------------------------------------------------

def test_concurrent_appends_no_loss(tmp_path):
    """两个写入端同时快速追加：flock 全程互斥，两行都落盘、互不删除。"""
    spool = tmp_path / "spool.jsonl"
    env = dict(os.environ)
    env["CDT_HOOK_SPOOL"] = str(spool)
    procs = [
        subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), event],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        for event in ("PreToolUse", "PostToolUse")
    ]
    for proc, event in zip(procs, ("PreToolUse", "PostToolUse")):
        out, err = proc.communicate(input='{"session_id":"c"}', timeout=10)
        assert proc.returncode == 0, err
        assert out == ""
    events = sorted(r["event"] for r in read_lines(spool))
    assert events == ["PostToolUse", "PreToolUse"], "两次并发追加都存活"


# ---------------------------------------------------------------------------
# 轮转（>1MB truncate）
# ---------------------------------------------------------------------------

def test_rotation_truncates_over_1mib(tmp_path):
    """spool 超过 1MiB：写入端先 truncate 再追加（读取端靠 offset>size 重读容错）。"""
    spool = tmp_path / "spool.jsonl"
    spool.write_text("x" * (1024 * 1024 + 4096), encoding="utf-8")
    assert spool.stat().st_size > 1024 * 1024
    run_hook("Stop", '{"session_id":"rot"}', spool)
    size = spool.stat().st_size
    assert size <= 1024 * 1024, "轮转后不超过阈值（truncate 生效）"
    lines = read_lines(spool)
    assert len(lines) == 1 and lines[0]["session_id"] == "rot"


# ---------------------------------------------------------------------------
# 纯函数与超时形状
# ---------------------------------------------------------------------------

def test_build_record_minimal_fields():
    mod = load_module()
    rec = mod.build_record(
        "PreToolUse",
        {"session_id": "s", "tool_name": "Bash",
         "prompt": "SECRET", "tool_input": {"cmd": "SECRET"},
         "cwd": "/home/user", "hook_event_name": "PreToolUse"},
        received_at="2026-09-12T00:00:00+0000")
    assert set(rec) == FIELDS
    flat = json.dumps(rec, ensure_ascii=False)
    assert "SECRET" not in flat and "/home/user" not in flat
    assert rec["session_id"] == "s" and rec["tool_name"] == "Bash"
    assert rec["received_at"] == "2026-09-12T00:00:00+0000"


def test_build_record_non_string_scalars_null():
    mod = load_module()
    rec = mod.build_record("Stop", {"session_id": 123, "tool_name": ["x"]})
    assert rec["session_id"] is None and rec["tool_name"] is None


def test_hook_runtime_within_budget(tmp_path):
    """单次调用真实耗时远小于安装器配置的 timeoutMs=3000（≤3s 红线的实测）。"""
    import time
    spool = tmp_path / "spool.jsonl"
    started = time.monotonic()
    run_hook("SessionStart", '{"session_id":"t"}', spool)
    assert time.monotonic() - started < 3.0
