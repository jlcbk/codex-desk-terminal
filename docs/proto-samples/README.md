# 协议样本存档（codex-cli 0.152.0，本机生成）

生成日期：2026-09-10（任务 P0.2）。所有内容来自本机 `codex app-server` 0.152.0 的真实交互，已脱敏。

## Schema 存档

生成命令（在本仓库根目录执行）：

```sh
codex app-server generate-json-schema --out artifacts/probe/schema-bundle --experimental
# 或直接复现存档：
python3 scripts/probe/probe_01_generate_schema.py
python3 scripts/probe/make_samples.py
```

- `codex_app_server_protocol.v2.schemas.json` — v2 协议面（`CodexAppServerProtocolV2`，734 个定义；本文档矩阵所引用的方法名全部来自此文件）
- `codex_app_server_protocol.v1.schemas.json` — v1 协议面（`CodexAppServerProtocol`，91 个定义；生成器原始文件名 `codex_app_server_protocol.schemas.json`）
- `methods-codex-0.152.0.json` — 从上述 schema 提取的方法名枚举（ClientRequest 154 / ServerNotification 81 / ServerRequest 11）

注意：schema 由本机锁定的 0.152.0 生成，属机器生成的非敏感产物，可直接入库。CLI 版本升级后必须重新生成并复核 `docs/CODEX_CAPABILITIES.md`。

## 交互样本

目录（均由 `scripts/probe/make_samples.py` 从 `artifacts/probe/` 证据自动提取）：

- `requests/` — client→server 请求，以及一例**未回应**的 server→client 审批请求
- `responses/` — server→client 响应（initialize / thread/list / rateLimits / thread:start / turn:start 等）
- `events/` — server→client 通知（生命周期、tokenUsage、rateLimits 推送、waitingOnApproval、serverRequest/resolved）

脱敏策略（`scripts/probe/redact.py`）：

- API key / token / cookie / 邮箱 / 账号 id / 会话 id → `"<redacted>"`
- 用户内容（preview、消息文本、线程名等）→ `"<content len=N>"`（只留长度）
- 用户 home 路径 → `~/` 前缀；token **计数**与 modelContextWindow 保留（协议事实，非凭证）

刷新样本：重跑 `scripts/probe/README.md` 中的探针命令后再跑 `make_samples.py`。
