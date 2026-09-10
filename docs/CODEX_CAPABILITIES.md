# Codex app-server 本机能力矩阵（P0.2）

任务：P0.2（角色 A1 Bridge/State）。探针日期：2026-09-10。
对象：本机 codex CLI 0.152.0（`/Users/cui/.local/lib/node-v24.19.0-darwin-arm64/bin/codex`），macOS 15.7.9 arm64。
方法：对 app-server 的 stdio JSON-RPC 接口做只读能力探针；唯一的写操作是在隔离临时目录中创建的一次性 ephemeral 会话（见矩阵第 7 行）。所有样本已脱敏（凭证/邮箱 → `<redacted>`，用户内容 → `<content len=N>`，home 路径 → `~/`）。可复现脚本见 `scripts/probe/README.md`，原始证据在 `artifacts/probe/`（不入库）。

## 结论速览

- **桌面正在运行的任务不能被第三方进程观察**。桌面（ChatGPT.app 内置 Codex Framework 152.0.7977.83）的 app-server 是它自己的私有 stdio 子进程，不监听任何本机 socket；本机也没有运行共享 daemon（`app-server-control.sock` 不存在）。旁听通道不存在。
- 但**持久化历史跨进程可读**：第三方进程自起一个 app-server 实例后，`thread/list` 能只读枚举桌面/VSCode 创建的历史线程（`source: "vscode"`）。运行时状态（加载/运行中/等待审批）不在其中——其他实例的线程一律显示 `notLoaded`。
- **额度可以读到**：`account/rateLimits/read` 返回 5h/周双窗口 usedPercent、窗口分钟数、resetsAt、credits、planType；且有 `account/rateLimits/updated` 推送。
- **审批等待可以只读观察**（限自己启动的会话）：审批请求本身是 server→client 的 JSON-RPC 请求（`item/commandExecution/requestApproval`），可以只接收不回应；等待期间 `thread/status/changed` 推送 `activeFlags: ["waitingOnApproval"]`。
- `codex app-server` 与桌面是**两个版本**（本机 CLI 0.152.0，桌面侧线程由 0.153.4 创建）；账户默认模型 `gpt-6-astra` 拒绝 0.152.0 发起的 turn（400 "requires a newer version of Codex"），adapter 必须显式固定模型。

## 探测矩阵

结论分类：**desktop-observed**（能旁听正在运行的桌面任务）/ **bridge-owned**（只能观察探针自己启动的受控会话）/ **unsupported** / **未测**。

| # | 能力 | 结论 | 关键事实 | 证据 |
|---|---|---|---|---|
| 1 | 协议 Schema 生成 | 可用（非敏感，可入库） | `codex app-server generate-json-schema --out DIR --experimental` 一次生成两份合并 schema：v1 面 91 个定义、v2 面 734 个定义，共 414 个文件 | `docs/proto-samples/schema-codex-0.152.0/`，`artifacts/probe/01-generate-schema.json` |
| 2 | initialize 握手 | 可用（bridge-owned） | stdio 上按行分隔 JSON-RPC；方法名 `initialize`（schema 核实）；参数 `clientInfo{name,title,version}` + `capabilities.experimentalApi:true`；响应 `{userAgent, codexHome, platformFamily, platformOs}`，userAgent 内嵌 `0.152.0`。协议没有独立版本号字段 | `artifacts/probe/02-initialize.json`，`docs/proto-samples/requests/initialize.request.json`、`responses/initialize.response.json` |
| 3a | 读取历史线程列表（持久化） | **desktop-observed（仅历史，非运行时）** | 新 stdio 实例调用 `thread/list`：返回用户历史线程（含 `source: "vscode"`、`cliVersion: 0.153.4` 的桌面会话），字段含 id/cwd/path/时间/preview；全部 `status.type: "notLoaded"`。纯读，不触碰线程 | `artifacts/probe/03-thread-list.json`，`responses/thread.list.response.json` |
| 3b | 读取运行中线程状态 | **unsupported（跨实例）** | `thread/loaded/list` 只返回**本实例**内存中已加载线程的 id（字符串数组）；其他实例（桌面）的线程对 `thread/list` 显示 `notLoaded`，无法读到运行状态 | 同上 + `artifacts/probe/05-notifications-stdio.json` |
| 4 | 读取额度 | 可用（desktop 级数据，任何实例可读） | `account/rateLimits/read`（params `null`）→ `{rateLimits{primary{usedPercent,windowDurationMins,resetsAt}, secondary{...}, credits, planType}, rateLimitsByLimitId, rateLimitResetCredits}`；实测 primary 24%/300min、secondary 22%/10080min、planType "plus"。`account/read`（`refreshToken:false`）→ planType（email 已脱敏）。USAGE 页所需字段齐备 | `artifacts/probe/04-rate-limits.json`，`responses/account.rateLimits.read.response.json` |
| 5 | 实时事件订阅 | **bridge-owned**（全局事件除外） | 自有会话收到完整生命周期推送：`thread/started → thread/status/changed → turn/started → hook/started/completed → item/started → item/completed → item/agentMessage/delta → thread/tokenUsage/updated → account/rateLimits/updated → turn/completed`。全局通知跨会话到达（`remoteControl/status/changed`、`account/rateLimits/updated`）。**桌面线程的任何 turn/item/thread 事件均不可达**：自有实例被动监听 30s，桌面在运行，桌面事件 0 条 | `artifacts/probe/05-notifications-stdio.json`、`07-bridge-owned-session.json`，`docs/proto-samples/events/` |
| 6 | 审批等待只读可观察 | **bridge-owned 可；desktop unsupported** | 审批以 server→client **请求**出现（`item/commandExecution/requestApproval`，含 command/cwd/availableDecisions），客户端可只接收不回应；等待期间 `thread/status/changed` 推送 `{type:"active", activeFlags:["waitingOnApproval"]}`；对自身 turn 调 `turn/interrupt`（不 resolve 审批）后转 `idle` 并收到 `serverRequest/resolved`（未回应请求被服务端解除）。注意：ephemeral 线程不出现在 `thread/list`，等待标志只能从通知读 | `artifacts/probe/06b-approval-observable.json`，`requests/server.item.commandExecution.requestApproval.unanswered.json`、`events/thread.status.changed.waitingOnApproval.json` |
| 7 | bridge-owned 受控会话全链路 | 可用 | 隔离临时 cwd + `thread/start{ephemeral:true, approvalPolicy:"never", sandboxPolicy:{type:"readOnly",networkAccess:false}, model:"gpt-5.6-sol"}` + `turn/start`"只回复 ok"：6.3s 完成，回复 "ok"，事件流完整。**坑**：不固定模型时账户默认 `gpt-6-astra` 被 0.152.0 拒绝（400）。ephemeral 线程不进用户历史 | `artifacts/probe/07-bridge-owned-session.json`，`requests/thread.start.request.json`、`responses/turn.start.response.json` |
| 8 | daemon / proxy 共享总线 | **unsupported（本机现状）** | 0.152.0 无 `daemon status` 子命令（有 start/restart/stop/version/bootstrap）；`daemon version` exit 1：`~/.codex/app-server-control/app-server-control.sock` 不存在 → 无 daemon。桌面进程命令行证实其 app-server 是私有 stdio 子进程（未加 `--listen`）。`codex app-server proxy` 无处可连（initialize 超时）。探针遵守红线未启动 daemon | `artifacts/probe/08-daemon-proxy.json`、`05-notifications-proxy.json`、`06-pending-approval-proxy.json` |
| 9 | thread/read、thread/items/list、thread/turns/list、thread/timeline/list、account/usage/read、turn/plan/updated、item/plan/delta、item/reasoning/*、turn/diff/updated | **未测**（方法名已在 schema 核实存在，留给 P3.1） | 历史明细与计划事件需要在 P3.1 adapter 中按 schema 逐个验证；`turn/plan/updated` 在本次最小提示下未触发（plan item 由模型 update_plan 工具产生） | `docs/proto-samples/schema-codex-0.152.0/methods-codex-0.152.0.json` |

## 本机版本与协议版本

- codex CLI：`codex-cli 0.152.0`（探针与 schema 生成均用它）。
- 协议版本：app-server **没有独立的协议版本号字段**。initialize 响应的 `userAgent` 内嵌核心版本（实测 `…/0.152.0 (Mac OS 15.7.9; arm64)…`）；协议面分 v1（`CodexAppServerProtocol`，91 定义）与 v2（`CodexAppServerProtocolV2`，734 定义）两套 schema；本文所有方法名取自 v2 面。声明 `capabilities.experimentalApi: true` 可选入实验方法。
- 版本漂移事实：本机桌面的 Codex 线程由 **0.153.4** 创建（ChatGPT.app 内置 Codex Framework 152.0.7977.83）。0.152.0 的 CLI 已经无法使用账户当前默认模型 `gpt-6-astra`。真实集成前需 A0 决策版本锁（升级 CLI 会改变 schema 基线）。

## 事件命名清单与 INTERFACES.md §3 对照

INTERFACES.md §3 要求 adapter 核验的候选事件，与 0.152.0 实测/schema 对照：

| INTERFACES §3 清单 | 0.152.0 实况 | 本次证据 |
|---|---|---|
| turn/started | 存在，已实测 | `events/turn.started.json` |
| turn/completed | 存在，已实测（含 status/items/durationMs） | `events/turn.completed.json` |
| item/started | 存在，已实测（userMessage/agentMessage 等） | `events/item.started.agentMessage.json` |
| item/completed | 存在，已实测 | `events/item.completed.agentMessage.json` |
| turn/plan/updated | schema 存在；本次未观察到（最小提示未产生 plan item）→ P3.1 复验 | — |
| thread/tokenUsage/updated | 存在，已实测；`tokenUsage.total{inputTokens,cachedInputTokens,outputTokens,totalTokens}` + `modelContextWindow`（258400）→ CONTEXT 字段可用，但 totalTokens 含系统指令开销 | `events/thread.tokenUsage.updated.json` |
| account/rateLimits/read | 存在，已实测 | `responses/account.rateLimits.read.response.json` |
| account/rateLimits/updated | 存在，已实测（turn 消耗后推送，全局） | `events/account.rateLimits.updated.json` |
| 审批请求 | `item/commandExecution/requestApproval`（另有 `item/fileChange/requestApproval`、`item/permissions/requestApproval`、`item/tool/requestUserInput`、`mcpServer/elicitation/request`；legacy `execCommandApproval`/`applyPatchApproval`）— commandExecution 已实测 | `requests/server.item.commandExecution.requestApproval.unanswered.json` |
| 审批解除通知 | `serverRequest/resolved` 已实测（turn 被 interrupt 后未回应请求自动解除） | `events/serverRequest.resolved.json` |
| 用户输入等待 | `ThreadActiveFlag: "waitingOnUserInput"`（schema）；`item/tool/requestUserInput` 未测 | schema |
| 线程级状态推送 | `thread/status/changed`（active/idle/notLoaded/systemError + activeFlags）已实测 | `events/thread.status.changed.waitingOnApproval.json` |
| 计划增量 | `item/plan/delta`（schema 存在，未测） | — |
| reasoning/diff | `item/reasoning/summaryTextDelta` 等、`turn/diff/updated`（schema 存在，未测） | — |

全部 0.152.0 方法名见 `schema-codex-0.152.0/methods-codex-0.152.0.json`（client 请求 154 个、server 通知 81 个、server→client 请求 11 个）。

## 桌面旁听为什么不可行（证据链）

1. 桌面进程树（pgrep 实测）：ChatGPT.app 以 `…/Resources/codex -c features.code_mode_host=true app-server --analytics-default-enabled …` 启动**私有子进程**，无 `--listen` 参数 → 默认 `stdio://`，管道对端是桌面应用自身，第三方无法接入。
2. 无共享 daemon：`codex app-server daemon version` 失败（control socket 不存在）。0.152.0 的 daemon 也不是桌面自动启动的。
3. 行为佐证：自有实例被动监听 30 秒（桌面在运行），未收到任何桌面线程事件；自有实例 `thread/list` 中桌面线程一律 `notLoaded`。
4. 第三方能读到的只有**落盘历史**（`~/.codex/sessions/...`，经 `thread/list` 只读枚举）。

## 限制与下一步建议

1. **P3.6（当前桌面任务端到端验收）保持 blocked**，如计划 §P0 Gate 所述；Mock/受控会话链路照常推进，不得把 bridge-owned 标成桌面旁听（`source.kind` 用 `codex_bridge_owned`）。
2. 可行的真实数据路径（供 A0 排期决策）：
   - a. 让桌面走共享模式后重测：若桌面或用户显式启用 `codex app-server daemon start`（或桌面加 `--listen unix://`），`codex app-server proxy` 即可只读接入同一实例；届时重跑 `probe_05 --proxy`、`probe_06 --proxy`、`probe_08`。本探针未擅自启动 daemon。
   - b. 只读历史轮询：`thread/list` 的 `updatedAt/recencyAt` 可推断"桌面最近有活动"，但拿不到运行时状态/审批等待，只能作为 AGENTS 页的弱信号，需在 UI 上明示数据源为历史。
   - c. bridge-owned 会话作为 v1 交付核心：事件流、额度、tokenUsage、审批等待全部可用且已脱敏验证。
3. 版本锁：与 A0 确认 CLI 是否升至桌面同版（0.153.4+）；升级后必须重新生成 schema 存档并复核本矩阵（尤其方法名与 `gpt-6-astra` 模型门禁）。adapter 在当前锁版本下必须显式传 `model`。
4. P3.1 首批复验清单：`thread/read`、`thread/items/list`、`thread/turns/list`、`turn/plan/updated`、`item/plan/delta`、`account/usage/read`、reasoning/delta 事件、重连后事件重放语义、`thread/list` 分页游标。
5. 安全边界复核：本探针未 resume/approve 任何已存在线程，未修改 `~/.codex`，未启动/停止 daemon；唯一的写是两个一次性 ephemeral 会话（探针 7 与 6b，均固定模型+只读沙箱+never/untrusted 审批策略，结束后进程与临时目录均已清理）。
