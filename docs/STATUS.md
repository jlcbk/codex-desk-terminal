# 实施状态台账（A0 维护）

真源：docs/DEVELOPMENT_PLAN.md（任务与验收）、docs/INTERFACES.md（契约）。本文件是唯一实施状态记录，由 A0 在每次任务收编时更新。状态取值：todo / doing / blocked / review / done。

约定：done 必须附证据路径与可复现命令；未验证的项不得标 done；硬件相关项在无真机实测时标"未验证"说明。

## P0：事实确认与契约冻结

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P0.1 | A0 | done | 无 | 2 次提交（基础仓库+版本清单） | `git log --oneline`；docs/VERSIONS.md 全条目带精确 tag/commit | docs/VERSIONS.md | 无 |
| P0.2 | A1（子代理） | doing | 无 | - | `codex --version`；scripts/probe/ 下各脚本 | docs/CODEX_CAPABILITIES.md（进行中） | - |
| P0.3 | A3（子代理） | done | 无 | （本次提交） | docs/HARDWARE.md §8 复核命令（A0 抽查 6/6 通过） | docs/HARDWARE.md | 10 项 unverified 已列 §7；ESP-IDF 已锁 v5.5.5 回填 VERSIONS |
| P0.4 | A0（子代理起草+A0冻结） | done | P0.2 能力已知 | （本次提交） | `uv run --with jsonschema python scripts/check_protocol.py`（15/15 PASS 退出码 0）；`cc -std=c99 -pedantic -fsyntax-only shared/state/codex_state.h` | protocol/、shared/state/、tests/fixtures/protocol/、artifacts/protocol/P0.4-draft-check-report.txt | 10 项裁决已落实（INTERFACES §1a/§3）；深度12合法侧 fixture 归 P1.5 |
| P0.5 | A4 | todo | P0.4 | - | - | - | - |

## P1：State、Mock 与模拟器基础

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P1.1 | A1 | todo | P0.4 | - | - | - | - |
| P1.2 | A1 | todo | P1.1 | - | - | - | - |
| P1.3 | A2（子代理） | doing | P0.1（done） | - | scripts/build_simulator.sh；simulator/smoke_offscreen.sh | artifacts/sim/（进行中） | 与 P0.2/P0.4 写入路径零冲突 |
| P1.4 | A0 | todo | P0.4 | - | - | - | - |
| P1.5 | A5 | todo | P0.4 | - | - | - | - |

## P2–P6

尚未开始；表格在进入对应阶段时展开。需求与验收见 docs/DEVELOPMENT_PLAN.md §5。

## 环境事实备忘（影响排期）

- 本机无 Homebrew：cmake/SDL2 获取方案已定（docs/VERSIONS.md「宿主缺口」节），在 P1.3 执行。
- codex CLI 0.152.0 在机，app-server 带协议生成命令（P0.2 使用）。
- 同板参考项目 /Users/cui/Documents/Projects/hermes-courier（2026-09-09 收官，官方 demo 已 vendor），只读引用，不复制其固件代码。
- ESP32-S3-RLCD-4.2 真机是否在手未确认：P4 起需要硬件，届时向用户确认。
