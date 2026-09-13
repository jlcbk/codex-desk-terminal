"""ZC2 离线单测：scripts/install_zcode_hooks.py（安装器；全部针对 tmpdir 假配置）。

红线：本测试绝不触碰真实 ~/.zcode——所有读写都在 tmp_path。

覆盖（任务书第 2 节）：
- merge_hooks 纯函数：幂等（重复合并不产生重复条目）、保留用户既有键
  （mcp/plugins 等）与用户自己的 hook 条目、入参不被修改；
- hook 条目 shape：process 型、python3 绝对路径、spool 脚本绝对路径+事件名、
  timeoutMs=3000、statusMessage=cdt-zcode-hook、matcher 省略（全匹配）；
- uninstall 纯函数：按 statusMessage 只摘自己的条目；events 全空且无其他
  来源 → 摘除整个 hooks 键（恢复原状）；enabled 原值经 sidecar 恢复；
- --install 真实落盘（tmpdir）：写入 + 备份 config.json.bak-cdt-* +
  sidecar；重复运行幂等（配置逐字节不变）；
- --uninstall 真实落盘：用户条目保留、原 enabled 恢复、sidecar 清除；
- --print：退出 0、不写盘（内容与 mtime 不变）。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cdt_bridge_shared import REPO_ROOT

SCRIPT_PATH = REPO_ROOT / "scripts" / "install_zcode_hooks.py"
SPOOL_SCRIPT = REPO_ROOT / "scripts" / "zcode_hook_spool.py"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
          "PostToolUse", "PostToolUseFailure", "Stop")
MARKER = "cdt-zcode-hook"


def load_module():
    spec = importlib.util.spec_from_file_location("install_zcode_hooks_mod",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def inst():
    return load_module()


PYTHON_BIN = sys.executable


def run_cli(args: list[str], cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT_PATH)] + args,
                          capture_output=True, text=True, timeout=30, cwd=cwd)


def user_entry(tag: str) -> dict:
    """用户自己的 hook 条目（无我们的标记）。"""
    return {"hooks": [{"type": "process", "command": "echo",
                       "args": ["user-%s" % tag], "statusMessage": "user-own"}]}


def user_config() -> dict:
    """带 OpenViking 风格 mcp/plugins 与自有 hook 的假用户配置。"""
    return {
        "mcp": {"openviking": {"command": "uvx", "args": ["openviking-mcp"]}},
        "plugins": {"enabled": ["browser-use"]},
        "model": "some-model",
        "hooks": {
            "enabled": True,
            "events": {"PreToolUse": [user_entry("pre")]},
        },
    }


# ---------------------------------------------------------------------------
# 纯函数：merge_hooks（幂等 / 保留用户键 / 纯度）
# ---------------------------------------------------------------------------

def test_merge_produces_seven_event_block(inst):
    planned = inst.build_hooks_block(str(SPOOL_SCRIPT), PYTHON_BIN)
    merged = inst.merge_hooks({}, planned)
    hooks = merged["hooks"]
    assert hooks["enabled"] is True
    assert set(hooks["events"]) == set(EVENTS)
    entry = hooks["events"]["PreToolUse"][0]
    assert "matcher" not in entry, "matcher 省略 = 全匹配"
    hook = entry["hooks"][0]
    assert hook["type"] == "process"
    assert Path(hook["command"]).is_absolute()
    assert hook["args"] == [str(SPOOL_SCRIPT), "PreToolUse"]
    assert Path(hook["args"][0]).is_absolute()
    assert hook["timeoutMs"] == 3000
    assert hook["statusMessage"] == MARKER


def test_merge_preserves_user_keys_and_entries(inst):
    config = user_config()
    snapshot = copy.deepcopy(config)
    planned = inst.build_hooks_block(str(SPOOL_SCRIPT), PYTHON_BIN)
    merged = inst.merge_hooks(config, planned)
    # 用户既有键一概不动
    assert merged["mcp"] == config["mcp"]
    assert merged["plugins"] == config["plugins"]
    assert merged["model"] == config["model"]
    # 用户自有条目保留（同事件共存）
    entries = merged["hooks"]["events"]["PreToolUse"]
    assert user_entry("pre") in entries
    assert len(entries) == 2
    # 其余 6 事件只有我们的条目
    for event in EVENTS:
        if event == "PreToolUse":
            continue
        assert len(merged["hooks"]["events"][event]) == 1
    # 纯度：入参不被修改
    assert config == snapshot


def test_merge_is_idempotent(inst):
    planned = inst.build_hooks_block(str(SPOOL_SCRIPT), PYTHON_BIN)
    once = inst.merge_hooks(user_config(), planned)
    twice = inst.merge_hooks(once, planned)
    thrice = inst.merge_hooks(twice, planned)
    assert twice == once, "重复合并不产生重复条目（statusMessage 标记替换）"
    assert thrice == twice
    for event in EVENTS:
        cdt = [e for e in thrice["hooks"]["events"][event]
               if inst._entry_has_marker(e)]
        assert len(cdt) == 1, event


def test_merge_replaces_stale_marker_entries(inst):
    """旧条目（如路径变化的旧版本）被替换，不叠加。"""
    config = user_config()
    old = inst.merge_hooks(config, inst.build_hooks_block("/old/path.py", "py"))
    new = inst.merge_hooks(old, inst.build_hooks_block(str(SPOOL_SCRIPT),
                                                       PYTHON_BIN))
    for event in EVENTS:
        entries = new["hooks"]["events"][event]
        marked = [e for e in entries if inst._entry_has_marker(e)]
        assert len(marked) == 1
        assert marked[0]["hooks"][0]["args"][0] == str(SPOOL_SCRIPT)
    # 用户条目仍在
    assert user_entry("pre") in new["hooks"]["events"]["PreToolUse"]


# ---------------------------------------------------------------------------
# 纯函数：unmerge_hooks / restore_enabled
# ---------------------------------------------------------------------------

def test_unmerge_removes_only_marker_entries(inst):
    merged = inst.merge_hooks(user_config(),
                              inst.build_hooks_block(str(SPOOL_SCRIPT),
                                                     PYTHON_BIN))
    unmerged = inst.unmerge_hooks(merged)
    assert user_entry("pre") in unmerged["hooks"]["events"]["PreToolUse"]
    for event in EVENTS:
        for e in unmerged["hooks"]["events"][event]:
            assert not inst._entry_has_marker(e), event
    assert unmerged["mcp"] == merged["mcp"]


def test_unmerge_restores_pristine_config(inst):
    """原配置无 hooks 键：install→uninstall 摘除整个 hooks 键（恢复原状）。"""
    pristine = {"mcp": {"x": 1}, "model": "m"}
    merged = inst.merge_hooks(pristine,
                              inst.build_hooks_block(str(SPOOL_SCRIPT),
                                                     PYTHON_BIN))
    assert "hooks" in merged
    unmerged = inst.unmerge_hooks(merged)
    assert "hooks" not in unmerged
    assert unmerged["mcp"] == pristine["mcp"]
    assert unmerged["model"] == pristine["model"]


def test_restore_enabled_semantics(inst):
    cfg = {"hooks": {"enabled": True, "events": {"Stop": [user_entry("s")]}}}
    assert inst.restore_enabled(cfg, False)["hooks"]["enabled"] is False
    assert "enabled" not in inst.restore_enabled(cfg, None)["hooks"]
    assert inst.restore_enabled(cfg, True)["hooks"]["enabled"] is True
    assert cfg["hooks"]["enabled"] is True  # 纯度：入参不变


# ---------------------------------------------------------------------------
# CLI：--install / --uninstall / --print（全部 tmpdir）
# ---------------------------------------------------------------------------

def test_cli_install_then_reinstall_idempotent_bytes(tmp_path):
    cfg_path = tmp_path / "config.json"
    args = ["--install", "--config", str(cfg_path),
            "--script", str(SPOOL_SCRIPT), "--python", PYTHON_BIN]
    proc = run_cli(args)
    assert proc.returncode == 0, proc.stderr
    first = cfg_path.read_text(encoding="utf-8")
    backups = list(tmp_path.glob("config.json.bak-cdt-*"))
    assert backups == [], "原文件不存在时不产生备份"
    # sidecar 记录原 enabled（键原本不存在 → null）
    state = json.loads((tmp_path / "config.json.cdt-install-state.json")
                       .read_text(encoding="utf-8"))
    assert state["original_enabled"] is None

    proc2 = run_cli(args)
    assert proc2.returncode == 0, proc2.stderr
    assert cfg_path.read_text(encoding="utf-8") == first, "重复 install 逐字节不变"
    backups2 = list(tmp_path.glob("config.json.bak-cdt-*"))
    assert len(backups2) == 1, "第二次 install 前产生一份备份"

    loaded = json.loads(first)
    assert set(loaded) == {"hooks"}
    assert len(loaded["hooks"]["events"]) == 7


def test_cli_install_backup_preserves_previous_content(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(user_config(), ensure_ascii=False),
                        encoding="utf-8")
    proc = run_cli(["--install", "--config", str(cfg_path),
                    "--script", str(SPOOL_SCRIPT), "--python", PYTHON_BIN])
    assert proc.returncode == 0, proc.stderr
    backups = list(tmp_path.glob("config.json.bak-cdt-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == user_config()


def test_cli_uninstall_restores_enabled_from_sidecar(tmp_path):
    """enabled=False + 用户自有事件：install 置 True，uninstall 恢复 False。"""
    cfg_path = tmp_path / "config.json"
    base = {
        "plugins": {"enabled": ["x"]},
        "hooks": {"enabled": False,
                  "events": {"SessionStart": [user_entry("ss")]}},
    }
    cfg_path.write_text(json.dumps(base), encoding="utf-8")
    common = ["--config", str(cfg_path), "--script", str(SPOOL_SCRIPT),
              "--python", PYTHON_BIN]
    assert run_cli(["--install"] + common).returncode == 0
    after_install = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after_install["hooks"]["enabled"] is True
    assert user_entry("ss") in after_install["hooks"]["events"]["SessionStart"]

    assert run_cli(["--uninstall"] + common).returncode == 0
    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after["hooks"]["enabled"] is False, "原 enabled 值经 sidecar 恢复"
    assert after["hooks"]["events"]["SessionStart"] == [user_entry("ss")]
    for e in after["hooks"]["events"]["SessionStart"]:
        assert not inst_marker(e)
    assert after["plugins"] == base["plugins"]
    assert not (tmp_path / "config.json.cdt-install-state.json").exists()


def inst_marker(entry) -> bool:
    inner = entry.get("hooks", []) if isinstance(entry, dict) else []
    return any(isinstance(h, dict) and h.get("statusMessage") == MARKER
               for h in inner)


def test_cli_uninstall_full_removal_when_nothing_else(tmp_path):
    """无 hooks 键起家：install→uninstall 后配置回到无 hooks、sidecar 清除。"""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"mcp":{"a":1}}', encoding="utf-8")
    common = ["--config", str(cfg_path), "--script", str(SPOOL_SCRIPT),
              "--python", PYTHON_BIN]
    assert run_cli(["--install"] + common).returncode == 0
    assert run_cli(["--uninstall"] + common).returncode == 0
    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after == {"mcp": {"a": 1}}, "恢复安装前原状"
    assert not (tmp_path / "config.json.cdt-install-state.json").exists()


def test_cli_uninstall_when_no_config_noop(tmp_path):
    missing = tmp_path / "absent.json"
    proc = run_cli(["--uninstall", "--config", str(missing)])
    assert proc.returncode == 0
    assert not missing.exists()


def test_cli_uninstall_noop_does_not_write(tmp_path):
    cfg_path = tmp_path / "config.json"
    original = json.dumps({"hooks": {"enabled": True,
                                     "events": {"Stop": [user_entry("u")]}}})
    cfg_path.write_text(original, encoding="utf-8")
    before_mtime = cfg_path.stat().st_mtime_ns
    proc = run_cli(["--uninstall", "--config", str(cfg_path)])
    assert proc.returncode == 0
    assert "未变更" in proc.stdout
    assert cfg_path.read_text(encoding="utf-8") == original
    assert cfg_path.stat().st_mtime_ns == before_mtime
    assert not list(tmp_path.glob("config.json.bak-cdt-*"))


def test_cli_print_writes_nothing(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(user_config()), encoding="utf-8")
    before = cfg_path.read_text(encoding="utf-8")
    before_mtime = cfg_path.stat().st_mtime_ns
    proc = run_cli(["--print", "--config", str(cfg_path),
                    "--script", str(SPOOL_SCRIPT), "--python", PYTHON_BIN])
    assert proc.returncode == 0, proc.stderr
    assert "当前 hooks" in proc.stdout and "install 目标 hooks" in proc.stdout
    assert "uninstall 目标 hooks" in proc.stdout
    assert cfg_path.read_text(encoding="utf-8") == before
    assert cfg_path.stat().st_mtime_ns == before_mtime
    assert not list(tmp_path.glob("*cdt*"))


def test_cli_install_missing_script_fails(tmp_path):
    proc = run_cli(["--install", "--config", str(tmp_path / "c.json"),
                    "--script", str(tmp_path / "nope.py"),
                    "--python", PYTHON_BIN])
    assert proc.returncode == 2
    assert not (tmp_path / "c.json").exists()


def test_cli_requires_action_flag(tmp_path):
    proc = run_cli(["--config", str(tmp_path / "c.json")])
    assert proc.returncode != 0


def test_default_script_points_at_repo_spool(inst):
    assert (REPO_ROOT / "scripts" / "zcode_hook_spool.py") == \
        (inst.REPO_ROOT / "scripts" / "zcode_hook_spool.py")
