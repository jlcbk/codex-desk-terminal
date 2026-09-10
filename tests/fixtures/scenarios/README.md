# tests/fixtures/scenarios/ — UI 注入场景 fixtures（P1.5 / A5 建立；P2.4/P2.5 / A6 落地）

场景定义真源：`tests/SCENARIOS.md`（S01–S21）。行格式契约：`docs/INTERFACES.md` §4 + `tests/UI_CONTRACT.md` §3。

## 文件命名规则

- 最小场景文件：`S<NN>_<slug>.jsonl`，`<NN>` 与 slug 必须与 SCENARIOS.md §3 清单的编号一致（如 `S09_low_battery.jsonl`）。
- lifecycle 家族（P2.5 完整回放用）：`S_lifecycle.jsonl`（正式版，业务快照 = P1.2 bridge replay 源快照原文）。
- 文件头用 `#` 注释行声明：目的、状态、checkpoint 帧清单（tag → 期望页面/状态词）。
- 每行 JSON：`{"at_ms", "action", "payload"[, "tag"]}`；`tag` 仅标注 checkpoint 帧，命名用小写下划线。

## 生成与再生成（P2.4/P2.5 起）

全部 22 个文件由 `scripts/gen_scenarios.py` 确定性生成（可 `--check` 逐字节复验）：

```sh
uv run --python 3.12 --with jsonschema python scripts/gen_scenarios.py
```

- 正式 `S_lifecycle.jsonl` 的 app_state payload = `python -m bridge --source replay --file tests/fixtures/bridge/lifecycle_events.jsonl --epoch lifecycle-001 --anchor-ms 1789000000000` 的快照原文（进程内调用同一 replay 源生成，`scripts/replay_lifecycle.py` 每次运行都对齐复验）。
- fixture 中文文案全部在字体子集覆盖范围内（`scripts/gen_font_noto_sc.py` 同源校验）；S17 的 1 个 emoji（U+1F680）刻意不在子集，用于验证未知字形以可见 `?` 替代。
- 电池 trace 满足 §7.2 连续性（临界段 1Hz、间隔 ≤2s），LOW BATTERY 一律经真实 Power FSM 判定，无画页捷径。

## 当前索引（S01–S21 全部就绪 + 正式 S_lifecycle；2026-09-10，P2.4/P2.5 / A6）

| 文件 | 场景 | 状态 |
|---|---|---|
| S01_idle.jsonl | idle | ready-now（已创建，golden 已建） |
| S02_thinking.jsonl | thinking | ready-now（已创建，golden 已建；含 30s 存活快照防自然 stale） |
| S03_working.jsonl | working | ready-now（已创建，golden 已建） |
| S04_plan_update.jsonl | plan_update | 正式定稿（对齐 P1.2 mock 词汇：plan v1→v2，PLAN UPDATE 保持 WORKING） |
| S05_needs_you.jsonl | needs_you | ready-now（已创建，golden 已建） |
| S06_done.jsonl | done | ready-now（已创建，golden 已建；终态时长定格，frozen 帧无像素变化不出 golden） |
| S07_error.jsonl | error | ready-now（已创建，golden 已建） |
| S08_cancelled.jsonl | cancelled | ready-now（已创建，golden 已建） |
| S09_low_battery.jsonl | low_battery | 正式定稿（§7.2 连续电池 trace 经真实 FSM；短按被拒无帧、长按只静音） |
| S10_battery_unknown.jsonl | battery_unknown | ready-now（已创建，golden 已建；范围外采样与 invalid 像素相同 → 语义断言覆盖） |
| S11_disconnected.jsonl | disconnected | ready-now（已创建，golden 已建；150s 自然判定） |
| S12_stale.jsonl | stale | ready-now（已创建，golden 已建；45s 自然判定） |
| S13_multi_agents.jsonl | multi_agents | ready-now（已创建，golden 已建；8 可见/10 总数、排序分页、选中任务） |
| S14_empty_plan.jsonl | empty_plan | ready-now（已创建，golden 已建） |
| S15_long_plan.jsonl | long_plan | ready-now（已创建，golden 已建；含 128 字节顶格步骤文本） |
| S16_long_project_name.jsonl | long_project_name | ready-now（已创建，golden 已建；96 字节顶格中英混合） |
| S17_unicode.jsonl | unicode | ready-now（已创建，golden 已建；含 1 个 emoji → 可见 ? 替代） |
| S18_usage_missing.jsonl | usage_missing | ready-now（已创建，golden 已建） |
| S19_usage_0.jsonl | usage_0 | ready-now（已创建，golden 已建；reset 倒计时按数据计算） |
| S20_usage_100.jsonl | usage_100 | ready-now（已创建，golden 已建） |
| S21_bridge_restart.jsonl | bridge_restart | 正式定稿（epoch A seq=5 → 断连 → epoch B seq=0 → 恢复） |
| S_lifecycle.jsonl | lifecycle（正式版） | 已定稿（app_state = P1.2 bridge replay 快照原文 + §7.2 电池 trace；P2.5 五阶段 checkpoint） |

## 与 SCENARIOS.md golden 命名列的已知偏差（P2.4/P2.5 落地实测，报 A0/A5 折衷）

1. **golden 帧名统一带 `__f<NNN>_<tag>` 后缀**（UI_CONTRACT §3.3 的回放器命名）；SCENARIOS §2「单帧场景无 __f 后缀」不再适用——check_ui 按帧名配对，二者须一致。
2. **「无 ViewModel 变化不出帧」（UI_CONTRACT §3.3）** 使部分 SCENARIOS 断言帧无独立 golden：S06 `f002_frozen60s`、S09 `f005_key_rejected`、S10 `f002_out_of_range`（与 invalid 采样像素相同）——均由 manifest 的 view 语义断言覆盖（check_ui SEMANTIC_TABLE）。
3. **实际帧集 ⊇ SCENARIOS 断言帧清单**：多帧场景含导航中间帧/存活快照帧（如 S02/S05 的 30s keepalive——INTERFACES §4「Bridge 每 15s 发全量存活快照」，防止 45s 自然 stale 干扰 60s 断言）。
4. S09/S13/S14/S18/S19/S20 的按键次数按 UI_CONTRACT §6 导航真值（NOW→AGENTS→PLAN→USAGE）落地，较 SCENARIOS 骨架的「key(short)×2」略有增补。

## 红线

- 不生成任何 golden；golden 只能来自模拟器实际渲染并经 A0 审批（SCENARIOS.md §5）；显式生成走 `scripts/gen_goldens.py`。
- AppState 载荷手写虚构数据，不含任何真实凭证或用户数据（INTERFACES §8）。
- 电池序列只演示注入格式；不把「换了张低压图」当成保护已验证（INTERFACES §4）。
