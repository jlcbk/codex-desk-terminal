"""契约 v1.2 增补（ZC8）：thread_started.branch 与 threads[].branch 端到端。

覆盖（docs/INTERFACES.md §3 threads[].branch 行 + §8 兼容矩阵）：
- events：thread_started(branch=…) to_dict/from_dict 往返；缺省（codex.py 等
  既有调用方不传）→ 不写键，协议侧 null；
- reducer：thread_started 带 branch 存储；重复 thread_started 无 branch 不
  覆盖；turn_started 不清（会话级语义，同 model/tokens）；
- render：快照恒输出 branch（缺失 → None）；超长按码点截断 ≤32 UTF-8 字节；
- schema：带 branch / 不带 branch 均过 protocol/state.schema.json；
- zcode 观察器：resolve_git_branch 三分支（成功/失败/超时，subprocess mock）、
  每会话缓存不重复执行、branch 进快照；mapper 无 cwd 不调 resolver。
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

import bridge.events as ev
from bridge.sources import zcode as zc
from bridge.state.engine import SOURCE_ZCODE_OBSERVED, StateEngine


def build(*events, epoch="epoch-zc8", anchor=1000):
    engine = StateEngine(epoch, source_kind="mock", utc_anchor_ms=anchor)
    snaps = [engine.apply(e, e.at_ms if e.at_ms is not None else 0) for e in events]
    return engine, snaps


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout


# ---- events：往返与缺省 ----

def test_thread_started_branch_roundtrip():
    m = ev.thread_started("t1", "proj", at_ms=5, branch="feat/agent-ui")
    d = m.to_dict()
    assert d["branch"] == "feat/agent-ui"
    m2 = ev.NormalizedEvent.from_dict(d)
    assert m2.type == ev.EVENT_THREAD_STARTED
    assert m2.branch == "feat/agent-ui" and m2.project == "proj" and m2.at_ms == 5


def test_thread_started_without_branch_is_backward_compatible():
    """既有调用方（codex.py/mock）不传 branch：事件无键，语义=null。"""
    d = ev.thread_started("t1", "proj", at_ms=1).to_dict()
    assert "branch" not in d
    m2 = ev.NormalizedEvent.from_dict(d)
    assert m2.branch is None


# ---- reducer：存储 / 不覆盖 / turn_started 不清 ----

def test_reducer_stores_branch_and_survives_turn_restart():
    engine = StateEngine("e")
    engine.apply(ev.thread_started("t1", "proj", at_ms=0, branch="main"), 0)
    s1 = engine.apply(ev.turn_started("t1", "turn-1", at_ms=10), 10)
    assert s1["threads"][0]["branch"] == "main"
    # 跨 turn 不清零（会话级字段）。
    engine.apply(ev.turn_completed("t1", "turn-1", ev.TURN_STATUS_COMPLETED, at_ms=90), 90)
    s2 = engine.apply(ev.turn_started("t1", "turn-2", at_ms=100), 100)
    assert s2["threads"][0]["branch"] == "main"


def test_reducer_keeps_branch_when_later_started_lacks_it():
    engine, snaps = build(
        ev.thread_started("t1", "proj", at_ms=0, branch="dev"),
        ev.thread_started("t1", "proj2", at_ms=10),
    )
    rec = snaps[-1]["threads"][0]
    assert rec["branch"] == "dev"
    assert rec["project"] == "proj2"  # project 照旧更新（既有口径）


# ---- render：恒输出 / 缺失 null / 截断 ----

def test_render_always_emits_branch():
    engine, snaps = build(ev.thread_started("t1", "p", at_ms=0))
    rec = snaps[-1]["threads"][0]
    assert "branch" in rec and rec["branch"] is None


def test_render_clamps_long_branch_to_32_bytes():
    engine, snaps = build(
        ev.thread_started("t1", "p", at_ms=0, branch="b" * 100),
    )
    assert snaps[-1]["threads"][0]["branch"] == "b" * 32


def test_render_clamps_branch_on_utf8_codepoint_boundary():
    # 16 个三字节 CJK = 48 字节 → 按 32 字节码点安全截断为 10 个字（30 字节）。
    engine, snaps = build(
        ev.thread_started("t1", "p", at_ms=0, branch="分" * 16),
    )
    assert snaps[-1]["threads"][0]["branch"] == "分" * 10


# ---- schema ----

def test_snapshot_with_branch_validates(validator):
    engine, snaps = build(ev.thread_started("t1", "p", at_ms=0, branch="release/1.2"))
    assert list(validator.iter_errors(snaps[-1])) == []
    assert snaps[-1]["threads"][0]["branch"] == "release/1.2"


def test_snapshot_without_branch_still_validates(validator):
    engine, snaps = build(ev.thread_started("t1", "p", at_ms=0))
    assert list(validator.iter_errors(snaps[-1])) == []


# ---- zcode：resolve_git_branch（subprocess mock 三分支）----

class _FakeCompleted:
    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout


def test_resolve_git_branch_success(monkeypatch, tmp_path):
    calls = []
    cwd = str(tmp_path)  # 必须真实存在（resolve 先做 isdir 防御）

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return _FakeCompleted(0, b"feat/agent-ui\n")

    monkeypatch.setattr(zc.subprocess, "run", fake_run)
    assert zc.resolve_git_branch(cwd) == "feat/agent-ui"
    argv, kw = calls[0]
    assert argv == ["git", "-C", cwd, "branch", "--show-current"]
    assert kw["timeout"] == zc.GIT_BRANCH_TIMEOUT_S == 3.0
    assert kw["check"] is False  # 只读查询，绝不抛非零退出


def test_resolve_git_branch_failure_and_empty(monkeypatch, tmp_path):
    cwd = str(tmp_path)
    monkeypatch.setattr(zc.subprocess, "run",
                        lambda argv, **kw: _FakeCompleted(128, b""))
    assert zc.resolve_git_branch(cwd) is None                # 非零退出 → None
    monkeypatch.setattr(zc.subprocess, "run",
                        lambda argv, **kw: _FakeCompleted(0, b"\n"))
    assert zc.resolve_git_branch(cwd) is None                # 空输出 → None
    monkeypatch.setattr(zc.subprocess, "run",
                        lambda argv, **kw: _FakeCompleted(0, b"main"))
    assert zc.resolve_git_branch(cwd) == "main"              # 尾部空白容忍


def test_resolve_git_branch_timeout_and_missing_git(monkeypatch, tmp_path):
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw["timeout"])

    monkeypatch.setattr(zc.subprocess, "run", slow)
    assert zc.resolve_git_branch(str(tmp_path)) is None      # 超时 → None

    def no_git(argv, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(zc.subprocess, "run", no_git)
    assert zc.resolve_git_branch(str(tmp_path)) is None      # git 缺失 → None


def test_resolve_git_branch_rejects_bad_cwd(monkeypatch):
    def boom(argv, **kw):  # 坏 cwd 不应触发任何 subprocess
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(zc.subprocess, "run", boom)
    assert zc.resolve_git_branch(None) is None
    assert zc.resolve_git_branch("") is None
    assert zc.resolve_git_branch("   ") is None


# ---- zcode：mapper 每会话缓存 / agent 首见 / 无 cwd ----

def test_mapper_session_start_resolves_branch_once():
    calls = []
    mapper = zc.ZcodeEventMapper(
        branch_resolver=lambda cwd: (calls.append(cwd) or "main"))

    first = mapper.spool_event("SessionStart", "s1", None, cwd="/repo/a")
    assert calls == ["/repo/a"]                       # 首见执行一次
    assert first[0].type == ev.EVENT_THREAD_STARTED
    assert first[0].branch == "main"

    again = mapper.spool_event("SessionStart", "s1", None, cwd="/repo/a")
    assert again == []                                # thread_started 去重
    assert len(calls) == 1                            # 缓存：不重复执行


def test_mapper_session_start_without_cwd_skips_resolver_but_recovers():
    calls = []
    mapper = zc.ZcodeEventMapper(
        branch_resolver=lambda cwd: (calls.append(cwd) or "dev"))

    none_cwd = mapper.spool_event("SessionStart", "s1", None, cwd=None)
    assert none_cwd[0].branch is None                 # 无 cwd：不带 branch
    assert calls == []                                # 且未消耗解析机会

    # 旧会话后续 spool 行带来 cwd 后再次 thread_started（新会话名验证不了；
    # 同会话 started 已去重——st.cwd 兜底仅在首条 thread_started 时生效，
    # 这里验证的是"同一事件里 cwd 缺失但此前行记住过 cwd"的兜底路径）。
    mapper2 = zc.ZcodeEventMapper(
        branch_resolver=lambda cwd: (calls.append(cwd) or "dev"))
    mapper2.spool_event("PreToolUse", "s2", "Bash", cwd="/repo/b")
    late = mapper2.spool_event("SessionStart", "s2", None, cwd=None)
    assert late[0].branch == "dev"                    # 回退 st.cwd 解析
    assert calls == ["/repo/b"]


def test_mapper_agent_first_seen_resolves_branch():
    calls = []
    mapper = zc.ZcodeEventMapper(
        branch_resolver=lambda cwd: (calls.append(cwd) or "feat/x"))

    events = mapper.agent_update("child-1", {"cwd": "/repo/c"}, at_ms=1)
    started = [e for e in events if e.type == ev.EVENT_THREAD_STARTED]
    assert len(started) == 1 and started[0].branch == "feat/x"
    assert calls == ["/repo/c"]

    # 再次 metadata 更新不再发 thread_started、不再解析。
    events2 = mapper.agent_update("child-1", {"cwd": "/repo/c"}, at_ms=2)
    assert all(e.type != ev.EVENT_THREAD_STARTED for e in events2)
    assert len(calls) == 1


def test_mapper_resolver_none_result_is_honest_null():
    mapper = zc.ZcodeEventMapper(branch_resolver=lambda cwd: None)
    events = mapper.spool_event("SessionStart", "s1", None, cwd="/repo")
    assert events[0].branch is None


def test_mapper_without_resolver_stays_pure():
    """单测既有用法（无 resolver）：纯内存，恒无 branch（零回归基线）。"""
    mapper = zc.ZcodeEventMapper()
    events = mapper.spool_event("SessionStart", "s1", None, cwd="/repo")
    assert events[0].branch is None


# ---- zcode：observer 接线（默认 resolver = resolve_git_branch）----

def test_observer_defaults_to_real_resolver():
    engine = StateEngine("e", source_kind=SOURCE_ZCODE_OBSERVED)
    obs = zc.ZcodeObserver(engine)
    assert obs.mapper.branch_resolver is zc.resolve_git_branch
    obs2 = zc.ZcodeObserver(engine, branch_resolver=None)
    assert obs2.mapper.branch_resolver is None


def test_observer_branch_flows_into_snapshot(tmp_path):
    """spool SessionStart(cwd) → thread_started(branch) → 快照 threads[].branch。"""
    from cdt_bridge_shared import assert_invariants

    root = tmp_path / "zcode"
    (root / "rollout").mkdir(parents=True)
    (root / "agents").mkdir(parents=True)
    spool = root / "spool.jsonl"
    spool.write_text("", encoding="utf-8")
    engine = StateEngine("zcode-test", source_kind=SOURCE_ZCODE_OBSERVED)
    observer = zc.ZcodeObserver(
        engine,
        rollout_dir=str(root / "rollout"),
        agents_dir=str(root / "agents"),
        spool_path=str(spool),
        branch_resolver=lambda cwd: "feature/zc8",
    )
    assert observer.poll_once(1000) == []  # 首拍武装 spool

    line = ('{"event":"SessionStart","session_id":"sess-br","tool_name":null,'
            '"received_at":"2026-09-12T10:00:00.000Z","cwd":"/tmp/repo-zc8"}')
    with open(spool, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    snaps = observer.poll_once(2000)
    assert snaps, "会话首见必须产快照"
    thread = next(t for t in snaps[-1]["threads"] if t["id"] == "sess-br")
    assert thread["branch"] == "feature/zc8"
    assert thread["project"] == "repo-zc8"
    for snap in snaps:
        assert_invariants(snap)


def test_resolve_git_branch_against_real_tmp_repo(tmp_path):
    """真实 git 冒烟（只读）：tmp git init 仓库 → 取到 init 默认分支名。

    无 git 二进制的环境跳过（mock 三分支已覆盖逻辑；本例只验证真实命令形状）。
    """
    if shutil.which("git") is None:
        pytest.skip("git binary not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True, timeout=10)
    branch = zc.resolve_git_branch(str(repo))
    assert isinstance(branch, str) and branch.strip()
