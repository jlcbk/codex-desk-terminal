# tests/UI_CONTRACT.md — check_ui.py 与模拟器回放命令契约（doc-only，P1.5 / A5）

状态：v1（2026-09-10）。**本文件是契约，不是实现**：`scripts/check_ui.py` 在 P2.4 才编写；模拟器回放参数在 P1.3 骨架上扩展。实现前，A2（模拟器）与 A5（check_ui.py）以本契约为对齐目标；计划 §8 允许 A0 调整最终命令名称，但调整必须同步本文件、README 与 CI。

真源：`docs/DEVELOPMENT_PLAN.md` §8（命令接口、截图实现、生命周期行）；`docs/INTERFACES.md` §4（注入 JSONL）；`tests/SCENARIOS.md`（场景与断言真源）。

## 1. 命令总览（与计划 §8 命令接口的对应）

| 计划 §8 示例（占位） | 本契约冻结形式 | 说明 |
|---|---|---|
| `./build/simulator/codex-display-sim --scenario tests/fixtures/lifecycle.jsonl --fixed-clock --capture-dir artifacts/ui` | 路径改为 `tests/fixtures/scenarios/<S 编号>_<slug>.jsonl` 或 lifecycle 家族文件；参数名不变 | §3 |
| `python scripts/check_ui.py --golden tests/golden --actual artifacts/ui` | 参数名不变；补充可选 `--scenario` 过滤 | §2 |
| `python -m bridge --source mock --scenario lifecycle` | A1/P1.2 范围；只要求其输出可映射为本契约的场景 JSONL | 不在本契约内定义 |

测试产物写 `artifacts/`（Git 忽略），golden 写 `tests/golden/`（版本控制）——与计划 §8 一致。

## 2. scripts/check_ui.py CLI 契约（P2.4 实现）

### 2.1 用法与参数

```sh
python scripts/check_ui.py --golden tests/golden --actual artifacts/ui [--scenario <id>]
```

| 参数 | 必填 | 语义 |
|---|---|---|
| `--golden` | 是 | golden 目录（tests/golden/），只读，绝不写入 |
| `--actual` | 是 | actual 帧目录（通常 artifacts/ui/），只读；diff 图写到该目录 |
| `--scenario` | 否 | 只检查指定场景（S 编号或 slug，如 `S04` / `S04_plan_update`）；缺省检查全部 |

### 2.2 帧配对规则

- 优先消费 `--actual` 目录内的 `manifest.jsonl`（由模拟器回放产出，见 §3.4）：每行声明 frame 文件 → scenario → frame_index → at_ms → action → seq。golden 文件名 `S<NN>_<slug>__f<NNN>_<tag>.png` 中的 `f<NNN>` 对应 manifest 的 frame_index。
- 无 manifest 时按文件名约定退化配对：golden 与 actual 同名同目录结构一一对应。
- 单帧场景 golden 名无 `__f` 后缀（如 `S01_idle.png`），对应该场景唯一终帧。

### 2.3 检查内容与输出

每个场景（逐帧）执行两类检查：

1. 像素比较：actual 与 expected（golden）均为 400×300 逻辑单色帧；逐像素 XOR 得差异像素数（整数）。任何一侧尺寸 ≠400×300 即 FAIL，差异像素数记 `n/a` 并输出原因。diff 图写到 actual 同目录：`<帧主名>__diff.png`，黑=差异、白=一致。
2. 语义断言：check_ui.py 内置 per-scenario 断言表，内容与 `tests/SCENARIOS.md` §3「断言要点」一一对应（表内注释标注 SCENARIOS.md 版本号；SCENARIOS.md 更新必须同步该表）。包括：文本存在（状态词、`--`、提示语）、关键控件坐标不越界、页面优先级（电池强制页 > 业务页、断连/陈旧提示独立）。

stdout 格式（逐场景一行，FAIL 行必须含原因与差异像素数）：

```text
[PASS] S01_idle            帧数=1 差异像素=0 语义=ok
[FAIL] S09_low_battery     帧数=5 差异像素=17 帧=__f004_forced_page 语义=FAIL(状态词非 NEEDS YOU 优先级)
汇总: 20/21 PASS
退出码: 1
```

### 2.4 退出码语义

| 退出码 | 含义 |
|---|---|
| 0 | 全部场景 PASS（差异像素=0 且语义断言全过） |
| 1 | 存在像素差异或语义断言失败；含：actual 缺帧（missing）、无 golden 对应的多余帧（unapproved-frame）、尺寸不符 |
| 2 | 环境错误：--golden 目录不存在、文件不可读等（非测试失败，不得用于掩盖回归） |

golden 缺失对应 actual（unapproved-frame）按 FAIL 处理——新帧必须经 A0 审批进入 golden，防止未经审批的渲染变化混入。actual 缺 golden 对应帧（missing）同样 FAIL——回放不完整即是回归。

### 2.5 「一个像素必须失败」自检（P2.4 验收项）

check_ui.py 必须自带自检（独立入口或 `--self-test`）：对任一 golden 翻转恰好 1 个像素生成临时 actual → 断言该场景 FAIL、差异像素数=1、退出码 1、三图保留。证据写 artifacts/ui/selftest/。P2.4 验收行原文：「修改一个像素能令测试失败；失败返回非零并保留图」。

### 2.6 golden 更新规则（重申，红线）

- golden 只能来自模拟器实际渲染产物，经 A0 审核 deliberate UI 变化后人工复制进 tests/golden/ 并提交。
- 禁止 CI 自动接受、自动重生成或自动覆盖 golden；禁止用测试截图覆盖 golden 来消除失败（AGENTS.md）。
- 不提供任何 `--update-golden` / `--accept` 类参数；check_ui.py 对 tests/golden/ 永远只读。

## 3. 模拟器回放命令契约（在 P1.3 骨架上扩展）

### 3.1 用法

```sh
./build/simulator/codex-display-sim --scenario <file.jsonl> --fixed-clock --capture-dir artifacts/ui
```

CI/无头环境叠加 `SDL_VIDEODRIVER=dummy`（P1.3 smoke_offscreen.sh 已验证该路径可离屏运行）。

| 参数 | 语义 |
|---|---|
| `--scenario` | 注入 JSONL 路径（§3.2 格式） |
| `--fixed-clock` | 启用虚拟单调时钟与确定性渲染（无此参数不允许产出可比对帧；实时/墙钟模式仅用于人工调试，帧不命名进 artifacts/ui） |
| `--capture-dir` | 帧与 manifest 输出目录；目录按场景文件主名建子目录 |

### 3.2 输入：注入 JSONL（INTERFACES §4 细化）

- 每行一个 JSON 对象：`{"at_ms": <非负整数>, "action": <枚举>, "payload": <对象>}`；`#` 开头的注释行与空行忽略（用于文件头说明）。
- `at_ms` 为虚拟时钟调度点，全文件必须单调不减；虚拟时钟从 0 开始。
- 加载时**全量预检**后再回放（先完整校验、失败不出帧，呼应协议层「先完整校验再原子替换」精神）：未知 action、at_ms 回退、payload 不符、AppState 非法均使进程以退出码 1 失败并打印行号与原因。

action 与 payload 契约：

| action | payload | 语义 |
|---|---|---|
| `app_state` | 完整 AppState，必须通过 `protocol/state.schema.json` 与解析器级检查（同 check_protocol.py B1–B5 规则集） | 注入一份业务快照；seq 按业务快照走（同 epoch 须严格递增，fixture 责任） |
| `battery_sample` | `{"battery_mv": <0–65535 整数或 null>, "battery_valid": <bool>}`；battery_valid=false 时 battery_mv 必须 null | 一次设备本地电池采样；经与固件相同的 Power FSM 纯逻辑（P1.4/P5.1 共享实现）驱动 LOW BATTERY；回放器**不得**提供直接绘制低压页的捷径（INTERFACES §4：「不能只换一张图冒充保护已验证」） |
| `key` | `{"key": "short_press" \| "long_press"}` | 一次 KEY 事件；语义按 §6（短按轮换四页/子页，长按静音当前提醒；长按不再触发短按） |
| `link` | `{"link_state": "connected" \| "stale" \| "disconnected"}` | 链路故障注入；15s 存活快照 / 45s stale / 150s disconnected 的自然计时仍由回放器按虚拟时钟执行，link 动作只做显式提前注入 |
| `advance_time` | `{"to_ms": <非负整数, ≥当前虚拟时钟>}` | 将虚拟时钟快进到 to_ms，不产生事件；用于触发 45s/150s/30s critical_hold 等「无事件等待」 |

- 生产固件禁用 battery_sample 输入（INTERFACES §8）；本接口仅存在于模拟器与显式标记 mock 的 Bridge。
- AppState 载荷内业务时间戳（generated_at_ms 等）与虚拟时钟解耦；运行/等待时长显示在 source 与 link 均 fresh 时由虚拟时钟单调增量推进，陈旧后冻结（INTERFACES §4）。

### 3.3 输出：确定性帧

- 每条 action 应用后，若 ViewModel 发生变化，则导出一帧：display framebuffer 经公共单色转换后的逻辑 400×300 PNG（截图固定条件见 tests/SCENARIOS.md §5：固定字体/DPI/LVGL 版本/随机种子，禁动画、禁光标闪烁、禁真实墙钟）。
- 帧命名：`<场景文件主名>__f<NNN>_<tag>.png`；NNN 为该场景帧序号（从 001 起），tag 由场景文件在该 action 行的可选字段 `"tag": "<slug>"` 提供或回放器按 action 自动生成；无 ViewModel 变化的 action 不出帧但记入 manifest。
- 同一输入文件的输出（帧集合、命名、像素）必须完全确定，重复运行零差异；这是 golden 可审批的前提。

### 3.4 输出：manifest.jsonl

`--capture-dir` 下每场景写一份 `manifest.jsonl`，每行：

```json
{"frame": "S09_low_battery__f004_forced_page.png", "scenario": "S09_low_battery", "frame_index": 4, "at_ms": 65000, "action": "battery_sample", "seq": 3}
```

- `seq` 为该帧时刻已应用的最新 AppState seq（尚无快照时为 null）。
- check_ui.py 优先按此 manifest 配对 golden（§2.2）。

### 3.5 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 回放完成，帧与 manifest 已产出 |
| 1 | 场景文件违规：未知 action、at_ms/advance_time 回退、payload 不符、AppState 校验失败（输出行号+原因，不出帧） |
| 2 | 运行环境错误：显示初始化失败、--capture-dir 不可写等 |

## 4. 生命周期逐帧断言规则（不只验末帧）

计划 §8：「生命周期须检查中间每一步，不只最后一帧」；P2.5 验收：「五个阶段均有快照；PLAN UPDATE 保持 WORKING；最终低压页出现」。规则：

1. lifecycle 家族场景文件头部（`#` 注释）必须声明 **checkpoint 帧清单**：`frame_index → 期望页面/状态词`（如 f001=NOW/WORKING、f002=PLAN/WORKING(plan v2)、f003=NOW/NEEDS YOU、f004=NOW/DONE、f005=LOW BATTERY 强制页）。
2. 五个阶段（WORKING、PLAN UPDATE 保持 WORKING、NEEDS YOU、DONE、最终 LOW BATTERY 页）每阶段至少 1 个 checkpoint 帧；缺任一阶段、manifest 帧缺失、checkpoint 声明与实际帧不符均为 FAIL（退出码 1），不允许降级为警告。
3. check_ui.py 对 lifecycle 场景逐 checkpoint 比对（像素+语义），并对相邻 checkpoint 之间「不应出现的页面」（如 DONE 阶段不得出现 NEEDS YOU 粗框）做语义否定断言。
4. 中间态覆盖要求同样适用于多帧场景：S04（plan 更新前后）、S09（LOW_WARN 进入、critical_hold 30s 计时中、强制页出现、key 被拒）、S11/S12（提示出现与恢复）、S21（epoch 前后）——各场景「断言帧」列出的每一帧都必须有对应 golden 与断言，不得只审末帧。
5. 本文件与 tests/SCENARIOS.md 为唯一契约；P2.4 实现若需偏离（参数名、diff 表现形式），由实现 owner 提出、A0 合并后修订本文件，禁止实现静默偏离契约。
