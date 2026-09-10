# tests/SCENARIOS.md — UI 测试清单真源（P1.5 / A5）

状态：v1（P1.5 交付，2026-09-10）。本文件是 21 个最小 UI 场景的唯一清单真源：每个场景映射到注入 fixture、断言要点、验收行与 golden 命名。
真源链：需求与验收行 = `docs/DEVELOPMENT_PLAN.md`（§6 页面规格、§8 测试策略）；注入格式 = `docs/INTERFACES.md` §4（已冻结）；协议 fixtures = `tests/fixtures/protocol/MANIFEST.md`（F01–F15）；回放与比较命令契约 = `tests/UI_CONTRACT.md`（doc-only）。

边界（P1.5 红线，见 AGENTS.md）：

- 本文件不实现 `scripts/check_ui.py`（P2.4 实现），不生成任何 golden 截图。golden 必须来自实际渲染（计划 §4 A5 边界：「A5 可在模拟器就绪前准备场景与断言，但 golden 必须来自实际渲染」）。
- P1.2 交付 Mock Bridge 后，其演示场景必须能映射回本清单；正式 lifecycle/电池 trace fixtures 以 P1.2 产出对齐后定稿。
- 清单变更（增删场景、改断言要点）由 A5 提出、A0 合并；本文件与 `tests/UI_CONTRACT.md`、check_ui.py 语义断言表三者必须同步。

## 1. 状态语义与当前分布

每个场景带一个状态值，语义如下：

| 状态 | 含义 |
|---|---|
| ready-now | 场景内容、断言要点、注入序列已在本文件定稿；fixture 只依赖已冻结契约（AppState schema、INTERFACES §4 注入格式、§7.2 阈值），可立即编写；P2 渲染就绪后即可执行出 golden |
| 待 P1.2 mock | fixture 的正式内容需与 P1.2 Mock Bridge 产出对齐（生命周期演示事件序列、独立电池 trace、epoch 重启演示）后定稿；本文件先固化断言与注入序列骨架，P1.2 不得偏离 |
| 待 P2 渲染 | 当前没有场景处于此状态：P2 渲染（P2.1–P2.3 页面 + P2.4 截图链路）是全部 21 个场景共同的执行前置（golden 必须来自实际渲染），属于全局门槛，不作为单个场景的差异化状态 |

当前分布：**ready-now 18 个（S01–S03、S05–S08、S10–S20），待 P1.2 mock 3 个（S04、S09、S21），待 P2 渲染 0 个**。

## 2. 公共约定（适用于全部场景）

- 场景 fixture：`tests/fixtures/scenarios/S<NN>_<slug>.jsonl`，格式为 INTERFACES §4 注入 JSONL（每行 `at_ms`/`action`/`payload`；action ∈ app_state/battery_sample/key/link/advance_time；`#` 注释行与空行忽略——格式细则见 tests/UI_CONTRACT.md §3）。
- 虚拟时钟从 0 起，`at_ms` 单调不减；AppState 载荷内的 `generated_at_ms`/`updated_at_ms` 等业务时间戳用固定虚构 UTC 基准 T0=1789000000000 加场景内偏移，与虚拟时钟解耦。
- 基准快照 B（各场景在此基础上改字段）：source=mock/connected/stale=false、单线程 `thread-demo` state=working、project=`codex-desk-terminal`、usage available=true 一窗口 42%；电池不经 AppState（§1：设备本地真源），未注入 battery_sample 的场景电压按 unknown 显示 `--`。
- 断言均为语义断言 + 像素比较双轨（§8 UI 行：「原尺寸单色golden；语义断言+像素比较」）；语义断言由 check_ui.py 按 check_ui 场景表执行，表内容与本文件断言要点一一对应。
- golden 命名：单帧场景 `tests/golden/S<NN>_<slug>.png`；多帧场景逐帧 `tests/golden/S<NN>_<slug>__f<NNN>_<tag>.png`，`<tag>` 为本文件各场景「断言帧」中定义的阶段标签。golden 集合必须覆盖断言要点中列出的每一个断言帧；actual/diff 写 `artifacts/ui/`（Git 忽略）。
- 帧对齐与比较规则见 tests/UI_CONTRACT.md §2/§3；截图固定条件见本文件 §5。

## 3. S01–S21 场景清单

| 编号 | 场景 | 场景内容与注入序列（§4 action） | 断言要点（对照 §6 / 计划验收行原文） | 对应验收行（DEVELOPMENT_PLAN） | golden | 状态 |
|---|---|---|---|---|---|---|
| S01 | idle | app_state：基准快照 B 改 threads=[]、threads_total=0、selected_thread_id=null | NOW 页；状态词 IDLE 醒目（六状态之一）；「无任务 IDLE」提示；标题栏/底栏布局（28px/24px、外边距 8px）；电压区显示 `--`；额度摘要缺失显示 `--` | §8 UI 行「六状态」；§6 NOW 行「无任务 IDLE」；P2.1「unknown 显示 --」 | S01_idle.png | ready-now |
| S02 | thinking | app_state：thread state=thinking，activity=分析类文案；advance_time(to_ms=60000) | 状态词 THINKING；活动摘要可见；无等待粗框（粗框仅等待状态）；advance_time 后运行时长按 10–30 秒节律推进 | §8 UI 行「六状态」；§6 NOW 行「六状态之一」 | S02_thinking.png、S02_thinking__f002_elapsed60s.png | ready-now |
| S03 | working | app_state：thread state=working，plan 3 步（1 completed、1 in_progress、1 pending） | 状态词 WORKING；PLAN 摘要显示完成数与当前步骤标记；当前活动摘要可见 | §8 UI 行「六状态」；§6 PLAN 行「当前步骤标记」 | S03_working.png | ready-now |
| S04 | plan_update | app_state：working+plan v1（3 步）；app_state：working+plan v2（4 步、状态推进、seq 递增） | PLAN UPDATE 后状态词仍为 WORKING（PLAN UPDATE 是内容事件，不增加业务状态）；PLAN 页步骤/完成数更新且旧计划不残留 | §8 生命周期行「PLAN UPDATE 保持 WORKING」；P2.5「PLAN UPDATE 保持 WORKING」；§3 reducer「PLAN UPDATE改变plan，通常保留WORKING」 | S04_plan_update__f001_before.png、S04_plan_update__f002_after.png | 待 P1.2 mock（与 P1.2 演示事件序列对齐后定稿） |
| S05 | needs_you | app_state：state=needs_you、attention={pending_count:1}；advance_time(to_ms=60000) | 状态词 NEEDS YOU 且 NOW 页有黑白粗框（「等待状态黑白粗框」）；等待提醒摘要可见；waiting 时长推进 | §8 UI 行「六状态」；§6 NOW 行「等待状态黑白粗框」 | S05_needs_you.png、S05_needs_you__f002_waiting60s.png | ready-now |
| S06 | done | app_state：state=done、end_reason=completed、attention=null；advance_time(to_ms=60000) | 状态词 DONE；pending 清零、粗框消失；终态后时长不再推进 | §8 UI 行「六状态」；§3 reducer「成功终态DONE…终态清理该turn pending」 | S06_done.png、S06_done__f002_frozen60s.png | ready-now |
| S07 | error | app_state：state=error、end_reason=failed、activity=超长错误文案 | 状态词 ERROR；错误文本裁剪不越界、不覆盖其他区域 | §8 UI 行「六状态」；§6 NOW 行「错误文本裁剪」 | S07_error.png | ready-now |
| S08 | cancelled | app_state：needs_you（前置帧）；app_state：state=idle、end_reason=cancelled | 取消后显示 IDLE+已取消说明；等待粗框消失；等待状态被明确的取消终态解除 | §6 NOW 行「取消显示 IDLE+已取消」；§3 end_reason 行「cancelled显示idle及取消说明」 | S08_cancelled__f001_needs_you.png、S08_cancelled__f002_cancelled.png | ready-now |
| S09 | low_battery | app_state：needs_you（业务等待中）；battery_sample 3980→3720→3700→3650→3601→3600（相邻 ≤2s）；advance_time(+30000 持续低压成立)；key(short)；key(long) | 低压页出现且优先于 NEEDS YOU（「电池优先于 NEEDS YOU」）；LOW BATTERY 页含电压、可用电量 0%、低压提示、充电/唤醒说明；key 短按无法切走强制页（「低压页面不可被普通页切换覆盖」）；长按静音不解除强制页 | §8 UI 行「低压」；P2.3「电池优先于 NEEDS YOU；低压页面不可被普通页切换覆盖」；§6 LOW BATTERY 行全文 | S09_low_battery__f001_needs_you.png、…__f002_low_warn3700.png、…__f003_critical_hold.png、…__f004_forced_page.png、…__f005_key_rejected.png | 待 P1.2 mock（正式电池 trace 对齐 P1.2「电池 trace 独立驱动 LOW BATTERY」；本行序列为骨架） |
| S10 | battery_unknown | app_state：working；battery_sample {mv:null, valid:false}；battery_sample {mv:5000, valid:true}（>4500 范围外） | 两种无效路径电压均显示 `--`（不猜 SOC、不显示编造百分比）；业务页面不受影响照常渲染 | §8 UI 行「unknown」；§7.1「范围外/驱动失败标记 unknown」；P2.1「unknown 显示 --」 | S10_battery_unknown__f001_invalid_sample.png、S10_battery_unknown__f002_out_of_range.png | ready-now |
| S11 | disconnected | app_state：working；advance_time(to_ms=150000) 期间无快照（150s 判定 disconnected）；app_state（seq+1 全量）；link(connected) | 断连提示可见（覆盖层/横幅，断连与错误有可见提示）；业务内容保留：不把任务改成 IDLE、不伪造 DONE；恢复后取全量快照并保留当前普通页面 | §1 表「断连与错误有可见提示」；P2.3「断连/陈旧覆盖、恢复顺序」；§3「上游断连不将所有任务改成IDLE」「transport失联也不伪造DONE」；§4「150秒无更新可标disconnected并重连」 | S11_disconnected__f001_online.png、…__f002_disconnected.png、…__f003_recovered.png | ready-now |
| S12 | stale | app_state：working；advance_time(to_ms=45000)（45s 无有效快照）；app_state（seq+1） | 陈旧提示出现且独立于业务状态（业务词仍 WORKING）；运行/等待计时冻结并标记最后更新；新 seq 快照后恢复 fresh | P2.3「断连/陈旧覆盖、恢复顺序」；§4「45秒未收到有效快照标link stale」「陈旧后冻结并标记最后更新」；§8 UI 行「陈旧」 | S12_stale__f001_fresh.png、…__f002_stale.png、…__f003_recovered.png | ready-now |
| S13 | multi_agents | app_state：8 线程混合六状态（含 2×needs_you、1×error、2×working、1×done、2×idle），threads_total=10、truncated=true；key(short)×2 | AGENTS 排序 needs_you→error→working/thinking→done→idle；同级按 updated_at 降序、id 升序打破平局；每页最多 4 行；显示总数与「还有 N 个」裁剪标记；按选中任务生成其他页 | §8 UI 行「多Agent」；§6 AGENTS 行排序与「最多 4 行/页」「显示总数/裁剪标记」；P2.2「空数据、多页和选中任务切换正确」 | S13_multi_agents__f001_page1.png、…__f002_page2.png、…__f003_next_main.png | ready-now（子页推进的最终行为按 P2.2 固定后验收） |
| S14 | empty_plan | app_state：working、plan total=0、steps=[]；key(short) 切到 PLAN | PLAN 页显示「暂无计划」；完成数显示 0；不渲染残留步骤 | §6 PLAN 行「无数据显示“暂无计划”」；§8 State/reducer 行「无计划/额度」；P2.2「空数据…正确」 | S14_empty_plan.png | ready-now |
| S15 | long_plan | app_state：plan total=14、steps=8、truncated=true，含 128 字节顶格步骤文本；key(short) 翻 PLAN 子页 | PLAN 分页正确（第 1/2 页内容不重叠不丢失）；截断标记可见；完成数只数 completed；超长步骤文本截断不覆盖布局 | §6 PLAN 行「只数 completed」「长计划分页」；P2.1「长文截断不覆盖」；§8 UI 行「长计划」 | S15_long_plan__f001_page1.png、…__f002_page2.png | ready-now |
| S16 | long_project_name | app_state：project 为 96 字节顶格长路径风格名称（中英混合） | NOW 标题栏项目名截断省略、单行不换行、不覆盖状态词与时长区 | P2.1「长文截断不覆盖」；§6「增加真实中文、混合英文、长路径…的测试」 | S16_long_project_name.png | ready-now |
| S17 | unicode | app_state：project/activity/attention.summary/usage.label 中文+英文混排，含全角标点与 1 个 emoji（字体无字形） | 中文渲染正确；换行/省略不切断 UTF-8 码点（截断后不出现残缺字节符）；emoji 以可见替代符显示，不静默缺字、不空白、不崩溃 | §8 UI 行「中文」；§6「项目名/摘要允许中文…换行/省略不能切断 UTF-8」「未知字符用可见替代符」 | S17_unicode.png | ready-now |
| S18 | usage_missing | app_state：usage available=false、windows=[]，context 三字段全 null；key 切到 USAGE | USAGE 页额度显示 `--`；context 显示 `--`；不编造百分比、token 总量不冒充 context | §6 USAGE 行「额度缺失 --」「token 总量不能冒充 context」；§8 UI 行「unknown」 | S18_usage_missing.png | ready-now |
| S19 | usage_0 | app_state：usage 窗口 used_percent=0.0、duration_mins=300、resets_at_ms=T0+4h | 0% 正确显示；窗口长度按数据显示实际值；reset 倒计时按 resets_at_ms 与虚拟时钟计算；窗口名来自 label 数据（不硬编码 5h/周） | §8 UI 行「0/100/unknown」；§6 USAGE 行「实际窗口长度、usedPercent、reset 倒计时」「不硬编码 5h/周」；P2.2「额度按实际窗口命名」 | S19_usage_0.png | ready-now |
| S20 | usage_100 | app_state：usage 窗口 used_percent=100.0 | 100% 显示不溢出不截断；进度指示满格；reset 倒计时正常 | §8 UI 行「0/100/unknown」；§6 USAGE 行「usedPercent」 | S20_usage_100.png | ready-now |
| S21 | bridge_restart | app_state（epoch=A，seq=5，working）；link(disconnected)；app_state（epoch=B 新 epoch，seq=0，同业务内容）；link(connected) | 新 epoch 全量快照被接受（重新握手语义）；UI 不回退到空白/IDLE 闪断页；状态最终收敛到同一业务内容 | §8 稳定性行「Bridge重启…状态最终收敛」；P3.5「Bridge 重启…UI 不回退」；§3 bridge_epoch 行「仅在认证连接/重新握手中接受变化」 | S21_bridge_restart__f001_epoch_a.png、…__f002_restarting.png、…__f003_epoch_b.png | 待 P1.2 mock（epoch/seq 重置演示与 P1.2 replay、P3.5 故障注入对齐后定稿） |

### 3.1 与 P2.5 生命周期回放的关系

P2.5「完整生命周期回放并自动断言页面/文本/时长」使用 lifecycle 场景文件（WORKING→PLAN UPDATE→NEEDS YOU→DONE 四阶段 + 独立 battery_sample 序列驱动最终低压页），逐帧断言规则见 tests/UI_CONTRACT.md §4。lifecycle 文件与本清单的关系：S03/S04/S05/S06/S09 的断言要点即生命周期各阶段 checkpoint 的断言来源；`tests/fixtures/scenarios/S_lifecycle.jsonl` 目前是**格式与契约样例**（P1.5 交付），正式 lifecycle fixtures 由 P1.2 mock 产出后对齐定稿。

## 4. 协议层 fixtures 映射（补充 F15）

协议 fixtures 的编号、场景、预期、决定层与 §8 协议行验收项的完整映射表真源在 `tests/fixtures/protocol/MANIFEST.md`（F01–F15 五列表格），此处引用不复述。P1.5 新增：

| 编号 | 文件 | 映射 |
|---|---|---|
| F15 | valid_depth12.json | 深度恰为 12 的**合法**边界包（基体=F01 最小合法包，未知附加字段 vendor_depth_probe 为 11 层嵌套数组；根计 1 故合计 12）→ §8 协议行「超大JSON/嵌套」的合法侧；§3 完整消息行「嵌套深度≤12」与「未知附加字段必须计入限制」。与 F12（深度 13 非法）构成完整边界对 |

台账差异（非本任务范围，报 A0）：`tests/fixtures/protocol/invalid_end_reason.json` 已在目录中并计入 15/15 基线与本次 16/16，但 MANIFEST 编号表（F01–F15）未给它编号，建议 A0 补号。

其余 §8 测试层的归属（不在本清单内）：State/reducer 行由 P1.1 的 reducer 单元 fixtures 覆盖；传输行由 P3.5 故障注入覆盖；Power FSM 行由 P5.1 虚拟单调时钟+电压 trace 覆盖（本清单 S09/S10 只是 UI 侧消费结果，不验证 FSM 本身）。

## 5. UI 截图契约清单（golden 生成/审批规则）

以下为全部 21 个场景的截图与像素比较固定条件，真源为计划 §8「截图实现」段与 P2.4 验收行；check_ui.py（P2.4）与模拟器截图路径必须逐条满足：

1. 固定时钟：一切时间来自虚拟单调时钟（§4 注入 at_ms / advance_time），禁用真实墙钟。
2. 固定渲染环境：LVGL 9.3.0（与设备同版本同源）、固定 DPI（无窗口缩放/HID 缩放因子）、显示驱动固定 400×300。
3. 固定字体：P0.1 锁定版本的字体文件（记录字节 hash 与字库可用字符范围；裁减字库必须记录范围，不静默缺字）。
4. 固定随机种子：渲染路径不引入未设定种子的随机性；若实现引入任何随机（布局抖动、调试覆盖层），必须显式设种子并记录。
5. 禁动画：LVGL 动画禁用或仅由虚拟时钟显式步进；无逐秒动画（§6：运行时长每 10–30 秒更新，紧急状态立即更新）。
6. 禁光标闪烁与任何周期性视觉元素。
7. 导出目标为 display framebuffer 经公共单色转换后的**逻辑 400×300 帧**（共享逻辑帧格式：1bpp、每行 50 字节、行优先、MSB=左像素、1=黑/0=白，共 15000 字节），**不是桌面窗口截图**；若用 LVGL snapshot，必须先验证它与最终 flush 输出一致并留证据（P2.4 验证项）。
8. 每次比较输出三图加数字：`actual.png`、`expected.png`、`diff.png` 与差异像素数（整数）；diff 图黑=差异、白=一致（逐像素 XOR）。
9. 语义断言与像素比较并行：文本存在、关键控件坐标不越界、页面优先级（电池强制页 > 业务页、链路提示独立）逐场景检查。
10. 退出码语义：0=全部通过；1=存在像素或语义断言失败；2=环境错误（golden 目录缺失、文件不可读）。任何被比较对象尺寸 ≠400×300 按 FAIL（退出码 1）处理。
11. 「一个像素必须失败」自检（P2.4 验收「修改一个像素能令测试失败」）：对任一 golden 翻转恰好 1 个像素构造 actual，check_ui.py 必须对该场景报 FAIL、差异像素数=1、退出码非零、三图保留在 artifacts。该自检纳入 check_ui.py 自身测试，证据留档。
12. golden 审批：golden 只能来自实际渲染；只有 A0 审核 deliberate UI 变化后人工更新 `tests/golden/` 并提交；**禁止 CI 自动接受或自动重生成 golden；禁止把测试截图自动覆盖为 golden 来消除失败**。
13. 零差异默认：固定 CI 宿主上默认像素零差异；跨系统差异先统一字体与软件渲染，**不设像素容差参数**，不用大容差放过布局问题。
14. 产物分区：golden=tests/golden/（版本控制）；actual/diff/manifest=artifacts/ui/（Git 忽略）。
