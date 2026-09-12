#!/usr/bin/env python3
"""install_zcode_hooks.py — ZCode hook 安装器（ZC2，A2）：把 cdt hook spool 写入端
（scripts/zcode_hook_spool.py）合并进用户级 ZCode 配置的 hooks 键。

三个动作（互斥）：
  --install    把 7 事件的 process 型 hook 合并进配置（幂等；写入前自动备份）；
  --uninstall  按 statusMessage 标记只移除自己的条目（用户既有条目一概不动）；
  --print      只打印将做的变更（当前 hooks 摘要 → 目标 hooks 摘要），不写盘。

设计约定：
- 合并/卸载是纯函数（输入现有 config dict → 输出新 dict，不改入参）；
  幂等标记 = statusMessage == "cdt-zcode-hook"：重复运行先摘掉旧条目再插入，
  绝不产生重复条目；用户的 mcp/plugins 等其他键一概不碰。
- 目标配置默认 ~/.zcode/cli/config.json；--config 可指向 tmpdir 假配置
  （本任务的红线：绝不真实运行 --install/--uninstall 作用于 ~/.zcode，
  用户配置由 A0 在部署阶段处置；单测全部针对 tmpdir）。
- install 时 hooks.enabled 置 true（ZCode 要求顶层
  "hooks": {"enabled": true, "events": {...}} 才生效）。原 enabled 值记入
  sidecar（<config>.cdt-install-state.json），uninstall 在「events 七键全空
  且无其他来源」时恢复原值；无 sidecar 时尽力而为（只摘自己的条目）。
- 备份：每次真实写盘前生成 config.json.bak-cdt-<时间戳>。
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 冻结的 7 个 hook 事件（与 scripts/zcode_hook_spool.py 的 HOOK_EVENTS 对齐）。
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
)

#: 幂等标记：识别「自己的」hook 条目（安装/卸载都只认这个标记）。
MARKER = "cdt-zcode-hook"

TIMEOUT_MS = 3000

#: 默认目标：用户级 ZCode 配置（生产路径；测试用 --config 覆盖到 tmpdir）。
DEFAULT_CONFIG = Path("~/.zcode/cli/config.json")

#: install 原始 enabled 记录（sidecar；uninstall 时据此恢复）。
STATE_SUFFIX = ".cdt-install-state.json"


# ---------------------------------------------------------------------------
# 纯函数层（可离线单测；不触盘、不改入参）
# ---------------------------------------------------------------------------

def hook_entry(event: str, script_path: str, python_bin: str) -> dict:
    """单事件 process 型 hook 条目（matcher 省略 = 全匹配；shape 同
    .zcode/config.json 的 dump 探针，command/args 用绝对路径）。"""
    return {
        "type": "process",
        "command": python_bin,
        "args": [str(script_path), event],
        "timeoutMs": TIMEOUT_MS,
        "statusMessage": MARKER,
    }


def build_hooks_block(script_path: str, python_bin: str,
                      events=HOOK_EVENTS) -> dict:
    """将插入的完整 hooks 块：{"enabled": true, "events": {事件: [全匹配项]}}。"""
    return {
        "enabled": True,
        "events": {
            name: [{"hooks": [hook_entry(name, script_path, python_bin)]}]
            for name in events
        },
    }


def _entry_has_marker(entry) -> bool:
    """一个事件条目（{"hooks":[...]}）里是否含本工具的标记条目。"""
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks")
    if not isinstance(inner, list):
        return False
    return any(
        isinstance(h, dict) and h.get("statusMessage") == MARKER for h in inner
    )


def _find_original_enabled(config: dict):
    """原 enabled 值；键缺失返回 None（与 False 区分，uninstall 时据此删除键）。"""
    hooks = config.get("hooks")
    if isinstance(hooks, dict) and "enabled" in hooks:
        value = hooks.get("enabled")
        return bool(value) if isinstance(value, bool) else value
    return None


def merge_hooks(config: dict, planned: dict) -> dict:
    """纯函数：现有 config dict + 计划插入的 hooks dict → 新 config dict。

    - 幂等：先摘掉带 statusMessage 标记的旧条目，再插入计划条目——重复运行
      不产生重复条目（同事件同标记只保留最新一份）。
    - 保留用户既有键（mcp/plugins 等）与用户自己的 hook 条目。
    - enabled 置为 planned 的值（True；ZCode 顶层要求）。
    """
    new = copy.deepcopy(config)
    if not isinstance(new, dict):
        raise ValueError("config must be a JSON object")
    hooks = dict(new.get("hooks") or {})
    events = dict(hooks.get("events") or {})
    # 1) 摘掉自己的旧条目（幂等替换的基础）。
    for name, entries in list(events.items()):
        if isinstance(entries, list):
            kept = [e for e in entries if not _entry_has_marker(e)]
            if len(kept) != len(entries):
                events[name] = kept
    # 2) 插入计划条目（深拷贝隔离，避免调用方共享可变引用）。
    for name, entries in (planned.get("events") or {}).items():
        lst = list(events.get(name) or [])
        lst.extend(copy.deepcopy(entries))
        events[name] = lst
    hooks["events"] = events
    hooks["enabled"] = bool(planned.get("enabled", True))
    new["hooks"] = hooks
    return new


def unmerge_hooks(config: dict) -> dict:
    """纯函数：按 statusMessage 标记移除自己的条目。

    若移除后 events 全空（七键无残留、也没有其他事件键有条目）且 hooks 块
    没有额外键 → 整个 hooks 块因我们而存在 → 移除整个 hooks 键（恢复
    「无 hooks 配置」的原状）；用户仍有自己的条目/其他键时只摘我们的。
    enabled 的原值恢复由调用方按 sidecar 处置（本函数保持纯函数职责）。
    """
    new = copy.deepcopy(config)
    hooks = new.get("hooks")
    if not isinstance(hooks, dict):
        return new
    events = hooks.get("events")
    if isinstance(events, dict):
        cleaned = {}
        for name, entries in events.items():
            if isinstance(entries, list):
                cleaned[name] = [e for e in entries if not _entry_has_marker(e)]
            else:
                cleaned[name] = entries  # 非标准形状：不动（不是我们写的）
        hooks["events"] = cleaned
        has_other_sources = any(
            isinstance(v, list) and v for v in cleaned.values()
        )
        has_extra_keys = bool(set(hooks.keys()) - {"enabled", "events"})
        if not has_other_sources and not has_extra_keys:
            del new["hooks"]
    return new


def restore_enabled(config: dict, original_enabled) -> dict:
    """纯函数：把 enabled 恢复为安装前的值（None = 键应不存在）。"""
    new = copy.deepcopy(config)
    hooks = new.get("hooks")
    if not isinstance(hooks, dict):
        return new
    if original_enabled is None:
        hooks.pop("enabled", None)
    else:
        hooks["enabled"] = original_enabled
    return new


def summarize_hooks(config: dict) -> str:
    """hooks 摘要（--print 用）：每事件条目数，标注 cdt/他人。不含命令行内容。"""
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return "(无 hooks 键)"
    events = hooks.get("events")
    if not isinstance(events, dict) or not events:
        return "(hooks.events 为空)"
    parts = []
    for name in sorted(events):
        entries = events.get(name)
        n_total = len(entries) if isinstance(entries, list) else -1
        n_cdt = sum(1 for e in (entries or []) if _entry_has_marker(e))
        parts.append("%s:%d(cdt %d)" % (name, n_total, n_cdt))
    enabled = hooks.get("enabled")
    return "enabled=%s events={%s}" % (enabled, ", ".join(parts))


# ---------------------------------------------------------------------------
# 落盘层（install/uninstall/print 的 CLI 编排）
# ---------------------------------------------------------------------------

def _load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SystemExit("install_zcode_hooks: 配置读取失败 %s: %s" % (path, exc))
    if not isinstance(raw, dict):
        raise SystemExit("install_zcode_hooks: 配置顶层必须是 JSON object: %s" % path)
    return raw


def _backup(path: Path) -> Path | None:
    """写盘前备份 config.json.bak-cdt-<时间戳>；源文件不存在则跳过。"""
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S")
    dest = path.with_name("%s.bak-cdt-%s" % (path.name, stamp))
    shutil.copy2(path, dest)
    return dest


def _write_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_state(path: Path) -> dict | None:
    state_path = path.with_name(path.name + STATE_SUFFIX)
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _write_state(path: Path, original_enabled) -> None:
    state_path = path.with_name(path.name + STATE_SUFFIX)
    state_path.write_text(
        json.dumps({"original_enabled": original_enabled,
                    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _remove_state(path: Path) -> None:
    path.with_name(path.name + STATE_SUFFIX).unlink(missing_ok=True)


def _resolve_python(explicit: str | None) -> str:
    """command 用 python3 绝对路径；解析失败回退当前解释器。"""
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    found = shutil.which("python3") or sys.executable
    return str(Path(found).resolve())


def _resolve_script(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (REPO_ROOT / "scripts" / "zcode_hook_spool.py").resolve()


def run_install(path: Path, *, script: Path, python_bin: str) -> int:
    config = _load_config(path)
    if not script.is_file():
        print("install_zcode_hooks: spool 脚本不存在: %s" % script,
              file=sys.stderr)
        return 2
    planned = build_hooks_block(str(script), python_bin)
    new_config = merge_hooks(config, planned)
    backup = _backup(path)
    _write_config(path, new_config)
    if _load_state(path) is None:  # 幂等：首装才记录原 enabled，重装不覆盖
        _write_state(path, _find_original_enabled(config))
    print("install: 已写入 %s（备份 %s）"
          % (path, backup if backup else "跳过：原文件不存在"))
    print("install: 7 事件 %s，spool=%s" % (", ".join(HOOK_EVENTS),
                                            script))
    return 0


def run_uninstall(path: Path) -> int:
    config = _load_config(path)
    new_config = unmerge_hooks(config)
    state = _load_state(path)
    if state is not None and "hooks" in new_config:
        new_config = restore_enabled(new_config, state.get("original_enabled"))
    if not path.is_file():
        print("uninstall: 配置不存在，无需清理: %s" % path)
        _remove_state(path)
        return 0
    if (json.dumps(config, ensure_ascii=False, sort_keys=True)
            == json.dumps(new_config, ensure_ascii=False, sort_keys=True)):
        _remove_state(path)
        print("uninstall: 无标记为 %s 的条目，配置未变更" % MARKER)
        return 0
    backup = _backup(path)
    _write_config(path, new_config)
    _remove_state(path)
    print("uninstall: 已移除标记为 %s 的条目（备份 %s）" % (MARKER, backup))
    return 0


def run_print(path: Path, *, script: Path, python_bin: str) -> int:
    """--print：当前 hooks 摘要 → install/uninstall 各自的目标摘要，不写盘。"""
    config = _load_config(path)
    planned_ok = script.is_file()
    print("--print（只读，不写盘）")
    print("config: %s（%s）" % (path, "存在" if path.is_file() else "不存在"))
    print("python: %s" % python_bin)
    print("script: %s（%s）" % (script, "存在" if planned_ok else "缺失！"))
    print("当前 hooks: %s" % summarize_hooks(config))
    if planned_ok:
        target = merge_hooks(config, build_hooks_block(str(script), python_bin))
        print("install 目标 hooks: %s" % summarize_hooks(target))
    print("uninstall 目标 hooks: %s" % summarize_hooks(unmerge_hooks(config)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install_zcode_hooks",
        description="CodexDT: install/uninstall ZCode hook spool writer into "
                    "the user-level ZCode config (statusMessage marker based, "
                    "idempotent).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true",
                       help="合并 7 事件 hook 进配置（幂等，先备份）")
    group.add_argument("--uninstall", action="store_true",
                       help="按 statusMessage 标记移除自己的条目")
    group.add_argument("--print", dest="print_plan", action="store_true",
                       help="只打印将做的变更，不写盘")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="目标配置路径（默认 ~/.zcode/cli/config.json；"
                             "测试指向 tmpdir 假配置）")
    parser.add_argument("--script", default=None,
                        help="spool 写入端脚本路径（默认本仓库 "
                             "scripts/zcode_hook_spool.py 绝对路径）")
    parser.add_argument("--python", dest="python_bin", default=None,
                        help="hook command 用的解释器（默认 python3 绝对路径）")
    args = parser.parse_args(argv)

    path = Path(args.config).expanduser()
    script = _resolve_script(args.script)
    python_bin = _resolve_python(args.python_bin)

    if args.print_plan:
        return run_print(path, script=script, python_bin=python_bin)
    if args.install:
        return run_install(path, script=script, python_bin=python_bin)
    return run_uninstall(path)


if __name__ == "__main__":
    raise SystemExit(main())
