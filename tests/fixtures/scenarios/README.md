# tests/fixtures/scenarios/ — UI 注入场景 fixtures（P1.5 / A5）

场景定义真源：`tests/SCENARIOS.md`（S01–S21）。行格式契约：`docs/INTERFACES.md` §4 + `tests/UI_CONTRACT.md` §3。

## 文件命名规则

- 最小场景文件：`S<NN>_<slug>.jsonl`，`<NN>` 与 slug 必须与 SCENARIOS.md §3 清单的编号一致（如 `S09_low_battery.jsonl`）。
- lifecycle 家族（P2.5 完整回放用）：`S_lifecycle.jsonl`（当前为格式与契约样例）及 P1.2 产出对齐后的正式文件。
- 文件头用 `#` 注释行声明：目的、状态（样例/正式）、checkpoint 帧清单（tag → 期望页面/状态词）。
- 每行 JSON：`{"at_ms", "action", "payload"[, "tag"]}`；`tag` 仅标注 checkpoint 帧，命名用小写下划线。

## 当前就绪索引（与 SCENARIOS.md §1 分布一致：ready-now 18 / 待 P1.2 mock 3 / 待 P2 渲染 0）

| 文件 | 场景 | 状态 |
|---|---|---|
| S_lifecycle.jsonl | lifecycle 格式与契约样例（四阶段 WORKING→PLAN UPDATE→NEEDS YOU→DONE + 独立电池 trace + 断连/恢复） | 样例已交付（P1.5）；正式 fixture 待 P1.2 对齐 |
| S01_idle.jsonl | idle | ready-now（未创建） |
| S02_thinking.jsonl | thinking | ready-now（未创建） |
| S03_working.jsonl | working | ready-now（未创建） |
| （S04_plan_update.jsonl） | plan_update | 待 P1.2 mock（骨架序列见 SCENARIOS.md S04） |
| S05_needs_you.jsonl | needs_you | ready-now（未创建） |
| S06_done.jsonl | done | ready-now（未创建） |
| S07_error.jsonl | error | ready-now（未创建） |
| S08_cancelled.jsonl | cancelled | ready-now（未创建） |
| （S09_low_battery.jsonl） | low_battery | 待 P1.2 mock（骨架电池 trace 见 SCENARIOS.md S09） |
| S10_battery_unknown.jsonl | battery_unknown | ready-now（未创建） |
| S11_disconnected.jsonl | disconnected | ready-now（未创建） |
| S12_stale.jsonl | stale | ready-now（未创建） |
| S13_multi_agents.jsonl | multi_agents | ready-now（未创建） |
| S14_empty_plan.jsonl | empty_plan | ready-now（未创建） |
| S15_long_plan.jsonl | long_plan | ready-now（未创建） |
| S16_long_project_name.jsonl | long_project_name | ready-now（未创建） |
| S17_unicode.jsonl | unicode | ready-now（未创建） |
| S18_usage_missing.jsonl | usage_missing | ready-now（未创建） |
| S19_usage_0.jsonl | usage_0 | ready-now（未创建） |
| S20_usage_100.jsonl | usage_100 | ready-now（未创建） |
| （S21_bridge_restart.jsonl） | bridge_restart | 待 P1.2 mock（骨架序列见 SCENARIOS.md S21） |

约定：「未创建」= 场景定义与断言已在 SCENARIOS.md 定稿，fixture 文件按需创建（ready-now 场景可随时由本清单直接落地，无需再等待输入）；括号包住的文件名表示该场景 fixture 定稿被 P1.2 阻塞，不得提前以自造序列顶替正式验收。fixture 创建后由 A0 记入 docs/STATUS.md。

## 红线

- 不生成任何 golden；golden 只能来自模拟器实际渲染并经 A0 审批（SCENARIOS.md §5）。
- AppState 载荷手写虚构数据，不含任何真实凭证或用户数据（INTERFACES §8）。
- 电池序列只演示注入格式；不把「换了张低压图」当成保护已验证（INTERFACES §4）。
