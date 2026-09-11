# ZCode Desk Terminal：后续适配开发计划

版本：v1，2026-09-11。状态：**规划完成、适配未开始**。执行顺序：先完成 `codex-desk-terminal`，再从其验收基线启动本计划。本次不修改已冻结协议、现有固件或 ZCode 配置。

## 1. 目标与实施方式

在相同 ESP32-S3-RLCD-4.2 上显示智谱 ZCode 的任务状态、需要用户处理的事项、计划和可获取的用量。沿用 NOW / AGENTS / PLAN / USAGE / LOW BATTERY、400×300 LVGL + SDL、BLE/Wi-Fi 和本地电池保护。

**推荐在现有仓库增加 ZCode source adapter 与小型采集插件，复用终端平台。** 软件显示品牌可叫 `ZCode Desk Terminal`，实现无需复制一个独立固件仓库。Codex 完成后创建独立适配分支/worktree；不提前重命名仓库或批量改 `cdt_` 符号。

```text
ZCode 桌面软件
  └─ 官方 Hooks / 经验证的只读状态源
          ↓
     本地采集器：只观察，脱敏、限时、限量
          ↓
     ZCode adapter → 现有 State Engine
                         ↓
                  统一 AppState
                         ↓
            现有 BLE / Wi-Fi Transport
                         ↓
              现有 State / Presenter / LVGL
                         ↓
                 SDL 或 ST7305

设备 ADC / 电源状态机 → 本地 LOW BATTERY（始终独立）
```

首版每次选择一个 source：Codex 或 ZCode；允许软件重启后切换，不要求同时聚合两款软件。AGENTS 先表示被观察到的 ZCode 会话；只有拿到可靠父子关系和生命周期，才显示真实子 Agent。更换模型为 GLM 不等于接入 ZCode 软件，不以智谱推理 API 代替任务状态接口。

## 2. 已知事实、未验证项和接入选择

### 2.1 官方资料核对

官方 Hooks 列出 SessionStart、UserPromptSubmit、PreToolUse、PermissionRequest、PostToolUse、PostToolUseFailure、Stop。Stop 是结束前检查点，可能继续；PermissionRequest 可被其他 Hook 改变决定。项目级 Hook 当前被忽略，应使用启用的插件或用户级配置，并在新会话验证。`process` 同步执行，`command` 支持异步。[ZCode Hooks](https://zcode.z.ai/en/docs/hooks)

插件可使用 `.zcode-plugin/plugin.json` 与 `hooks/hooks.json`。Hook 输入包含会话和工具标识；`transcript_path` 是本次调用临时文件，不能作为长期观察路径。纯观察 Hook 应退出0、stdout为空，不返回审批、阻止或上下文修改结果。[官方插件开发指南](https://github.com/zai-org/zcode-plugins/blob/main/docs/PLUGIN_DEVELOPMENT_CN.md)

ZCode 用量页区分本地会话统计与远端 Coding Plan 额度。页面能显示统计，不等于已经提供第三方稳定读取 API；本计划未确认可调用的额度接口。[Usage Stats](https://zcode.z.ai/en/docs/usage-stats)

以上是在线文档核对，不是本机运行验证。Z0 必须记录用户实际 ZCode 版本，复核该版本契约。文档内部若出现设置入口/作用域说明冲突，以能力测试和明确的执行限制为准。

### 2.2 接入优先级

| 路径 | 用途 | 接受条件 |
|---|---|---|
| 官方 Hooks | 首选实时事件采集 | 本机新会话确实触发；失败不干扰用户工作；身份与顺序可关联 |
| 官方稳定只读 API/导出 | 补充最终状态、计划、额度 | Z0 找到文档和可复现样本；本次未假定存在 |
| 本地记录/日志只读适配 | 仅填补必要能力缺口 | 路径/格式/版本可核验，最小读取、脱敏、有故障测试；单独标明依赖版本 |
| 临时 transcript | Hook 当次提取必要的结构化字段 | 在有效生命周期内同步提取；不保存整个对话，不交给异步任务稍后打开 |
| OCR、解析整个应用画面、抓私有网络请求 | 不作为首版主链路 | 若正规路径不足，明确记录阻塞，不用猜测数据冒充完整接入 |

采集目标是用户正常使用的 ZCode 桌面任务。仅在独立测试工具里伪造事件通过，不算完成桌面适配。新会话才开始采集时，UI说明“仅显示启用后观察到的任务”，不声称列出了全部历史任务。

### 2.3 能力矩阵（Z0 填入实证）

每项记录 supported / partial / unavailable / unverified、版本、来源、样本、时间精度和限制。

| 能力 | 当前候选 | 必须回答的问题 | 缺失时行为 |
|---|---|---|---|
| 工作开始/活动 | prompt/tool Hooks | 能否区分被其他规则拒绝的提交？是否有稳定turn ID？ | 明确“收到请求/活动已观察”，不用假精确阶段 |
| 等待用户 | PermissionRequest | Hook运行后真的显示对话框吗？批准/拒绝如何解除？ | 仅显示“可能需要处理”，不得伪装已确认NEEDS YOU |
| 最终完成 | Stop + 最终状态源 | 其他Stop Hook要求继续时怎样确认？ | “结束待确认”，不能单凭Stop宣布DONE |
| 失败/取消 | 工具失败/最终状态源 | 能否区分可恢复工具错误与整轮失败？ | 保持活动或未知，工具失败不直接等于ERROR |
| 计划 | 已成功执行的结构化计划更新/只读源 | 工具名、步骤格式、更新成功与任务归属？ | “计划暂不可读取”；不能从工具次数造进度 |
| 多会话 | session_id + 会话活动 | 恢复/压缩是否换ID？窗口/工作区如何区分？ | 仅列观察到的会话 |
| 子Agent | 经核验的父子ID/事件 | Agent工具调用是否足以证明子Agent活跃/完成？ | 不增加虚构子Agent行 |
| 上下文 | 显式context统计 | 数值是累计token还是当前上下文？ | null/-- |
| 套餐额度 | 官方可读统计源 | 账号、地区、窗口、单位、reset含义？ | available=false，显示不可读取 |
| 存活/恢复 | collector状态 + 会话重同步 | Bridge健康是否意味着ZCode当前任务仍被观测？ | 两者分开，丢事件标数据不完整 |

**Go/No-Go：** 工作活动可用但等待/终态不可靠时，可以完成“受限预览”，不得标为完整适配。完整首版要求真实桌面任务的 WORKING、已确认 NEEDS YOU、明确 DONE 可验证；PLAN/USAGE 可按能力明确不可用，不编造数值。用户若以后要求计划/额度必须真实可得，则提升为对应发布门槛。

## 3. 继承哪些成果，新增哪些工作

| 模块 | 处理 |
|---|---|
| ESP-IDF、LVGL9.3.0、SDL2、字体与构建 | 使用 Codex 最终验收版本及锁文件；不趁适配升级工具链 |
| ST7305、GPIO4校准、KEY、Light/Deep Sleep | 直接复用；电量0%=3.600V、持续低压策略不变 |
| BLE/WSS分包、认证、重连 | 复用；只测新快照大小和事件频率带来的影响 |
| Shared State/Presenter/UI | 增加来源品牌与必要能力展示，保留页面布局 |
| Bridge/reducer | 复用通用状态；新增ZCode mapper，不复制Codex JSON-RPC客户端 |
| 协议 | 针对现有source枚举/品牌/数据可信度做有边界的兼容升级 |
| 测试 | 继承Codex基线；新增ZCode样本、故障回放、截图与双来源隔离 |

2026-09-11 本机读取到的参考HEAD为 `75de01c`；这里只记录调研锚点，工作树有持续开发，不是未来开工基线。读取到的 README/STATUS 仍列有真机传输和功耗未验证项；本次没有复跑或审定这些验收。Z0须重新读取完成后的事实，不能从该短hash直接开始并宣布平台已完成。

当前可核实的接口障碍：`protocol/state.schema.json`、`bridge/state/engine.py`、`shared/state/cdt_parser.c` 均只接受 mock/codex_bridge_owned/codex_desktop_observed；直接写入zcode会被拒绝。既有协议已经冻结，因此不采用“只换一个Python文件就能兼容所有旧固件”的承诺。

## 4. 协议与事件契约

### 4.1 AppState 升级建议

Z0重读最终协议后确定版本号。若届时仍为冻结v1，建议新增v2：新固件同时读旧v1及新v2；Codex默认仍输出v1，ZCode输出v2。旧固件拒绝v2是预期保护行为；Bridge在配置检查阶段阻止不匹配组合。不要将ZCode伪标为codex来源或mock以绕过校验。

推荐v2最小变化：source新增明确的ZCode来源；新增有限provider标识供标题栏显示；需要时增加可选的数据质量/能力字段，以区分empty、unavailable、stale和unconfirmed。具体字段由Z0冻结，**本文件不改动当前真源**。

| 拟新增语义 | 建议表达 | 兼容要求 |
|---|---|---|
| 来源软件 | provider=codex/zcode | 本地枚举映射显示CODEX/ZCODE，不发送任意UI样式 |
| 观察路径 | zcode_hooks_observed；必要时zcode_records_observed | 实际启用的源必须如实标记，版本/诊断留Bridge |
| 不确定运行态 | 新版支持unknown，或等价的明确未知展示 | 不把不知道写为idle；Z0同时核对C类型/排序/六状态页面 |
| 等待/终态质量 | confirmed/unconfirmed/stale | 未确认不套用醒目的已确认提醒，不用超时推断最终成功 |
| plan/usage能力 | available/unavailable/unknown，来源更新时刻 | 空计划与读不到计划区分；没有额度不画0% |

优先复用最终平台已存在的通用字段；只有不能正确表达时才新增。不得为将来十种厂商设计动态插件框架。保持16KiB消息上限、8会话/8步骤/4额度窗口、UTF-8字节限制和原子替换规则，除非真实基线已有经审定变更。

### 4.2 ZCode 事件归一化

Hook原始协议→采集器私有envelope→ZCode mapper→现有 `bridge/events.py` 的通用事件→State Engine。私有envelope不是设备协议。

最小元数据：collector_version、event_id、session_id、可得的turn_id/tool_use_id、原始hook名称、采集时间、经脱敏的必要payload。没有turn_id时可在每次确认的新用户提交建立本地generation；恢复/并发/连续输入关联不明确时标未知，不能任意归入上一轮。

| 观察事件 | 推荐处理 | 必测反例 |
|---|---|---|
| SessionStart | 建立或恢复session；依据source区别压缩/清空/启动 | compact不重复生成新Agent，不意外清空活跃等待 |
| UserPromptSubmit | 创建候选generation，等待实际运行证据 | 后续Hook阻止提交，不能永久显示WORKING |
| PreToolUse | 更新工具活动摘要，保留工具关联ID | 请求执行不等于执行成功；不能提前应用计划修改 |
| PermissionRequest | 建立未确认attention候选 | 其他Hook自动批准/拒绝，未出现人工等待 |
| 实证等待/解除 | 进入NEEDS YOU；按明确请求关联解除 | 并行另一个工具成功不能清掉所有pending |
| PostToolUse | 更新活动；确认成功的结构化计划更新才能应用 | 不能把本次成功当整个任务完成 |
| PostToolUseFailure | 显示工具失败摘要，等待后续恢复/终态 | 失败后重试成功；取消不同于普通失败 |
| Stop | 记录完成候选，不直接DONE | 其他Stop Hook继续、模型继续工具调用 |
| 经验证的最终状态 | 明确成功DONE、失败ERROR、取消按原契约 | 迟到Stop/工具事件不能复活旧generation |

无可靠结束证据时停留unknown/unconfirmed并显示原因。任何“静默N秒=完成”的推断都不能进入confirmed。提示词内容、工具输出中的自然语言只作为摘要数据，不能成为程序指令或协议事件。

### 4.3 采集器与可靠性

首选轻量同步process Hook：仅在本机将**裁剪脱敏后的单个小事件**原子落入插件数据目录，然后立即退出；Bridge异步消费。初始预算单事件≤8KiB、队列≤1000项/8MiB、保留≤24h、Hook本地写入p95目标≤50ms、硬超时目标≤500ms（含进程启动需实测）。工具原始stdin另设有界读取与类型检查，超限记录drop，不能为读完巨大输出无限分配。

选择“每事件临时文件→同目录原子rename”，避免并发写同一JSONL交错。消费者只读完成文件；成功接收后删除，使用有界去重索引处理崩溃重复。并发Hook的文件名/接收先后不代表语义顺序：优先上游ID/序号；若无稳定顺序，不假装精确排序，重同步或标数据不完整。

队列满、磁盘满、权限错误、Bridge停止：Hook正常退出且不改变ZCode行为，记录有界错误计数；恢复后页面标事件可能丢失，不能复播旧WORKING成当前事实。只有可信快照/新明确生命周期证据才能解除不完整标记。Bridge维护source健康与无线连接两种状态；仅有心跳不能替未知终态背书。

Hook不直接操作BLE/Wi-Fi，不携带智谱API key，不读取完整用户配置。只保留session/工具标识及必要摘要；项目路径可转换为本地basename或匿名映射。目录权限限制当前用户，输入不能决定任意输出路径，stderr不打印原始prompt/密钥。临时transcript若必要，仅在Hook当次限量提取；默认不读。

若实测进程启动成本不可接受，再评估官方支持的异步command路径；保留相同落盘和去重语义，不使用失效临时文件，不把“大量后台进程”当作省时优化。

## 5. 页面适配

| 页面 | 改动与验收 |
|---|---|
| NOW | 标题ZCODE，当前项目/活动/等待/时长；显示未确认或陈旧状态，不把产品名换成GLM模型名 |
| AGENTS | 观察到的session列表；多个工作区不串线；子Agent关系未知时按会话显示，不伪造数量 |
| PLAN | 已证实的步骤、完成数；读取不到明确提示；空计划另用空态 |
| USAGE | 区分本地token、当前context与套餐配额；prompt次数/MCP次数不能冒充token或context百分比 |
| LOW BATTERY | 沿用电压、0%与休眠说明；任何ZCode事件都不能覆盖本地critical |

配额按实际源提供的单位和窗口显示，不硬编码Codex套餐。没有可信reset时间则显示--；缺分母不换算百分比。模型标签可显示GLM，但来源品牌仍为ZCODE；Z.ai/BigModel账号差异留在host配置，设备不持有认证信息。首版不执行额度重置或审批动作。

## 6. 阶段与任务拆分

### Z0：基线交接与能力验证（预计1–2人日，不含外部等待）

| ID / Owner | 任务 | 依赖 | 完成标准 |
|---|---|---|---|
| Z0.1 / A0 | 确认Codex完成基线、版本、未决项；建适配worktree和任务台账 | 用户指定的Codex完成节点 | 有可回退tag/commit、工作树变更已妥善处理；不覆盖主线在途修改 |
| Z0.2 / A1 | 锁本机ZCode版本；受控桌面任务采集七种候选Hook及异常样本 | Z0.1 | 有脱敏原始证据，明确新/旧会话覆盖边界 |
| Z0.3 / A1 | 验证等待解除、Stop续跑、计划、额度与子Agent能力 | Z0.2 | 能力矩阵完整；未知不写supported；决定完整/受限/阻塞 |
| Z0.4 / A0 | 冻结最小协议增量、质量字段、版本兼容和私有event envelope | Z0.3 | schema、Python/C类型、迁移测试设计一致 |

Z0没有可靠等待/终态源时，A1继续解决能力缺口；A2/A3只做可独立的Mock和兼容测试，发布保持受限或阻塞。

### Z1：最小采集与 Mock 全链路（预计1–2人日）

| ID / Owner | 任务 | 依赖 | 完成标准 |
|---|---|---|---|
| Z1.1 / A1 | 观察插件、事件落盘、超时/容量/脱敏、消费去重 | Z0.2/Z0.4 | Bridge关闭/磁盘故障不影响ZCode提交、权限或结果 |
| Z1.2 / A1 | ZCode mapper复用现有事件/reducer | Z0.4 | 重复/乱序/恢复/多pending/generation测试通过 |
| Z1.3 / A2 | 兼容parser、provider显示、unknown/能力空态 | Z0.4 | 新固件仍读Codex v1；ZCode新版本跨语言校验通过 |
| Z1.4 / A3 | Mock WORKING→PLAN UPDATE→NEEDS YOU→DONE→LOW BATTERY | Z1.2/Z1.3 | SDL完整回放；低电量由独立本地电压trace驱动 |

### Z2：真实桌面任务与页面回归（预计2–3人日）

| ID / Owner | 任务 | 依赖 | 完成标准 |
|---|---|---|---|
| Z2.1 / A1 | 安装启用观察插件；真实桌面新会话验证 | Z1.1/Z1.2 | 事件无需用户改用另一套CLI；卸载/禁用可恢复 |
| Z2.2 / A1 | 实证等待/结束补充源、计划/额度可用部分接入 | Z0.3/Z2.1 | 其他Hook干预和取消正确；不可用字段明确降级 |
| Z2.3 / A2 | 400×300 ZCode五页与中文摘要优化 | Z1.3 | 真实事件和Mock都正确渲染，长文不溢出 |
| Z2.4 / A3 | 新golden和Codex全量回归；快照语义检查 | Z2.2/Z2.3 | Codex非预期像素变化为0；ZCode差异经审核 |

### Z3：真机与可靠性（预计1–2人日，另加24h观察）

| ID / Owner | 任务 | 依赖 | 完成标准 |
|---|---|---|---|
| Z3.1 / A2 | BLE/Wi-Fi分别真实桌面→板卡验证 | Z2 Gate、平台真机基线 | 两链路显示同一业务状态；断连恢复不显示旧完成 |
| Z3.2 / A3 | 主机睡眠/唤醒、ZCode/Bridge重启、队列丢失、100次重连 | Z3.1 | 无崩溃、错误串线或无界增长；丢失可见 |
| Z3.3 / A2+A3 | 相同事件频率功耗抽测、低压优先权 | Z3.1 | ≤3.6V策略仍成立；事件洪峰不阻塞采样/休眠 |
| Z3.4 / A3 | 24h稳定性与Hook开销测量 | Z3.2 | 有CPU/内存/磁盘/延迟记录；正常ZCode功能未被改变 |

沿用Codex已经完成的BLE/Wi-Fi功耗结论，但若本次改变刷新/心跳、包大小、重连或无线配置，重跑对应A/B场景。无需仅为换来源重做全部ST7305驱动。

### Z4：交付与回退（预计0.5–1人日）

| ID / Owner | 任务 | 依赖 | 完成标准 |
|---|---|---|---|
| Z4.1 / A0 | 锁ZCode支持版本、平台基线、插件/Bridge/固件矩阵 | Z3 | 新Agent按文档可复现，不自动升级软件版本 |
| Z4.2 / A0 | 安装/新会话启用/运行/卸载/回退指南 | Z4.1 | 不覆盖用户其他Hooks；禁用插件无遗留进程 |
| Z4.3 / A0 | 对照能力矩阵验收并归档 | Z4.2 | 明确完整/受限结果；任何未知不勾全完成 |

关键路径：Codex完成 → Z0.1 → Z0.2/3 → Z0.4 → Z1 → Z2 → Z3 → Z4。合计约7–10人日的粗略工作量，协议缺口或只读接口不可用会增加不确定等待；并行不减少真机/24h观察本身所需时间。

## 7. Agent 分工与共享文件边界

| 角色 | 可独立推进 | 写入范围（建议） | 合并边界 |
|---|---|---|---|
| A0 集成负责人 | 基线、能力裁决、协议冻结、版本/发布 | docs、protocol、公共类型和顶层构建的最终合并 | 唯一批准协议/golden变更 |
| A1 ZCode接入 | 插件、collector、mapper、真实能力验证 | integrations/zcode、bridge/sources/zcode.py、对应tests | 不改LCD/电池/共享页面 |
| A2 平台适配 | parser、Presenter、品牌/空态、真机集成 | shared/state、shared/presenter、shared/ui | 依据A0冻结协议实现，不自行加字段 |
| A3 测试 | 样本设计、故障回放、截图、性能/回归 | tests/zcode、tests/golden/zcode、证据目录 | 不改采集逻辑来迎合测试、不覆盖Codex golden |

并行起点：Z0.4后A1采集/mapper与A2协议/UI并行，A3先写测试场景。共享一块板时预约窗口，不并发烧录。A1新增通用事件若必须改bridge/events.py或reducer，由A0协调单owner修改；既有Codex adapter保留原行为。

每个任务交付编号、修改路径、可执行命令、实测结果、脱敏证据和限制。后续正式执行时写入现有唯一STATUS台账的Z系列任务，不在这份计划里伪造进度。

## 8. 必测场景与指标

| 场景 | 断言 |
|---|---|
| 正常活动与结束 | 已确认的终态才DONE；活动文本与真实任务一致 |
| PermissionRequest被自动处理 | 不残留NEEDS YOU；未弹窗不标确认等待 |
| 两个pending交错 | 只解除对应请求，未关联成功事件不能清空等待 |
| Stop后继续至少两轮 | 无提前DONE和错误提醒；终态来自明确信号 |
| 提交被其他Hook拒绝 | 不保持虚假运行；监控Hook自身不改决策 |
| 工具失败后恢复/取消 | 可恢复失败不同于整轮ERROR；取消不显示成功 |
| 压缩/恢复/多窗口 | session映射稳定，不重复计Agent或把旧plan放进新turn |
| 无计划/无额度/无context | 分别显示不可读取或--，不是空白进度/0% |
| Hooks禁用/旧会话未启用 | 显示未采集/覆盖限制，不把Bridge在线当ZCode在线 |
| 重复、乱序、丢文件、磁盘满 | 状态不回退；丢失标不完整；ZCode正常继续 |
| 大输入与恶意路径 | 限量、拒绝越界；不可通过payload写任意文件或执行命令 |
| Codex→ZCode→Codex切换 | 停旧source、换epoch、清旧任务/额度/队列、重新鉴权/同步 |
| 旧固件/新Bridge组合 | ZCode不冒充Codex来源；配置错误明确；Codex旧模式可回退 |
| 本地低压 | 远端WORKING/NEEDS YOU洪峰仍被LOW BATTERY抢占并休眠 |

性能目标初值：正常本地Hook写入p95≤50ms；已确认上游状态到SDL≤500ms、到已连接真机p95≤2s；不含额外确认源的轮询等待。报告必须单列“事件确认耗时”和“显示链路耗时”，不能只测后半段。采集器不高频扫描全目录；队列有界；低频存活快照沿用平台最终设置。

测试命令复用基线scripts，新增最少的ZCode测试入口。Z1交付时填写真实命令；本计划不声称尚未创建的命令现在可运行。首次ZCode golden由A0审阅，后续CI只比较；不把修改品牌造成的预期差异混入Codex基线覆盖。

## 9. 推荐目录增量

```text
codex-desk-terminal/
  docs/ZCODE_ADAPTATION_PLAN.md        # 本次交付
  docs/ZCODE_CAPABILITIES.md           # Z0实际证据与限制
  docs/ZCODE_SETUP.md                  # Z4安装/配对/卸载/回退
  integrations/zcode/
    .zcode-plugin/plugin.json          # 后续创建，最小观察插件
    hooks/hooks.json
    hooks/collect.py                   # 若沿用Python最方便；语言以本机运行时为准
  bridge/sources/zcode.py              # 消费器+mapper，初期不拆多层框架
  tests/zcode/                        # 原始事件→期望State、可靠性检查
  tests/fixtures/zcode/                # 仅脱敏最小样本
  tests/golden/zcode/                  # 品牌和缺失能力页面
```

具体schema/头文件按现有命名增量扩展；不复制vendor、firmware、Transport或整个golden目录。当前仅创建计划文件，其他目录均为实施建议。

## 10. 交付、回退与下一步

最终交付：可安装的本地观察插件、ZCode source adapter、支持来源显示的终端版本、完整能力矩阵、Codex/ZCode双回归、两种真机传输证据、安装卸载指南。仅本地使用不需要发布插件市场或GitHub。

安装过程先展示将新增的Hook条目/插件，保留用户既有配置；优先独立插件开关。回退时停ZCode source、禁用插件、切Codex source与匹配协议，必要时恢复已验收固件；只清理本插件数据，不删除用户全局.zcode目录。升级后重新开会话验证Hooks，不假设现有任务自动生效。

Codex完成后的首个任务书：

> 阅读本计划、当前AGENTS、INTERFACES、VERSIONS和STATUS；确认Codex验收基线后执行Z0.1–Z0.3。核验本机ZCode版本和真实Hooks，重点证明等待/解除与最终完成的语义，输出ZCODE_CAPABILITIES.md。不要提前改协议、复制固件或自动批准操作。证据齐全后由A0冻结Z0.4，才派发并行实现。

**本计划的核心验收原则：同一套终端平台，真实反映ZCode的可观察状态；缺失能力公开显示，不能用推断出来的精确状态替代事实。**
