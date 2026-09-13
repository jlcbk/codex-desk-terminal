"""ZC2 离线单测：scripts/zcode_hook_spool.py（hook 写入端，生产脚本）。

覆盖（任务书第 1 节单测清单 + ZC3 扩展）：
- 正常追加：冻结七字段行（基础四字段 + ZC3 可选 cwd/summary/plan）、逐次调用
  追加不覆盖；
- 坏 stdin（非 JSON）容错：照样落行（session_id/tool_name=null）、exit 0、无 stdout；
- ZC3 放行：cwd 原样提取；TodoWrite 的 todos → plan（含 >8 截断、status 归一化、
  content 空值剔除、200 字符码点安全截断）；PermissionRequest 的审批内容摘要 →
  summary（command/path/url/query/file_path 首行、strip、≤400 截断）；非
  TodoWrite 工具 plan 恒 null，非 PermissionRequest 事件 summary 恒 null，
  其 tool_input 绝不落盘；
- CDT_HOOK_SPOOL 覆盖默认路径；spool 文件权限 0600（现含 cwd/任务文本收紧）；
- 并发追加：两次快速调用不互删（flock 覆盖查大小→轮转→写全程）；
- 轮转：spool >1MB 时写入端 truncate（读取端按 offset>size 重读语义容错）；
- 防御：未注册事件名不落盘仍 exit 0；缺目录自动创建。

策略：行为测试走真实子进程（exit code / stdout / 文件内容全链路）；
纯函数（build_record / extract_plan / resolve_spool_path）直接 importlib 加载断言。
红线自检：记录里绝不出现 prompt 内容 / 其他工具的 tool_input / 环境变量。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cdt_bridge_shared import REPO_ROOT

SCRIPT_PATH = REPO_ROOT / "scripts" / "zcode_hook_spool.py"

FIELDS = {"event", "session_id", "tool_name", "received_at", "cwd", "summary",
          "plan"}


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
    # 红线：prompt / 非放行工具的 tool_input 绝不落盘；ZC3 放行的 cwd 原样保留。
    assert "SECRET" not in flat
    assert rec["cwd"] == "/home/user"
    assert rec["plan"] is None  # Bash 不是 TodoWrite
    assert rec["session_id"] == "s" and rec["tool_name"] == "Bash"
    assert rec["received_at"] == "2026-09-12T00:00:00+0000"


def test_build_record_non_string_scalars_null():
    mod = load_module()
    rec = mod.build_record("Stop", {"session_id": 123, "tool_name": ["x"]})
    assert rec["session_id"] is None and rec["tool_name"] is None
    assert rec["cwd"] is None and rec["plan"] is None and rec["summary"] is None


def test_hook_runtime_within_budget(tmp_path):
    """单次调用真实耗时远小于安装器配置的 timeoutMs=3000（≤3s 红线的实测）。"""
    import time
    spool = tmp_path / "spool.jsonl"
    started = time.monotonic()
    run_hook("SessionStart", '{"session_id":"t"}', spool)
    assert time.monotonic() - started < 3.0


# ---------------------------------------------------------------------------
# ZC3：cwd 提取（放行裁决 2026-09-12）
# ---------------------------------------------------------------------------

def test_cwd_extracted_verbatim(tmp_path):
    """stdin 的 cwd 原样进入记录（写端不加工；basename/脱敏归观察器）。"""
    spool = tmp_path / "spool.jsonl"
    stdin = json.dumps({"session_id": "s1", "cwd": "/Users/me/dev/my proj"})
    assert run_hook("SessionStart", stdin, spool).returncode == 0
    rec = read_lines(spool)[0]
    assert rec["cwd"] == "/Users/me/dev/my proj"
    assert rec["plan"] is None


@pytest.mark.parametrize("bad_cwd", [None, 123, ["", "/x"], {"cwd": 1}])
def test_cwd_bad_types_become_null(bad_cwd):
    """cwd 坏类型/缺失 → null（写端只做类型门槛，不加工内容）。"""
    mod = load_module()
    rec = mod.build_record("Stop", {"session_id": "s", "cwd": bad_cwd})
    assert rec["cwd"] is None


def test_cwd_empty_string_kept_verbatim():
    """原样语义：空串也是字符串，原样保留（读取端按缺失处理）。"""
    mod = load_module()
    rec = mod.build_record("Stop", {"session_id": "s", "cwd": ""})
    assert rec["cwd"] == ""


# ---------------------------------------------------------------------------
# ZC3：TodoWrite plan 提取（唯一放行 tool_input 的工具）
# ---------------------------------------------------------------------------

def todowrite_payload(todos, **extra):
    """TodoWrite 的 hook stdin payload（dict；子进程用 json.dumps 序列化）。"""
    return dict({"session_id": "s1", "tool_name": "TodoWrite",
                 "tool_input": {"todos": todos}}, **extra)


def todowrite_stdin(todos, **extra):
    return json.dumps(todowrite_payload(todos, **extra))


def test_todowrite_plan_extracted_end_to_end(tmp_path):
    """PreToolUse+TodoWrite → plan 落盘；shape=total/steps/truncated。"""
    spool = tmp_path / "spool.jsonl"
    todos = [
        {"content": "read AGENTS.md", "status": "completed",
         "activeForm": "reading"},
        {"content": "  implement ZC3  ", "status": "in_progress"},
        {"content": "run tests", "status": "pending"},
    ]
    proc = run_hook("PreToolUse", todowrite_stdin(todos), spool)
    assert proc.returncode == 0 and proc.stdout == ""
    plan = read_lines(spool)[0]["plan"]
    assert plan == {
        "total": 3,
        "steps": [
            {"text": "read AGENTS.md", "status": "completed"},
            {"text": "implement ZC3", "status": "in_progress"},
            {"text": "run tests", "status": "pending"},
        ],
        "truncated": False,
    }


@pytest.mark.parametrize("status,expected", [
    ("in_progress", "in_progress"),   # 原样
    ("completed", "completed"),       # 原样
    ("pending", "pending"),           # 原样语义
    ("unknown", "pending"),           # 未知归一化
    ("", "pending"),
    (None, "pending"),
    (7, "pending"),                   # 坏类型
])
def test_todowrite_status_normalized(status, expected):
    mod = load_module()
    plan = mod.extract_plan("PreToolUse", todowrite_payload(
        [{"content": "x", "status": status}]))
    assert plan["steps"] == [{"text": "x", "status": expected}]


def test_todowrite_empty_content_dropped_total_preserved():
    """content 空值剔除（strip 后空/非字符串）；total 恒=原始 todos 条数。"""
    mod = load_module()
    todos = [
        {"content": "  ", "status": "pending"},
        {"content": "real step", "status": "pending"},
        {"status": "pending"},                      # 无 content
        {"content": 42, "status": "pending"},       # 坏类型
    ]
    plan = mod.extract_plan("PreToolUse", todowrite_payload(todos))
    assert plan["total"] == 4
    assert plan["steps"] == [{"text": "real step", "status": "pending"}]
    assert plan["truncated"] is False


def test_todowrite_steps_truncated_over_8():
    """>8 条：保留前 8 条、truncated=true、total=原始总数（12）。"""
    mod = load_module()
    todos = [{"content": "step %d" % i, "status": "pending"} for i in range(12)]
    plan = mod.extract_plan("PreToolUse", todowrite_payload(todos))
    assert len(plan["steps"]) == 8
    assert [s["text"] for s in plan["steps"]] == ["step %d" % i for i in range(8)]
    assert plan["total"] == 12 and plan["truncated"] is True


def test_todowrite_text_truncated_at_200_chars_codepoint_safe():
    """单条 text 码点安全截断 ≤200 字符（CJK 不劈开多字节字符）。"""
    mod = load_module()
    assert mod.PLAN_MAX_TEXT_CHARS == 200
    long_cjk = "汉" * 260
    plan = mod.extract_plan("PreToolUse", todowrite_payload(
        [{"content": long_cjk, "status": "pending"}]))
    text = plan["steps"][0]["text"]
    assert text == "汉" * 200
    assert len(text) == 200


def test_todowrite_requires_todowrite_and_pretooluse():
    """放行条件三者缺一不可：其他工具/其他事件/缺失 todos → plan=None，
    且其 tool_input 绝不进入返回值。"""
    mod = load_module()
    todos = [{"content": "SECRET-TODO", "status": "pending"}]
    # 其他工具（即使 input 里也有 todos）
    rec = mod.build_record("PreToolUse", {
        "session_id": "s", "tool_name": "Bash",
        "tool_input": {"todos": todos, "command": "SECRET-CMD"}})
    assert rec["plan"] is None
    assert "SECRET-TODO" not in json.dumps(rec)
    assert "SECRET-CMD" not in json.dumps(rec)
    # TodoWrite 但事件不对
    assert mod.extract_plan("PostToolUse", todowrite_stdin(todos)) is None
    # TodoWrite+PreToolUse 但 todos 缺失/空/坏类型
    assert mod.extract_plan("PreToolUse", {
        "tool_name": "TodoWrite", "tool_input": {}}) is None
    assert mod.extract_plan("PreToolUse", {
        "tool_name": "TodoWrite", "tool_input": {"todos": []}}) is None
    assert mod.extract_plan("PreToolUse", {
        "tool_name": "TodoWrite", "tool_input": {"todos": "nope"}}) is None
    assert mod.extract_plan("PreToolUse", {"tool_name": "TodoWrite"}) is None


def test_todowrite_tool_input_dual_naming_accepted():
    """实测 stdin 双命名：toolInput（驼峰）也兼容。"""
    mod = load_module()
    todos = [{"content": "via camel", "status": "pending"}]
    plan = mod.extract_plan("PreToolUse", {
        "tool_name": "TodoWrite", "toolInput": {"todos": todos}})
    assert plan is not None and plan["steps"][0]["text"] == "via camel"


def test_todowrite_plan_via_subprocess_and_others_clean(tmp_path):
    """子进程全链路：TodoWrite 行带 plan；同会话其他工具行 plan=null 且
    todos 文本绝不出现。"""
    spool = tmp_path / "spool.jsonl"
    run_hook("PreToolUse", todowrite_stdin(
        [{"content": "SYNTHETIC-TODO-MARKER", "status": "pending"}]), spool)
    run_hook("PreToolUse", json.dumps({
        "session_id": "s1", "tool_name": "Read",
        "tool_input": {"file_path": "/x", "todos": [
            {"content": "SYNTHETIC-TODO-MARKER", "status": "pending"}]}}), spool)
    lines = read_lines(spool)
    assert lines[0]["plan"]["steps"][0]["text"] == "SYNTHETIC-TODO-MARKER"
    assert lines[1]["plan"] is None
    raw = spool.read_text(encoding="utf-8")
    assert raw.count("SYNTHETIC-TODO-MARKER") == 1, "非 TodoWrite 的 todos 不落盘"


# ---------------------------------------------------------------------------
# ZC3：spool 文件权限 0600
# ---------------------------------------------------------------------------

def test_spool_file_created_with_0600(tmp_path):
    spool = tmp_path / "perm" / "spool.jsonl"
    assert run_hook("Stop", '{"session_id":"p"}', spool).returncode == 0
    assert spool.stat().st_mode & 0o777 == 0o600


def test_spool_existing_loose_permissions_tightened(tmp_path):
    """历史遗留的宽松权限文件在下次写入时被收紧为 0600。"""
    spool = tmp_path / "spool.jsonl"
    spool.write_text("", encoding="utf-8")
    os.chmod(spool, 0o644)
    assert run_hook("Stop", '{"session_id":"p"}', spool).returncode == 0
    assert spool.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# ZC3：向后兼容
# ---------------------------------------------------------------------------

def test_old_four_field_record_shape_still_buildable():
    """旧格式（stdin 无 cwd/无 TodoWrite/无审批）产出 null 可选字段——读取端把
    缺失/null 一视同仁，行仍合法。"""
    mod = load_module()
    rec = mod.build_record("UserPromptSubmit", {"session_id": "legacy"},
                           received_at="2026-01-01T00:00:00+0000")
    assert set(rec) == FIELDS
    assert rec["cwd"] is None and rec["plan"] is None and rec["summary"] is None


# ---------------------------------------------------------------------------
# ZC3：PermissionRequest 审批内容摘要（唯一放行 summary 的事件）
# ---------------------------------------------------------------------------

def permreq_payload(tool_input, **extra):
    """PermissionRequest 的 hook stdin payload（dict）。"""
    return dict({"session_id": "s1", "tool_name": "Bash",
                 "tool_input": tool_input}, **extra)


def test_summary_extracted_from_command_end_to_end(tmp_path):
    """PermissionRequest+Bash → summary 落盘（首行、strip）；NEEDS YOU 页
    真实审批内容的写端半程。"""
    spool = tmp_path / "spool.jsonl"
    stdin = json.dumps(permreq_payload({"command": "git push origin main\n"}))
    proc = run_hook("PermissionRequest", stdin, spool)
    assert proc.returncode == 0 and proc.stdout == ""
    rec = read_lines(spool)[0]
    assert rec["summary"] == "git push origin main"
    assert rec["cwd"] is None and rec["plan"] is None


def test_summary_first_line_only_and_stripped():
    """多行命令只取首行（不落整段脚本）；首尾空白剔除。"""
    mod = load_module()
    summary = mod.extract_summary("PermissionRequest", permreq_payload(
        {"command": "  echo first  \nSECRET-LINE-TWO\nline three"}))
    assert summary == "echo first"
    assert "SECRET-LINE-TWO" not in summary


@pytest.mark.parametrize("tool_input,expected", [
    ({"command": "a", "path": "b"}, "a"),          # 依次尝试：command 优先
    ({"path": "/x/y.txt"}, "/x/y.txt"),            # path 回退
    ({"url": "https://e/x"}, "https://e/x"),       # url 回退
    ({"query": "select 1"}, "select 1"),           # query 回退
    ({"file_path": "/a/b.md"}, "/a/b.md"),         # file_path 回退
])
def test_summary_key_precedence(tool_input, expected):
    mod = load_module()
    assert mod.extract_summary("PermissionRequest",
                               permreq_payload(tool_input)) == expected


def test_summary_truncated_at_400_chars_codepoint_safe():
    mod = load_module()
    assert mod.SUMMARY_MAX_CHARS == 400
    long_cjk = "汉" * 500
    summary = mod.extract_summary("PermissionRequest", permreq_payload(
        {"command": long_cjk}))
    assert summary == "汉" * 400
    assert len(summary) == 400  # 码点安全（不劈开多字节字符）


@pytest.mark.parametrize("tool_input", [
    {},                                            # 无任何放行键
    {"command": ""},                               # 空串
    {"command": "   "},                            # strip 后空
    {"command": "\n\nrest"},                       # 首行空白
    {"command": 42},                               # 坏类型
    {"command": ["git", "push"]},
    {"other": "SECRET-VALUE"},                     # 非放行键绝不提取
])
def test_summary_missing_or_bad_yields_none(tool_input):
    mod = load_module()
    assert mod.extract_summary("PermissionRequest",
                               permreq_payload(tool_input)) is None


def test_summary_only_for_permission_request():
    """放行条件：仅 PermissionRequest 事件；其他事件（即使 tool_input 里有
    command）summary 恒 None 且 command 绝不落盘。"""
    mod = load_module()
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse",
                  "PostToolUse", "PostToolUseFailure", "Stop"):
        assert mod.extract_summary(event, permreq_payload(
            {"command": "SECRET-CMD"})) is None
    rec = mod.build_record("PreToolUse", {
        "session_id": "s1", "tool_name": "TodoWrite",
        "tool_input": {"command": "SECRET-CMD", "todos": [
            {"content": "t", "status": "pending"}]}})
    assert rec["summary"] is None
    assert "SECRET-CMD" not in json.dumps(rec)
    assert rec["plan"]["steps"][0]["text"] == "t"  # 同行 TodoWrite 放行不受影响


def test_summary_camel_case_tool_input_accepted():
    """实测 stdin 双命名：toolInput（驼峰）也兼容。"""
    mod = load_module()
    summary = mod.extract_summary("PermissionRequest", {
        "tool_name": "Bash", "toolInput": {"command": "camel cmd"}})
    assert summary == "camel cmd"


def test_summary_via_subprocess_and_others_clean(tmp_path):
    """子进程全链路：PermissionRequest 行带 summary；同会话其他事件行
    summary=null 且命令文本绝不出现。"""
    spool = tmp_path / "spool.jsonl"
    run_hook("PermissionRequest", json.dumps(permreq_payload(
        {"command": "SYNTHETIC-CMD-MARKER"})), spool)
    run_hook("PreToolUse", json.dumps(permreq_payload(
        {"command": "SYNTHETIC-CMD-MARKER"})), spool)
    lines = read_lines(spool)
    assert lines[0]["summary"] == "SYNTHETIC-CMD-MARKER"
    assert lines[1]["summary"] is None
    raw = spool.read_text(encoding="utf-8")
    assert raw.count("SYNTHETIC-CMD-MARKER") == 1, "非审批事件的命令不落盘"
