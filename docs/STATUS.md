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
| P3.2 | A1（子代理） | done | P3.1（done） | （本次提交） | `pytest tests/bridge -q`（A0 复跑全量 89 passed exit 0，含取消消歧 deselect 的门禁项）；live user-input 17 快照/cancel 9 快照语义抽查吻合 §3 | bridge/sources/codex.py、tests/bridge/（+15 测试）、artifacts/codex/p32-*（6 组脱敏自检过） | A0 裁决：①transports/ 入纯度排除（IO 层）；②老化采纳提案 C（重连重建 StateEngine=新 epoch 全量替换，零契约变化），实现归下一 A1 波次，A/B 备案；③plan live 未触发=诚实否定证据，CLI 升级时重测；④item/plan/delta 形态缺口挂契约观察项 |
| P3.3（loopback） | A4-W（子代理×2：中断+续作） | done | P0.5+P1.4（done） | （本次提交） | `pytest tests/transport/wss -q`（A0 复跑 54 passed exit 0）；loopback 自验 7/7（A0 复跑 all_passed=True exit 0） | bridge/transports/wss/、tests/transport/wss/、config/examples/、scripts/gen_dev_certs.sh、artifacts/transport/wss/ | 续作修复 2 真缺陷（__init__ 导出缺口、1009 计数失效）；W2/W5 真机项清单内嵌；板 1301 已确认 USB 枚举（/dev/cu.usbmodem1301，Espressif 0x303a） |
| P3.4（host 阶段） | A4-B（子代理×2：中断+续作） | done | P0.5+P1.4（done） | （本次提交） | `sh scripts/build_transport_tests.sh`（A0 复跑 97 PASS+CRC 4/4+门禁 exit 0）；`run_loopback.py`（A0 复跑全过，18/18，cmp 4 组逐字节一致） | shared/transport/{crc32,fragmenter,reassembler}、bridge/transports/ble/、tests/transport/ble/、scripts/build_transport_tests.sh、artifacts/transport/ble/ | 五项契约偏差处置合理（异 id 中途片=CONTEXT_MISMATCH、超时语义差记录不改、ACK 超时常量归适配器层、按名发现→B2 改 UUID 过滤、证据目录参数化）；B0-B5 真机取证清单内嵌 |
| P3.2-老化C | A1（子代理） | done | P3.2（done） | （本次提交） | `pytest tests/bridge -q`（A0 复跑 90 passed exit 0） | bridge/sources/codex.py 重连重建 StateEngine+tests/bridge/（+1 用例） | 裁决 C 落地：新 epoch=`基底-r<N>`、seq 归零、断连期 stale 快照保留；证据文件名改 snapshot_<epoch>_<seq> 防覆盖；live 端到端老化观察记未验证；__main__ docstring 已由 A0 更正 |
| P3.5 | A4+A5（子代理） | done | P3.3+P3.4（done） | （本次提交） | `sh scripts/run_p35.sh`（A0 复跑 exit 0，16/16；P3.4 回归 exit 0） | tests/transport/integration/（C harness 串联 reassembler→store→presenter）、scripts/run_p35.sh、artifacts/transport/p35/ | **P3 软件阶段收官**：UI 不回退（真实 presenter 判定）/单活动 transport/链路可观测三锚点全绿；真机项（B0-B5/W2/W5/新鲜度实测）随 P4/P5 |


**P3 Gate 补记（2026-09-11，A0）：P3 软件部分通过。** 双传输 loopback 语义（P3.3/P3.4/P3.5）+ 真实 codex 链路（P3.1/P3.2）齐备；P3.6 桌面旁听维持 blocked（P0.2 结论），不 Mock 掩盖；真机传输项挂 P4/P5。

## P4：真机显示、电池与输入（进行中——板 1301 在手，2026-09-10 用户确认）

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P4.1a | A3（子代理） | done | P0.1/P0.3 | （已提交） | `idf.py --version`=5.5.5、vendor HEAD=eb1f634、03_ADC_Test 构建零编译警告（A0 三项复核实测吻合） | scripts/vendor_waveshare.sh、artifacts/idf/（13 件） | VERSIONS 已回填 IDF 双 ID（tag ff1bac0/commit b774170f，P4.1a 发现并经 A0 核实）；磁盘余 25Gi |
| P4.1b | A3（子代理） | done | 板在位 | （备份不入库） | `shasum -a 256`（A0 独立复核 54afe421… 吻合）；独立复读头 4KB cmp 一致 | artifacts/board/backup-20260910-2328/（16MB 全量+README+恢复命令） | ESP32-S3 rev v0.2/N16R8 确认；恢复命令已记录未执行（按授权纪律） |
| P4.1c | A3（子代理） | done | P4.1a+P4.1b | （产物不入库） | `idf.py flash` 3 次全 Hash verified；monitor 80s+30s；A0 抽查 boot.log 五项核验吻合（电压一手核实 4.131-4.143V） | artifacts/board/p41c-{boot,boot-2nd,flash1-3}.log、p41c-summary.md | **P4.1 收官**：无 boot loop、三烧不砖；PCB 修订仍需丝印目检（维持 unverified） |
| P6.2（PC 前置压缩） | A5（子代理） | done | P3 软件（done） | （本次提交） | `sh scripts/run_soak.sh`（A0 复跑 exit 0 53s；overall_pass=true） | scripts/{soak_bridge.py,run_soak.sh}、artifacts/soak/（report+CSV+参照物） | 确定性 500+400 轮全同/golden 零差异；内存漂移 560KiB（判据入报告）；两 CLI 冻结差异记录（replay 用 --file；--fixed-clock 与 --scenario 互斥）；真机 24h/断连/权限项归 P5/P6 真机段 |
| P4.3 | A2+A3（子代理） | done（串口+对齐；目检待用户） | P2 Gate+P4.2 | （本次提交） | `p43_align.py`（A0 复跑 PASS：6/6 固件=模拟器 CRC，5 场景与 golden 逐位全等）；95s 串口两循环 CRC 逐一相同、零 panic | firmware/components/cdt_lvgl、firmware/main/{lvgl_port,p43_scenes}、firmware/tools/、artifacts/board/p43-* | A0 裁决：①COMPONENTS 临时排除 transport（合并传输时撤）；②presenter 格式化警告已由 A0 修补（uint32 显式 cast，42+137 复跑绿）；LVGL 两真 bug 已修（reshape 自旋+64B、I1 +8B 调色板偏移）；**用户目检+照片待补** |
| P3.3/P3.4 固件骨架 | A4（子代理） | done（编译级） | P3.3+P3.4 host（done） | （本次提交） | `cd firmware/examples/transport_skeleton && idf.py build`（A0 复跑 exit 0，零警告；bin 783KB，较空基线 +574KB） | firmware/components/transport/（WSS 客户端+NimBLE 外设）、firmware/examples/transport_skeleton/、artifacts/idf/ | 依赖 esp_websocket_client 1.8.0 已回填 VERSIONS；**SPKI pinning 未实现**（esp-tls 无对端证书钩子，留注入点，W5 真机项）；BLE 选 NimBLE（省内存）；KEY 配对确认默认拒绝不降级；真机 W5/B0-B5 全部未验证 |
| P4.2 | A3（子代理） | done | P4.1+P1.4 | （已提交） | 串口侧：`idf.py build` 零警告、CRC 三循环稳定、板载 CRC32=zlib 锚点（A0 抽查日志吻合）；**用户目检 2026-09-11 确认五图案全部符合**（无花屏、方向正确、无残影） | firmware/{CMakeLists,sdkconfig.defaults,components/display_st7305,main}、artifacts/board/p42-* | SPI 10MHz 起步（24MHz 爬升留后）；无 busy/TE 兜底 20ms 延时；照片证据待用户补拍（可选） |

## P5：低功耗和低压保护（逻辑部分先行）

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P5.1 | A3（子代理） | done | P1.4（done）；P4.4 仅逻辑参数（硬件校准保持阻塞） | （本次提交） | `sh scripts/build_shared.sh`（A0 复跑 36+101=137 PASS exit 0；ASan 版 101 PASS 无报告） | shared/power/{cdt_power.h,cdt_power.c}、tests/shared/test_power.c、artifacts/power/ | A0 三裁决：BATTERY_FAULT 冻结为 flag+动作位（不增枚举，§7.3 定义其为入 CRITICAL 路径）；BOOT_CHECK 迟滞带不停留问题=自消解路径（放电→critical→睡；充电→recovery→active），P5.4 真机观察；CRITICAL 单拍保持满足"成立后 2s 内"（≤1s 显示+≤1s 末帧）。另修复 build_shared.sh 泄漏检查 BRE→ERE（此前空转，已真实复跑） |

## P2：完整页面与自动回归（已完成）

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P2.4（工具部分） | A5（子代理） | done | P1.5 契约（done） | （本次提交） | `uv run --python 3.12 python scripts/check_ui.py --self-test`（A0 复跑 SELF-TEST: PASS exit 0：正向对照 2/2 退出码 0；翻转 1 像素 PBM/PNG/多帧三组负向对照均 FAIL、差异像素=1、退出码 1、三图保留） | scripts/check_ui.py、artifacts/ui/selftest/ | golden 生成与语义断言表接入归 P2 集成（依赖 P2.2/P2.3 页面）；--golden 永只读、无 --update-golden（契约 §2.6 红线落实） |

| ID | Owner | 状态 | 依赖 | 提交 | 验证命令 | 证据 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| P2.1 | A2（子代理） | done | P1.3+P1.4（done） | （本次提交） | `sh scripts/build_presenter_tests.sh`（A0 复跑 42 PASS exit 0）；`--state` 渲染+两次 cmp 确定性 OK（A0 复验）；目检 needs_you 帧六要素 | shared/presenter/{cdt_view,cdt_presenter}、shared/ui/、simulator/（--state/--capture-frame）、tests/shared/test_presenter.c、artifacts/ui/ 10 帧 | CJK 为 ? 占位归 P2.2（Noto Sans SC）；LOW BATTERY 完整页归 P2.3；A0 裁决：门禁改分层（portable 禁 lvgl、ui 允许 lvgl 禁 esp/SDL），并修复其丢掉 grep -E 的回归+注释误报，五路注入验证生效 |
| P2.2+P2.3 | A2（子代理） | done | P2.1（done） | （本次提交） | `sh scripts/build_presenter_tests.sh`（A0 复跑 42+67 PASS exit 0，门禁 4/4）；`sh scripts/build_shared.sh`（101 PASS exit 0）；`sh simulator/smoke_offscreen.sh`（exit 0） | shared/ui/{cdt_nav,cdt_ui_internal,cdt_ui_pages.h,cdt_ui_agents,cdt_ui_plan,cdt_ui_usage,cdt_ui_lowbat}、tests/shared/test_pages.c、artifacts/ui/page_{now,agents,plan,usage}.bmp+overlay_{disconnected_agents,stale_plan}.bmp+lowbat_forced.{bmp,log}、multi_agents.json | 遗留四项：①P2.1 NOW 页 make_label 后调 make_box 清字体样式（实际 LV_FONT_DEFAULT 14px 渲染，恰与 F_BAR 等值故视觉无差）→A0 裁决：随 P2.4 集成先修再产 golden；②CJK Noto Sans SC 子集未接（? 占位）→并入 P2.4 集成、先于 golden；③USAGE 倒计时=generated_at_ms+fresh 单调增量近似（陈旧冻结，代码注释已声明）；④模拟器 --battery-mv/--battery-seq 并存以后者为准 |
| P2.4 golden 集成、P2.5 | A6（子代理） | done | P2.2+P2.3（done） | （本次提交） | 见下分项 | 见下分项 | 见下分项 |

P2.4 集成+P2.5 验收明细（A0 复跑全绿，2026-09-10）：
- **阶段一（先于 golden）**：NOW 页 make_label/make_box 字体缺陷已修（cdt_uii_text，42 断言复跑 PASS）；Noto Sans SC v2.004 子集接入（自研 fonttools 导出器 857 码点，A0 目检 CJK 真实渲染：fixcheck_cjk_now.png「中文项目名与English混排」等清晰无叠字；ASCII 逐像素不变）。
- **阶段二（golden）**：`gen_goldens.py` 显式生成（默认候选目录，写 tests/golden 需 --golden-dir --force）；105 帧+SHA256SUMS；A0 复验 105/105 sha256 OK、22/22 场景零像素差异、语义断言激活（语义=ok）、check_ui --self-test PASS（--golden 只读红线保持）、1 像素翻转必败。
- **阶段三（P2.5）**：`replay_lifecycle.py` A0 复跑全部通过 exit 0——bridge replay 10 快照逐字段一致、五阶段快照断言（plan_update 保持 WORKING/PLAN 1/3）、时长断言（终态定格）、短按被拒/长按只静音。
- **门禁**：42+67/36+101 断言、双平台头门禁、smoke、check_protocol 16/16、bridge pytest 76 全绿。
- **附带收编**：①cdt_json.c P1.4 潜伏空指针修复（cdtj_read_number 浮点分支未判 dval==NULL，skip_value 跳浮点字面量必段错误——A0 复核确认真实缺陷、一行守卫修复）；②presenter 终态时长定格（S06 语义）；③模拟器 --scenario/--capture-dir 注入回放（UI_CONTRACT §3 契约落地，+823 行宿主层）；④S04/S09/S21 fixture 对齐定稿（P1.2 阻塞项清账）。
- **SCENARIOS §2/§3 落地偏差四项**（帧名 __f 后缀/无变化不出帧/超集帧集/按键次数真值）：以 tests/fixtures/scenarios/README.md 为准，A0 认可，待 A5 下轮修订 SCENARIOS.md 同步。




## 环境事实备忘（影响排期）

- 本机无 Homebrew：cmake/SDL2 获取方案已定（docs/VERSIONS.md「宿主缺口」节），在 P1.3 执行。
- codex CLI 0.152.0 在机，app-server 带协议生成命令（P0.2 使用）。
- 同板参考项目 /Users/cui/Documents/Projects/hermes-courier（2026-09-09 收官，官方 demo 已 vendor），只读引用，不复制其固件代码。
- ESP32-S3-RLCD-4.2 真机在手（板号 1301，/dev/cu.usbmodem1301），2026-09-10 起硬件阶段解锁；16MB 备份为板上固件恢复凭证。
