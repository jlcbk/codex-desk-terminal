# P0.2 app-server 能力探针脚本

对应任务 P0.2（A1 Bridge/State）、能力矩阵见 `docs/CODEX_CAPABILITIES.md`。
全部脚本为 Python 3.9 stdlib 实现，每次交互显式超时，子进程以独立进程组启动并在退出时 SIGTERM/SIGKILL 收割，不留后台进程。

## 安全边界（先读）

- 除 `probe_06b` 与 `probe_07` 外全部只读；这两个脚本也只在 `tempfile.mkdtemp(prefix="codex-probe-*")` 隔离目录里创建**全新的 ephemeral 线程**（固定模型、readOnly 沙箱、never/untrusted 审批策略），绝不 resume/approve 任何已存在线程，绝不修改 `~/.codex`，绝不启动/停止 daemon。
- `probe_06b` 收到审批请求后**只记录不回应**，用自己的 `turn/interrupt` 结束 turn。
- 证据写 `artifacts/probe/`（git 忽略）；入库样本经 `redact.py` 脱敏后由 `make_samples.py` 生成到 `docs/proto-samples/`。

## 脚本 → 矩阵行对照

| 脚本 | 矩阵行 | 说明 |
|---|---|---|
| `probe_01_generate_schema.py` | 1 | 运行 `generate-json-schema --out --experimental`，记录 414 文件清单 |
| `probe_02_initialize.py` | 2 | stdio JSON-RPC initialize 握手 |
| `probe_03_thread_list.py` | 3a/3b | 只读 `thread/list` + `thread/loaded/list` |
| `probe_04_rate_limits.py` | 4 | 只读 `account/rateLimits/read` + `account/read` |
| `probe_05_notifications.py` | 5 | 被动监听通知；默认自有 stdio 实例，`--proxy` 走 daemon proxy |
| `probe_06_pending_approval.py` | 6（扫描） | 只读扫描线程 `activeFlags`；`--proxy`/`--listen` 可选 |
| `probe_06b_approval_observable.py` | 6（受控验证） | 隔离会话内触发审批请求，不回应，只读观察 `waitingOnApproval`，最后 interrupt |
| `probe_07_bridge_owned_session.py` | 7 | 隔离目录 bridge-owned 全生命周期；`--with-plan-turn` 可选第二轮取 plan 事件；`--model` 固定模型 |
| `probe_08_daemon_proxy.py` | 8 | `daemon version` 只读探活；若有 daemon 则 proxy 只读探测；绝不启停 daemon |
| `appserver_client.py` | — | 共享 JSON-RPC stdio 客户端（含 server→client 请求路由） |
| `redact.py` | — | 证据脱敏（凭证/内容/路径） |
| `make_samples.py` | — | 从证据生成 `docs/proto-samples/` 入库样本 |

## 运行命令

```sh
# 全套只读探针（约 2 分钟）
python3 scripts/probe/probe_01_generate_schema.py
python3 scripts/probe/probe_02_initialize.py
python3 scripts/probe/probe_03_thread_list.py --limit 5
python3 scripts/probe/probe_04_rate_limits.py

# 被动监听 30s（自有实例）；桌面在跑时可观察"桌面事件是否到达"
python3 scripts/probe/probe_05_notifications.py --seconds 30 --with-thread-list
# 若本机有 daemon 在跑，可试共享通道（无 daemon 会超时失败，即证据）
python3 scripts/probe/probe_05_notifications.py --seconds 10 --proxy

# 审批等待只读扫描 + 受控验证（后者含一次隔离写会话）
python3 scripts/probe/probe_06_pending_approval.py --listen 10
python3 scripts/probe/probe_06b_approval_observable.py

# bridge-owned 全生命周期（含一次隔离写会话；模型必须显式固定，见下）
python3 scripts/probe/probe_07_bridge_owned_session.py --seconds 90
python3 scripts/probe/probe_07_bridge_owned_session.py --seconds 90 --with-plan-turn  # 可选：追加 plan 事件观察

# daemon/proxy 现状
python3 scripts/probe/probe_08_daemon_proxy.py --listen 20

# 从证据刷新 docs/proto-samples/ 入库样本
python3 scripts/probe/make_samples.py
```

## 已知坑（0.152.0 实测）

- 账户默认模型 `gpt-6-astra` 需要 ≥0.153.x；探针/adapter 必须传 `model`（0.152.0 可用：`gpt-5.6-sol`/`gpt-5.6-terra`/`gpt-5.6-luna`/`gpt-5.5`，用只读 `model/list` 核实）。
- `turn/interrupt` 必须同时带 `threadId` 和 `turnId`（缺 turnId 报 -32600）。
- `model/list` 等方法不接受 `"params": null`，要传空对象/完整参数。
- `thread/loaded/list` 只返回线程 id 字符串数组；ephemeral 线程不出现在 `thread/list`，其状态只能靠 `thread/status/changed` 通知。
- 0.152.0 没有 `daemon status` 子命令，用 `daemon version` 探活（只读）。

## 证据位置

`artifacts/probe/01-generate-schema.json` … `08-daemon-proxy.json`（git 忽略）。
`docs/CODEX_CAPABILITIES.md` 的矩阵表逐行引用这些文件。
