# 传输层冻结定义（protocol/transport.md）

状态：**P0.5 已由 A4 于 2026-09-10 冻结**。依赖并复述 docs/INTERFACES.md v1（P0.4，2026-09-10 冻结）§5/§6/§7 的冻结值；本文在其上补充可实施细节（UUID、ACK/NACK 字段填充、握手/错误码表、退避参数、权限流程）。帧头 16 字节结构与字段语义是 INTERFACES 冻结值，本文只复述不改动；本文新增细节的变更须走 A0 契约变更流程，同步 `shared/transport/cdt_frame.h`、`bridge/transports/`、`firmware/components/transport/` 三处实现。

红线：Transport 只搬字节。帧/CRC/分片/重连不解释业务 JSON——本文件与 C 头不引入 AppState/Telemetry 语义（唯一例外：按 INTERFACES §7 冻结的尺寸上限 16384/512 来自业务契约，此处仅作为传输缓冲上限复述）。凭证（设备 token、证书私钥）不进入 State、fixtures、日志或 Git。

---

## 1. 公共传输语义（两种适配器同一接口）

引用 INTERFACES §5 Transport 行，两种适配器对外行为完全一致：

- 接口：`start(config)`、`stop()`、`send(bytes,len)`、`on_message(bytes,len)`、`on_link(status)`。
- `send` 返回 `accepted / busy / error`；只回调完整包；不回调半包。
- 两种 Transport 传输**字节完全相同的** AppState JSON（§8：hash 一致，渲染一致）。Transport 不生成、不修补、不解读 payload。
- 重连总是取得全量快照；v1 只发全量，无 delta。
- 配置切换 transport 须先 `stop()` 旧适配器再 `start()` 新适配器；同一时刻只有一个活动 transport（P3.5 断言）。
- 设备本地电池保护优先于任何传输行为：进入 CRITICAL 后取消重连与在途发送（DEVELOPMENT_PLAN §7.2/§7.3）。

方向与尺寸冻结：

| 方向 | 载荷 | 聚合上限 |
|---|---|---:|
| Bridge → 设备 | AppState JSON（完整快照） | ≤16384 字节 |
| 设备 → Bridge | DeviceTelemetry JSON（可选上行，独立类型） | ≤512 字节 |

上行是可选能力：Transport 按"下行/上行两个独立的尺寸上限"搬运，不读取内容；设备无遥测时不发。两方向的消息上下文（message_id 计数、重组缓冲）相互独立。

---

## 2. BLE GATT 定义（冻结）

### 2.1 服务与特征 UUID（本次生成一次，永不再变）

| 项 | UUID（128bit） | 生成方式 |
|---|---|---|
| Service | `B931F216-B7FD-50E9-8C33-F1416ADE3B1D` | UUIDv5（URL 命名空间）|
| RX（central → 设备，Write） | `890AAC2C-3C19-5200-BDEC-74943BA536A1` | UUIDv5（URL 命名空间）|
| TX（设备 → central，Notify/Indicate） | `8EABE170-A4E7-5C26-A287-6F5871014707` | UUIDv5（URL 命名空间）|

生成方式记录（2026-09-10，A4 一次生成后冻结；结果可由下式确定性复现，但不允许再派生新 UUID 扩展 v1 协议）：

```text
uuid5(NAMESPACE_URL,
      "https://codex-desk-terminal.invalid/protocol/transport/v1#ble-service")
uuid5(NAMESPACE_URL,
      "https://codex-desk-terminal.invalid/protocol/transport/v1#ble-rx")
uuid5(NAMESPACE_URL,
      "https://codex-desk-terminal.invalid/protocol/transport/v1#ble-tx")
```

选型理由：UUIDv5 确定性可复现（审计可重算），`.invalid` 域保证不与任何真实 URL 冲突；不采用 Nordic UART Service 等第三方 UUID（所有权与冲突风险），也不采用随机 v4（无法复核来源）。

### 2.2 GATT 结构与权限

ESP32 为 GATT peripheral（服务器），Mac/PC Bridge 为 central（客户端）。

| GATT 项 | UUID | 属性（Properties） | 权限/安全要求 |
|---|---|---|---|
| Primary Service | `B931F216-…-3B1D` | — | — |
| RX characteristic | `890AAC2C-…-36A1` | Write、Write Without Response | **加密 + MITM**（已认证配对）方可写；无读属性 |
| TX characteristic | `8EABE170-…-4707` | Notify、Indicate | 订阅（CCCD 写入）要求**加密 + MITM**；特征本身无读/写属性 |
| CCCD（TX 内含） | 0x2902 | Read、Write | 同 TX：加密 + MITM |

实现映射：ESP-IDF NimBLE 侧即 `BLE_ATT_F_WRITE / BLE_ATT_F_WRITE_NO_RSP`、`BLE_ATT_F_READ`（仅 CCCD）加 `encrypted + MITM` 标志；CoreBluetooth 侧无需逐特征配置，由系统配对机制落实。v1 特征不开放 Read（数据不落特征缓存，绕过"读回旧半包"问题）。

广播（便于 Mac 端按服务过滤）：Flags + 128bit Service UUID（18 字节，可入 adv 包）；设备名 `CodexDT` 放 scan response。Bridge 只连接广播含本服务 UUID 且已绑定过的设备。

### 2.3 配对与绑定（身份验证）

冻结方案：**LE Secure Connections（ECDH P-256）+ 数字比较（Numeric Comparison）+ 绑定**。

1. 首次连接触发配对：设备屏显示 6 位 passkey，macOS 弹出比较对话框；用户在 Mac 上确认，并在设备上**长按 KEY 确认**（双向确认）。
2. 绑定成功后 LTK 存设备 NVS；后续重连直接使用既有绑定，加密建立后才允许业务读写（未加密的 RX 写入/CCCD 订阅被 GATT 权限拒绝）。
3. 未注明事项：KEY 参与配对确认尚未在真机验证（P3.4 小样步骤 B2 取证）。若实测发现本板 KEY 无法用于配对确认，按 INTERFACES §7 记录并修订认证设计后再部署，**不得默默退化为公开写入或 Just Works**。

### 2.4 MTU 与写入节奏

- 以协商 ATT MTU 计算每片容量：`chunk = ATT_MTU − 3 − 16`，要求 >0；MTU23 时 chunk=4，16384 字节整包恰为 4096 片 = `CDT_MAX_FRAGMENTS`（见 §3）。
- 默认 MTU23 必须可工作（协议正确性不靠大 MTU 掩盖）；正常连接尽量协商更大 MTU（macOS 通常协商至 ≥185，实测值在 P3.4 记录）。
- RX 优先 Write Without Response（提高吞吐、避免逐写等待），流控由 §3.4 的"一次一条在途 + ACK"承担；设备须同时接受带响应写。
- 连接参数初值由 central 侧（macOS）决定，设备不主动申请极端参数；功耗相关调优（连接间隔、modem-sleep）属 P5.2/P6.1，不在本文冻结。

---

## 3. BLE 二进制帧头（冻结，16 字节小端）

### 3.1 字段表（= INTERFACES §7 冻结值，逐字复述）

| 字段 | 字节数 | 语义 |
|---|---:|---|
| version | 1 | 帧版本1 |
| type | 1 | DATA=1、ACK=2、NACK=3 |
| message_id | 4 | 本次连接内递增，重连清空接收上下文 |
| fragment_index | 2 | 从0开始 |
| fragment_count | 2 | 总片数，≥1 |
| total_len | 2 | 完整JSON字节数≤16384 |
| crc32 | 4 | 完整JSON的CRC32；只检传输错误，不替代认证 |

合计 16 字节；字节偏移：version=0、type=1、message_id=2..5、fragment_index=6..7、fragment_count=8..9、total_len=10..11、crc32=12..15；多字节字段一律小端。C 定义见 `shared/transport/cdt_frame.h`（C99，`#pragma pack(1)`，含常量、`cdt_crc32` 声明与编解码接口声明，文件头注明"由 P0.5 冻结，与 protocol/transport.md 一致"）。

`crc32` 覆盖**完整 payload（重组后的 JSON 字节）**，不是单帧；帧头本身不参与 CRC。ACK/NACK 无 payload（写入/通知仅 16 字节头）。

### 3.2 ACK/NACK 字段填充（P0.5 冻结的具体化）

INTERFACES §7："ACK 回传相同 message_id/header 元数据、无 payload；NACK 用相同头、无 payload 表示需重发全包。"具体冻结：

| 字段 | DATA（发送方填） | ACK / NACK（接收方回填） |
|---|---|---|
| version | 1 | 1 |
| type | 1 | 2（ACK）或 3（NACK） |
| message_id | 本次连接内从 1 递增（两方向各自独立计数） | 被确认 DATA 的 message_id |
| fragment_index | 0..fragment_count−1 | **0** |
| fragment_count | 本消息总片数（≥1） | 被确认 DATA 的 fragment_count |
| total_len | 完整 payload 字节数 | 被确认 DATA 的 total_len |
| crc32 | 完整 payload 的 CRC32 | 被确认 DATA 的 crc32 |

### 3.3 分片与重组规则（= INTERFACES §7，含落地顺序）

发送方：

1. `fragment_count = ceil(total_len / chunk)`（total_len=0 不存在：AppState/Telemetry 均非空；收到 total_len=0 的 DATA 视为矛盾头，NACK 并丢弃）。
2. 第 i 片 payload = 原始字节 `[i×chunk, min((i+1)×chunk, total_len))`；**只有最后一片允许短片**。
3. 所有片携带完全相同的头（除 fragment_index 外逐字段一致）。
4. 一次只有 1 条在途消息；发出最后一片后启动 ACK 等待，**冻结 ACK 超时 5000ms**（初值，P3.4 实测后可由 A0 调整）；最多重发全包 2 次，仍失败则断开重连/重新同步最新快照。

接收方：

1. 收到第一片即锁定重组上下文（message_id/count/len/crc）；任一片与上下文矛盾（count/len/crc 不一致、index 越界）→ 拒绝整包并回 NACK。
2. 同一 index 收到字节相同的片 → 忽略；字节不同的片 → 拒绝整包并回 NACK。
3. 缓冲上限 16384 字节 + 接收片位图；`total_len > 16384` 或 `fragment_count > 4096` → 矛盾头，NACK 并丢弃。
4. **3 秒**无新片进展 → 丢弃上下文并回 NACK；整条消息重组**超过 30 秒**未完成 → 丢弃并回 NACK（两个初值 = INTERFACES 冻结值；P0 小 MTU 实测可调整，走 A0 流程）。
5. 全部分片到齐 → `cdt_crc32(重组字节) == crc32`？否 → NACK。是 → 交上层应用。
6. 断连立刻清空半包与位图；重连后 message_id 上下文清空，从新连接重新同步（不恢复旧半包）。

### 3.4 ACK 语义与业务层衔接（保持"不解释 JSON"红线）

冻结：接收侧传输管线在"完整包通过 CRC"之后，依据**上层应用回调的三值结果**决定 ACK：

| 上层回调结果（Store 层给出，Transport 不解析内容） | 传输行为 |
|---|---|
| applied（新快照接受） | ACK |
| duplicate（同 epoch 同 seq 已应用；按 §7 冻结应 ACK 但不再渲染） | ACK |
| rejected（JSON 非法/校验失败等） | NACK（触发重发全包；再次 rejected 依旧 NACK，重试耗尽后按 §3.3 第 4 条处理） |

Transport 始终只见 applied/duplicate/rejected 三值信号，不读取任何业务字段。桥接器收到设备遥测 DATA 后同样以三值回调驱动 ACK（经 RX 写回 ACK 帧）。

---

## 4. CRC32 测试向量（冻结）

算法冻结：**标准 IEEE CRC32**（反射多项式 0xEDB88320，初值 0xFFFFFFFF，结果异或 0xFFFFFFFF），与 Python `zlib.crc32` 逐位一致。复核脚本 `scripts/gen_crc_vectors.py`（自带逐位参考实现与 zlib/binascii 三方交叉验证），本节数值由其计算并写死：

| 向量 | 名称 | payload 长度（字节） | CRC32（hex） |
|---|---|---:|---|
| V1 | empty_payload | 0 | 0x00000000 |
| V2 | short_json | 24 | 0x22B3AE57 |
| V3 | pattern_16k | 16384 | 0xE81722F0 |
| V4 | fox_reference | 43 | 0x414FA339 |

payload 构造规则（契约的一部分，不得静默修改）：

- V1：`b""`（0 字节，`total_len=0` 的参考值；§3.3 已冻结 DATA 不允许 total_len=0，此向量仅用于 CRC 实现对齐）。
- V2：`b'{"kind":"state","seq":1}'`（24 字节短 JSON）。
- V3：`bytes(range(256)) * 64`（16384 字节，恰好等于整包上限的上界向量）。
- V4：`b"The quick brown fox jumps over the lazy dog"`（43 字节；0x414FA339 为公开文献参照值，用于与任意外部 CRC32 实现交叉核对）。

自验命令（重跑输出须与本表一致，退出码 0）：

```sh
python3 scripts/gen_crc_vectors.py --verify   # PASS: 4/4 个冻结向量与 protocol/transport.md 一致
```

---

## 5. Wi-Fi / WSS 协议（冻结）

### 5.1 角色与端点

- **ESP32 为 WSS 客户端，Bridge 为服务端**；业务路径冻结为 **`/v1/state`**（upgrade 请求路径；其余路径一律 404 并关闭）。
- 端口：开发默认 8765（可配置）；无自动发现，设备配置Bridge 的 IP:port。
- 每 **WebSocket text message 一份完整 AppState**；底层分片由 WebSocket 库处理，仍限制**聚合后 ≤16384 字节**。上行遥测为独立 text message，聚合 ≤512 字节。v1 不使用 binary frame、不做 HTTP 轮询、不加其他端点。
- 开发期 loopback 允许 `ws://`（明文）**仅限** 127.0.0.1/::1 且构建/启动显式标记 dev；生产构建硬性禁止关闭 TLS（配置存在即拒绝启动）。

### 5.2 握手序列（冻结）

1. 设备发起 TCP + TLS 1.2+ 连接；证书验证失败/指纹不符 → `CONFIG_ERROR`（终态，见 5.4）。
2. HTTP upgrade：`GET wss://<bridge>/v1/state`，携带 `Authorization: Bearer <device-token>`。token 为**独立设备 token**（Bridge 配置工具生成、两端本地保存：设备入 NVS，Bridge 入本地配置文件；不使用 Codex 凭证；不入 Git、不进日志——日志只记 token 指纹前 8 hex）。校验失败/缺失 → HTTP **401**；已连设备数超上限等策略拒绝 → HTTP **403**。设备对 401/403 一律进入 `CONFIG_ERROR` 终态。
3. upgrade 成功（HTTP 101）→ `on_link(connected)`；**Bridge 立即发送一份完整 AppState 作为首条 text message**（认证后立即发送完整快照，INTERFACES §6）。
4. 设备按首条快照的 `bridge_epoch` 初始化 seq 上下文（此判定在业务接收层完成，Transport 只按字节交付）：
   - **新 epoch**（Bridge 重启过）：清空旧链路未完成消息与 seq 上下文，直接应用全量。
   - **同 epoch 重连**：保留 last_seq；Bridge 后续发送必须更新 seq；设备对重复 seq 按 duplicate 处理（不回退 UI）。
5. 心跳：WebSocket ping/pong 双端启用（websockets 库默认 `ping_interval=20s / ping_timeout=20s`）；pong 超时按 1006 断开处理。快照新鲜度（15s 存活快照 / 45s stale / 150s disconnected）是业务/Runtime 层职责，不在此重复定义。

### 5.3 消息尺寸与错误码表（冻结）

| 错误场景 | 信号 | 设备侧行为 |
|---|---|---|
| token 缺失/错误 | upgrade 应答 HTTP 401 | `CONFIG_ERROR` 终态；**不重试**，仅配置变更/人工介入后重试 |
| 策略拒绝（设备数超限等） | HTTP 403 | 同 401 |
| 证书/SPKI 指纹不匹配 | TLS 握手失败 | `CONFIG_ERROR` 终态；不重试 |
| upgrade 期间服务器错误 | HTTP 500/503 | 可重试：进入退避 |
| 网络失联/无 close frame | close code 1006 | 可重试：进入退避 |
| Bridge 正常关闭 | close 1000（Bridge 主动停机） | 可重试：进入退避（等待 Bridge 重启） |
| 连接中鉴权态失效 | close 1008 | `CONFIG_ERROR` 终态 |
| 收到聚合 >16384 字节的下行消息 | close 1009（Bridge 侧禁止发生；发生即 Bridge 缺陷） | 记录计数、关闭重连（退避），不解析不应用 |
| Bridge 收到 >512 字节上行消息 | close 1009（对设备） | 设备记录计数并退避重连 |
| 服务器内部错误 | close 1011 | 可重试：进入退避 |

### 5.4 重连与退避（冻结）

- 可重试类失败退避序列：**1 / 2 / 4 / 8 / 16 / 30 秒，封顶 30s，叠加 ±20% 均匀抖动**（如 1s → 0.8–1.2s 实际等待）；抖动目的为多设备错峰。
- 连接保持 ≥60s 后重置回 1s 档（稳定即重置）。
- `CONFIG_ERROR`（401/403/证书失败）**不进入退避循环**：状态置为配置错误并上报 UI（可操作提示），只有配置变更或人工触发才重试——禁止高频无限重试。
- 设备进入 CRITICAL/低压流程：取消一切重连与在途发送（电池保护优先）。
- 重连成功后按 §5.2 第 3–4 步取得最新全量快照；旧链路半包/未完成消息全部丢弃。

### 5.5 证书与指纹要求（v1.1 修订，A0 2026-09-12）

- 设备**必须验证** Bridge 身份，生产配置不允许关闭（fail-closed）。
- **v1 生效方案（已修订）**：Bridge 使用自签证书（SAN 含 Bridge 主机名/LAN IP），设备侧执行：①以预置的**专用自签 CA 证书**验证证书链；②验证**主机名**与连接目标一致（mbedTLS 默认 hostname 校验）。两者任一失败即 `CONFIG_ERROR` 终态。
- **SPKI SHA-256 指纹 pinning 降级为可选强化项**（不在 v1 交付）：安全决策记录——专用 CA 私钥仅存 Bridge 本地且 gitignored；攻击者若能获取 CA 私钥即可为任意主机签发证书，等效绕过 SPKI pin（因 pin 值本身编在固件中可被同一攻击者提取）。因此 v1 的实际安全边界=CA 私钥的物理安全。未来若部署到不受控网络（如公网穿透、非家用 LAN），须升级为 SPKI pinning 并补负向测试（错误 pin 拒绝、正确 pin 通过）。
- 供给方式：CA 证书在**首次烧录/USB 串口供应阶段**写入 NVS 或编译期嵌入（dev_net_config.h，gitignored）；不通过空气下发。证书私钥只存 Bridge 本地（`.gitignore` 已覆盖 `*.pem/*.key`、`config/local/`）。
- 使用成熟 TLS/WebSocket 实现（websockets + 系统 OpenSSL；设备侧 ESP-TLS/mbedTLS），不自制加密。

---

## 6. macOS 权限流程与开发/真机配置

### 6.1 BLE central（Mac 端，Bridge 进程）

| 项 | 要求与行为 |
|---|---|
| 用途声明 | 打包为 .app 分发时 Info.plist 必须含 `NSBluetoothAlwaysUsageDescription`（macOS 10.15+，缺失则 CoreBluetooth 调用直接失败）。当前 Bridge 为 CLI（uv 管理的 Python 进程），**授权归属于承载进程**（Terminal/iTerm/IDE），首次扫描时系统向该 App 弹出蓝牙授权框，允许一次长期有效 |
| Entitlement | 仅 App Sandbox 分发需要 `com.apple.security.device.bluetooth`；开发期 CLI 非 sandbox，无 Entitlement 要求 |
| 权限被拒错误路径 | 扫描/连接抛出授权类异常（CoreBluetooth authorization=denied）。Transport 必须把错误转成可操作提示：「系统设置 → 隐私与安全性 → 蓝牙 → 为终端 App 开启，然后重启 Bridge 进程」，并 `on_link(disconnected)`；不得静默循环重试 |
| 蓝牙关闭错误路径 | central state=poweredOff → 可操作提示「系统设置 → 蓝牙 打开」，不重试扫描 |
| 未配对/配对被拒 | 设备拒绝配对或用户在任一端取消 → 连接失败并提示重新配对步骤；不退化为未加密连接 |

准确弹窗文案与授权归属（终端 App 还是 python 进程）在 P3.4 小样步骤 B0 实测并截图存证（artifacts/）。

### 6.2 Wi-Fi（Mac 端 Bridge 为服务端）

- macOS 对监听端口**无 TCC 隐私权限**；但两种系统提示需要记录：
  1. **应用防火墙**（若开启）：未签名进程首次监听会弹「“python”想要接受传入网络连接」。拒绝后设备无法连入；修复：系统设置 → 网络 → 防火墙 → 选项放行，或 `sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add <python> --allow`。
  2. **本地网络隐私提示**（macOS 15+）：进程访问局域网设备可能触发"本地网络"授权，归属同 6.1 的承载进程；拒绝会导致连不上设备。
  弹窗行为在 P3.3 小样步骤 W2 实测取证；本节为文档化预期，不作为实测结论。
- Bridge 绑定规则：loopback 配置绑 `127.0.0.1`；真机配置**显式绑定 LAN 接口地址**（不绑 0.0.0.0，除非配置明确要求）。

### 6.3 两种开发配置

| 项 | 开发期 loopback | 真机 LAN |
|---|---|---|
| Bridge 监听 | `ws://127.0.0.1:8765/v1/state`（明文，仅 dev 标记构建） | `wss://<bridge-lan-ip>:8765/v1/state`（TLS+pinning） |
| 设备端 | Python mock 设备客户端（同协议同错误码） | ESP32 WSS client |
| 证书 | 不需要 | 自签 CA + SPKI 指纹预置 NVS |
| token | dev token（配置文件，不入库） | 正式设备 token（同左存储规则） |
| 权限提示 | 无（loopback 不触发防火墙/本地网络） | 首次监听/连入各一次系统提示（6.2） |

BLE 对应的两种配置：小样期在**同一 Mac 用进程内虚拟链路**实现 Transport 接口做语义测试（无系统权限要求）；真机期才触发 6.1 的蓝牙授权与配对流程。

---

## 7. 技术小样计划（P3.3 / P3.4 前置，本文只定计划不实现）

小样总原则（= DEVELOPMENT_PLAN §P3 说明）：先用 loopback/mock receiver 验证传输语义，再上真机；真机未实测的项一律标"未验证"。小样共用一份消息 fixtures（与 Wi-Fi/BLE 相同 JSON 字节）。

### 7.0 共同第 0 步：纯逻辑帧内核（PC，无硬件）

对 Python 侧帧编解码/分片重组器（`bridge/transports` 共用内核）执行故障矩阵（注入式，虚拟单调时钟）：

| 用例 | 通过判据 |
|---|---|
| 正常分片（chunk=4 与 chunk=228 两档）/重组 | 重组字节与原始逐字节一致 |
| §4 四个冻结向量 | CRC 计算一致；`gen_crc_vectors.py --verify` 退出码 0 |
| CRC 坏、同 index 异数据、index 越界、count/len 矛盾、total_len>16384 | 一律拒绝整包并 NACK，缓冲清空，不交付 on_message |
| 重复片、乱序片 | 最终重组正确；重复片被忽略 |
| 3s 无进展 / 30s 整包超时（虚拟时钟推进） | 上下文丢弃 + NACK，无内存增长 |
| 断连清半包 | 上下文立即清空；重连后 message_id 从头计数 |

### 7.1 WSS 小样（P3.3 前置）

| 步骤 | 内容 | 通过判据（引用 P3.3 验收行：「断网重连取得最新快照；拒绝未授权连接及超大帧」） |
|---|---|---|
| W0 | 依赖安装（websockets==17.1，见 §8）+ loopback 服务端 + Python mock 设备客户端 | upgrade 101 后首条消息为完整 AppState；心跳 ping/pong 生效 |
| W1 | 鉴权矩阵：正确 token / 缺失 / 错误 | 401/403 路径：客户端进入 CONFIG_ERROR，**重试计数为 0**；正确 token 正常收首快照 |
| W2 | LAN 预演（Mac 本机双地址 + 防火墙开启）：记录系统弹窗与放行步骤截图 | 弹窗行为与 §6.2 描述一致并存证 artifacts/；被拒路径有明确错误输出 |
| W3 | 尺寸/错误码：发 >16KiB text；异常断开 | 收到 close 1009；1006/1000/1011 分别进入预期分支 |
| W4 | 断连重连语义：kill 服务端→退避重连；Bridge 重启（新 epoch）；同 epoch 重连 | 退避序列 1/2/4/8/16/30s±20% 用虚拟时钟断言；重连后**取得最新快照**；同 epoch 保留 last_seq、新 epoch 全量替换 |
| W5 | 真机 LAN（ESP32 client，TLS+pinning+token） | 真机完成 P3.3 验收行全项；未完成项如实标未验证 |

### 7.2 BLE 小样（P3.4 前置）

| 步骤 | 内容 | 通过判据（引用 P3.4 验收行：「最小 MTU/大包/断连半包不会破坏 store；重连重发全量」） |
|---|---|---|
| B0 | macOS 蓝牙权限与弹窗取证（运行扫描一次） | §6.1 流程与实际弹窗一致，截图存证；被拒路径输出可操作错误 |
| B1 | 进程内虚拟 BLE 链路（实现同一 Transport 接口）跑 §7.0 故障矩阵 + ACK/NACK 状态机（含 duplicate→ACK、rejected→NACK、5000ms ACK 超时、2 次重试后重连） | 全部断言通过；store 始终保留最后合法快照 |
| B2 | ESP32 peripheral（NimBLE，冻结 UUID）+ Bleak central：LE Secure Connections 配对（数字比较 + KEY 确认）、订阅 TX、写 RX | 配对成功；KEY 确认可行性取证（不可行则按 §2.3 记录并修订设计，不静默降级） |
| B3 | 大包传输：16KiB 快照在协商 MTU 下分片传输至 ACK；注入单帧 CRC 损坏 | ACK 收敛；CRC 损坏触发 NACK → 全包重发 → ACK；**store 未破坏** |
| B4 | 断连半包：传输中途断开（关蓝牙/超距） | 半包丢弃、store 保留旧快照；重连后**重发全量**并收敛 |
| B5 | 最小 MTU23（强制不分片库优化，逐 4 字节片）吞吐测量 | 消息在 30s 整包超时内完成或如实记录未达标值；记录正常/最坏 MTU 两种指标（§7 INTERFACES 要求） |

### 7.3 与 P3.5 共同故障测试的衔接

小样通过后，重复/乱序/超时/Bridge 重启/transport 切换矩阵并入 P3.5（A4+A5 共同执行）：断言 UI 不回退、一次仅一个活动 transport、连接状态可观测。小样阶段产物（日志、截图、虚拟时钟断言输出）存 `artifacts/`（不入库），证据路径记入 docs/STATUS.md 由 A0 收编。

---

## 8. 小样依赖建议（冻结建议，正式版本锁由 A0 记入 docs/VERSIONS.md）

2026-09-10 经 PyPI 实时查询（pip index + PyPI 项目页双源核对）：

| 依赖 | 建议锁定版本 | 许可证 | Python 3.12 兼容 | 依据 |
|---|---|---|---|---|
| BLE central | `bleak==3.0.2`（2026-05-02 发布） | MIT | `requires_python >=3.10` → 兼容 | macOS 走 CoreBluetooth（10.15+），跨平台异步 API，社区活跃（2025-11 起 2.x→3.x 连续发版）；本机 pip(3.9) 仅可见 1.1.1，与 3.x 要求 ≥3.10 一致，Bridge 运行时为 CPython 3.12 故取 3.0.2 |
| WSS server | `websockets==17.1`（2026-08-26 发布） | BSD-3-Clause | `requires_python >=3.11`、classifier 显式含 3.12 → 兼容 | RFC 6455/7692 全量合规测试、内置 ping/pong 与 backpressure、asyncio 实现契合 Bridge；长期单一维护者+Tidelift 支持，Production/Stable |

小样阶段用 `uv` 注入这两个精确版本（不浮动）；API 细节（bleak 3.x 相对 1.x 的接口差异）以 P3.4 步骤 B2 实测为准，若有破坏性差异记录后由 A0 重新评审版本。

---

## 9. P0.5 冻结清单（对照 INTERFACES §9）

- [x] BLE 服务/RX/TX UUID 冻结（§2.1，UUIDv5 生成方式可复现）。
- [x] 16 字节帧头复述冻结 + ACK/NACK 填充、分片/重组参数具体化（§3）；C 头 `shared/transport/cdt_frame.h` 同步（`cc -std=c99 -pedantic` 语法检查通过）。
- [x] CRC32 算法与 4 组测试向量冻结，`scripts/gen_crc_vectors.py --verify` 自验一致（§4）。
- [x] macOS BLE 配对/权限流程确定（§2.3/§6.1）；Wi-Fi 证书/设备 token/loopback 流程确定（§5/§6.2/§6.3）。
- [ ] 真机取证项（KEY 配对确认、实际弹窗截图、MTU 吞吐、配对实测）留待 P3.3/P3.4 小样，按 §7 计划执行并如实标注验证状态。
