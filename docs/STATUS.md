# 实施状态台账（A0 维护）

真源：docs/DEVELOPMENT_PLAN.md（任务与验收）、docs/INTERFACES.md（契约）。本文件是唯一实施状态记录，由 A0 在每次任务收编时更新。状态取值：todo / doing / blocked / review / done。

约定：done 必须附证据路径与可复现命令；未验证的项不得标 done；硬件相关项在无真机实测时标"未验证"说明。

## P0：事实确认与契约冻结

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P0.1 | A0 | done | 无 | 2 次提交（基础仓库+版本清单） | `git log --oneline`；docs/VERSIONS.md 全条目带精确 tag/commit | docs/VERSIONS.md | 无 |
| P0.2 | A1（子代理） | done | 无 | （本次提交） | `python3 scripts/probe/probe_04_rate_limits.py`（A0 复跑 PASS，29%/23% 实时值；摘要键名 bug 已修）；其余探针见 scripts/probe/README.md | docs/CODEX_CAPABILITIES.md、docs/proto-samples/、scripts/probe/、artifacts/probe/ | **桌面运行时不可旁听**（私有 stdio 子进程，无 daemon）→ P3.6 保持 blocked；额度/bridge-owned 事件流可用；CLI 维持 0.152.0，升级需重生成 schema 基线（P3.1 决策点） |
| P0.3 | A3（子代理） | done | 无 | （已提交） | docs/HARDWARE.md §8 复核命令（A0 抽查 6/6 通过） | docs/HARDWARE.md | 10 项 unverified 已列 §7；ESP-IDF 已锁 v5.5.5 回填 VERSIONS |
| P0.4 | A0（子代理起草+A0冻结） | done | P0.2 能力已知 | （已提交） | `uv run --with jsonschema python scripts/check_protocol.py`（15/15 PASS 退出码 0）；`cc -std=c99 -pedantic -fsyntax-only shared/state/codex_state.h` | protocol/、shared/state/、tests/fixtures/protocol/、artifacts/protocol/P0.4-draft-check-report.txt | 10 项裁决已落实（INTERFACES §1a/§3）；深度12合法侧 fixture 归 P1.5 |
| P0.5 | A4（子代理） | done | P0.4（done） | （本次提交） | `python3 scripts/gen_crc_vectors.py --verify`（A0 复跑退出码 0）；`cc -std=c99 -pedantic -fsyntax-only -I shared/transport shared/transport/cdt_frame.h` | protocol/transport.md、shared/transport/cdt_frame.h、scripts/gen_crc_vectors.py | KEY 配对确认/权限弹窗文案/MTU23 吞吐三项待 P3 小样实测（transport.md §7 已列判据） |

**P0 Gate 结论（2026-09-10，A0）：通过。** 协议冻结（P0.4）、能力矩阵（P0.2）、硬件证据（P0.3）、传输冻结（P0.5）齐备；桌面实时源不可接已如实记录为真实集成阻塞（P3.6 blocked），不以 Mock 掩盖；P1/P2/P4/P5 照常推进。剩余待勾选项（队列/内存边界）随 P1.4/P2 落实。

## P1：State、Mock 与模拟器基础

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P1.1 | A1（子代理） | done | P0.4（done） | （本次提交） | `uv run --python 3.12 --with pytest --with jsonschema pytest tests/bridge -q`（A0 复跑 58 passed exit 0） | bridge/events.py、bridge/state/{reducer,render,engine}.py、tests/bridge/ | waitingOnUserInput flag 合成归 P3.1 adapter |
| P1.2 | A1（子代理） | done | P1.1（done） | （本次提交） | `uv run --python 3.12 python -m bridge --source mock --scenario lifecycle --out DIR`（A0 复跑 exit 0）；**交叉验证：Bridge 快照→C 端 shared 解析器 30/30 PASS**（双端契约一致） | bridge/sources/{mock,replay}.py、bridge/__main__.py、tests/fixtures/bridge/、artifacts/bridge/ | S04/S09/S21 场景 fixture 对齐归 P2.5；断连信号由 P3.1 adapter 产生 |
| P1.3 | A2（子代理） | done | P0.1（done） | （已提交） | `simulator/smoke_offscreen.sh`（A0 复跑 exit 0）；`scripts/build_simulator.sh` | simulator/、scripts/{vendor_lvgl,build_sdl2,build_simulator}.sh、artifacts/sim/ | LVGL sha256 `9c6f8230…`、SDL2 sha256 `560da2e5…`、cmake 3.31.6 待回填 VERSIONS；--scenario 参数与 ui_key 接线归 P2 |
| P1.4 | A0 | done | P0.4（done） | （已提交） | `scripts/build_shared.sh`（36/36 PASS exit 0；ASan/UBSan 复验 36/36） | shared/{state,display,presenter} 新增 9 文件、tests/shared/ | 自研有界 JSON 解析器（零外部依赖）——VERSIONS「JSON 解析库」行按 in-house 收编；duration_mins≤65535 已在解析器执行（schema 同步见 P2 提交） |
| P1.5 | A5（子代理） | done | P0.4（done） | （本次提交） | `uv run --with jsonschema python scripts/check_protocol.py`（A0 复跑 16/16 PASS exit 0） | tests/SCENARIOS.md、tests/UI_CONTRACT.md、tests/fixtures/scenarios/、F15+MANIFEST | S01–S21：18 ready / 3 待 P1.2 mock（S04/S09/S21）；F16 已补记；F15 fixture 文件本体 2026-09-10 补提交（P1.5 提交时漏 add，16/16 复验 PASS） |

## P3：真实 Codex 与双 Transport（进行中）

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P3.1 | A1（子代理） | done | P0.2+P1.1（done） | （本次提交） | `pytest tests/bridge -q`（A0 复跑 76 passed exit 0）；`scripts/codex_live_smoke.py`（A0 复跑 exit 0，13 快照）；**live 快照→C 端交叉验证 33/33 PASS**；脱敏扫描唯一命中为 desk-terminal 正则误报 | bridge/sources/codex.py、bridge/codex_rpc.py、bridge/redact.py、tests/bridge/test_codex_adapter.py、scripts/codex_live_smoke.py、artifacts/codex/ | A0 认可 test_determinism 排除 IO 模块（纯度扫描不应覆盖 adapter）；重连缺线程老化策略归 P3.2；requestUserInput/turn/plan/updated 待 P3.2 实证；P3.6 维持 blocked |
| P3.2–P3.5 | - | todo | P3.1/P0.5 | - | - | - | 传输小样按 transport.md §7 |

## P5：低功耗和低压保护（逻辑部分先行）

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P5.1 | A3（子代理） | done | P1.4（done）；P4.4 仅逻辑参数（硬件校准保持阻塞） | （本次提交） | `sh scripts/build_shared.sh`（A0 复跑 36+101=137 PASS exit 0；ASan 版 101 PASS 无报告） | shared/power/{cdt_power.h,cdt_power.c}、tests/shared/test_power.c、artifacts/power/ | A0 三裁决：BATTERY_FAULT 冻结为 flag+动作位（不增枚举，§7.3 定义其为入 CRITICAL 路径）；BOOT_CHECK 迟滞带不停留问题=自消解路径（放电→critical→睡；充电→recovery→active），P5.4 真机观察；CRITICAL 单拍保持满足"成立后 2s 内"（≤1s 显示+≤1s 末帧）。另修复 build_shared.sh 泄漏检查 BRE→ERE（此前空转，已真实复跑） |

## P2：完整页面与自动回归（进行中）

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P2.4（工具部分） | A5（子代理） | done | P1.5 契约（done） | （本次提交） | `uv run --python 3.12 python scripts/check_ui.py --self-test`（A0 复跑 SELF-TEST: PASS exit 0：正向对照 2/2 退出码 0；翻转 1 像素 PBM/PNG/多帧三组负向对照均 FAIL、差异像素=1、退出码 1、三图保留） | scripts/check_ui.py、artifacts/ui/selftest/ | golden 生成与语义断言表接入归 P2 集成（依赖 P2.2/P2.3 页面）；--golden 永只读、无 --update-golden（契约 §2.6 红线落实） |

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P2.1 | A2（子代理） | done | P1.3+P1.4（done） | （本次提交） | `sh scripts/build_presenter_tests.sh`（A0 复跑 42 PASS exit 0）；`--state` 渲染+两次 cmp 确定性 OK（A0 复验）；目检 needs_you 帧六要素 | shared/presenter/{cdt_view,cdt_presenter}、shared/ui/、simulator/（--state/--capture-frame）、tests/shared/test_presenter.c、artifacts/ui/ 10 帧 | CJK 为 ? 占位归 P2.2（Noto Sans SC）；LOW BATTERY 完整页归 P2.3；A0 裁决：门禁改分层（portable 禁 lvgl、ui 允许 lvgl 禁 esp/SDL），并修复其丢掉 grep -E 的回归+注释误报，五路注入验证生效 |
| P2.2+P2.3 | A2（子代理） | done | P2.1（done） | （本次提交） | `sh scripts/build_presenter_tests.sh`（A0 复跑 42+67 PASS exit 0，门禁 4/4）；`sh scripts/build_shared.sh`（101 PASS exit 0）；`sh simulator/smoke_offscreen.sh`（exit 0） | shared/ui/{cdt_nav,cdt_ui_internal,cdt_ui_pages.h,cdt_ui_agents,cdt_ui_plan,cdt_ui_usage,cdt_ui_lowbat}、tests/shared/test_pages.c、artifacts/ui/page_{now,agents,plan,usage}.bmp+overlay_{disconnected_agents,stale_plan}.bmp+lowbat_forced.{bmp,log}、multi_agents.json | 遗留四项：①P2.1 NOW 页 make_label 后调 make_box 清字体样式（实际 LV_FONT_DEFAULT 14px 渲染，恰与 F_BAR 等值故视觉无差）→A0 裁决：随 P2.4 集成先修再产 golden；②CJK Noto Sans SC 子集未接（? 占位）→并入 P2.4 集成、先于 golden；③USAGE 倒计时=generated_at_ms+fresh 单调增量近似（陈旧冻结，代码注释已声明）；④模拟器 --battery-mv/--battery-seq 并存以后者为准 |
| P2.4 golden 集成、P2.5 | A6（子代理） | doing | P2.2+P2.3（done） | - | - | - | 2026-09-10 派发：NOW 页字体修复+Noto Sans SC 子集（均先于 golden）→ golden 候选生成（A0 认可后定稿）→ check_ui 语义断言表接入 → P2.5 全生命周期回放 |




## 环境事实备忘（影响排期）

- 本机无 Homebrew：cmake/SDL2 获取方案已定（docs/VERSIONS.md「宿主缺口」节），在 P1.3 执行。
- codex CLI 0.152.0 在机，app-server 带协议生成命令（P0.2 使用）。
- 同板参考项目 /Users/cui/Documents/Projects/hermes-courier（2026-09-09 收官，官方 demo 已 vendor），只读引用，不复制其固件代码。
- ESP32-S3-RLCD-4.2 真机是否在手未确认：P4 起需要硬件，届时向用户确认。
