# 接口契约 v1（已冻结）

状态：**P0.4 已由 A0 于 2026-09-10 冻结**。冻结裁决补记见 §3 与 §1a；此后语义变化必须走主版本升级（§8）并同步修改本文、protocol/ schema 与 shared/state/ C 类型。此文件约束各 Agent 的共同实现；示例是项目协议，不是 Codex app-server 原始消息。所有具体大小均为首版预算，性能证据可支持调整。

## 1. 三种数据，单一业务快照

1. `AppState`：Bridge 产生、跨 BLE/Wi-Fi 发送的 Codex 业务全量快照。
2. `DeviceRuntime`：设备本地电池、连接、页面和时钟状态。生产固件不接受 Bridge 覆盖电池。
3. `ViewModel`：Presenter 在本地合并上述两者，UI 唯一输入。模拟器通过独立测试接口注入 DeviceRuntime。

不要在 AppState 混入假电压使其同时变成测试脚本和生产协议。统一 State JSON 指业务协议统一；两种 Transport 传输字节完全相同。可选上行 `DeviceTelemetry` 用独立类型，且不影响本地保护。

### 1a. DeviceTelemetry v1（P0.4 冻结）

可选上行（设备→Bridge），独立于 AppState，整包 ≤512 UTF-8 字节、深度 ≤12、合法 UTF-8；未知附加字段允许。字段：`schema_version`(=1)、`kind`(="telemetry")、`battery_mv`(整数 0–65535 或 null，2500–4500mV 有效性属设备本地校准不在协议层强制)、`battery_valid`(bool，false 时 battery_mv 必须 null)、`rssi_dbm`(整数 −128–0 或 null)、`transport`(枚举 ble/wifi/mock)、`sent_at_ms`(UTC Unix 毫秒或 null，设备无有效墙钟时 null)。语义边界：遥测绝不影响本地电池保护；生产构建禁用远端电池注入。真源 schema：protocol/telemetry.schema.json。

## 2. AppState 示例

```json
{
  "schema_version": 1,
  "kind": "state",
  "bridge_epoch": "mock-run-001",
  "seq": 7,
  "generated_at_ms": 1789002000000,
  "source": {
    "kind": "mock",
    "connected": true,
    "stale": false,
    "last_event_at_ms": 1789002000000
  },
  "selected_thread_id": "thread-demo",
  "threads_total": 1,
  "threads_truncated": false,
  "threads": [
    {
      "id": "thread-demo",
      "turn_id": "turn-001",
      "project": "codex-desk-terminal",
      "state": "needs_you",
      "activity": "请在电脑上处理权限请求",
      "updated_at_ms": 1789002000000,
      "elapsed_ms": 120000,
      "waiting_ms": 5000,
      "end_reason": null,
      "attention": { "pending_count": 1, "summary": "运行命令需要批准" },
      "plan": {
        "total": 3,
        "truncated": false,
        "steps": [
          { "text": "检查需求", "status": "completed" },
          { "text": "实现界面", "status": "in_progress" },
          { "text": "运行测试", "status": "pending" }
        ]
      },
      "context": { "used_tokens": null, "capacity_tokens": null, "used_percent": null }
    }
  ],
  "usage": {
    "available": true,
    "updated_at_ms": 1789002000000,
    "windows_total": 1,
    "windows_truncated": false,
    "windows": [
      { "id": "quota-example", "label": "SHORT WINDOW", "used_percent": 42.0, "duration_mins": 300, "resets_at_ms": 1789012800000 }
    ]
  }
}
```

## 3. 字段规则、上限与状态顺序

| 字段/类目 | 契约 |
|---|---|
| schema_version / kind | 必填，v1仅接受整数1和state；未知主版本拒绝并显示协议不兼容 |
| bridge_epoch | Bridge每次启动生成新ID，1–64 UTF-8字节；仅在认证连接/重新握手中接受变化 |
| seq | 同epoch严格递增安全整数，0至2^53−1；每次实际发送快照递增，包括存活快照；同epoch≤已应用值丢弃 |
| 时间 | *_at_ms为UTC Unix毫秒或null；elapsed_ms/waiting_ms为非负持续时间；不用墙钟判断低压/重连 |
| source.kind | mock / codex_bridge_owned / codex_desktop_observed / zcode_observed；不可把受控会话标为桌面旁听。zcode_observed=v1.1 增补（A0 2026-09-12）：ZCode 会话文件观察源；旧端 UNKNOWN_ENUM 拒包（fail-closed 不半应用），bridge 与固件须成对部署 |
| source.connected/stale | 上游状态，独立于设备无线连接；上游失联保持最后值并标陈旧 |
| threads | 最多8项；每项id唯一；总数≥数组长度；截断有标记。计数与总数类字段（threads_total、plan.total、windows_total、attention.pending_count）上限65535，与设备uint16存储对齐 |
| 字符串 | id 类字段（thread id / turn_id / usage window id）统一≤128字节、project≤96、activity/attention.summary≤192、plan.text≤128、usage.label≤48；按UTF-8完整码点截断 |
| state | idle / thinking / working / needs_you / done / error；未知枚举拒绝该快照，保留旧快照 |
| plan | steps最多8项，total为原始总数，truncated说明裁剪；status统一pending/in_progress/completed |
| context | 仅有可信来源才给值；缺值为null；used_tokens/capacity_tokens为非负整数或null；used_percent为0–100或null（与usage窗口同规）；累计token消耗不能直接映射当前context占用 |
| threads[].model | v1.2 可选增补（A0 2026-09-12）：string|null，≤48 UTF-8 字节（如 "GLM-5.3"）；会话模型名，缺失/null 显示 "--" |
| threads[].tokens | v1.2 可选增补（A0 2026-09-12）：object（不接受 null 整体），存在时三键必填 {"input_tokens","output_tokens","cached_tokens"}，各为非负整数或 null；语义=会话累计（跨 turn 不清零，turn_started 不重置）；设备端 >2^32 饱和到 uint32 上限；缺失显示 "--" |
| threads[].branch | v1.2 可选增补（A0 2026-09-12，ZC8）：string\|null，≤32 UTF-8 字节（git 分支名）；语义=来源端 best-effort（观察器对会话 cwd 只读执行 `git branch --show-current`，失败/超时/空输出→null），缺失/null 显示 "--" |
| usage | windows最多4项；windows_total≥数组长度（同threads规则）；窗口长度用正整数分钟；百分比0–100或null（含context.used_percent）；缺额度available=false、windows=[] |
| end_reason | null / completed / failed / cancelled；cancelled显示idle及取消说明 |
| selected_thread_id | null或必须指向已包含线程；优先保留选中线程并占一个数组名额 |
| 完整消息 | UTF-8 JSON≤16384字节，嵌套深度≤12；全部长度在分配内存前检查 |

上述字段全部必填；允许为null的字段只有表中时间点、选中ID、turn_id、end_reason、数值未知项，以及attention（允许整包null）。plan不为null但可空数组。所有对象可忽略未知附加字段，但必须计入消息/深度限制；schema应采用相同前向兼容规则。类型错误、越界或缺必填字段拒绝整包，不能半应用。

裁剪顺序：Bridge先按提醒优先级排序，保留选中线程；裁剪超长摘要/步骤/窗口后编码；仍超16KiB则逐步减少非选中低优先级线程，更新total/truncated。选中线程和NEEDS YOU优先；线程数超过上限时设备可见“还有N个”，v1不增加远程分页RPC。

reducer规则：

- keyed by thread_id + turn_id；turn开始清除旧plan、pending和计时，新turn不继承DONE。
- THINKING只在明确的上游活动可辨认时使用；未知工作阶段显示WORKING，不从延迟猜“思考”。
- pending request集合非空即NEEDS YOU；本地静音不删除pending。明确的resolve/completion/取消才清理；其他item事件不能抢掉等待状态。
- 同线程多个pending保留计数；有可靠失败终态时ERROR，成功终态DONE，取消IDLE+cancelled。终态清理该turn pending并禁止迟到活动复活该turn。
- PLAN UPDATE改变plan，通常保留WORKING；上游inProgress在adapter中转换为in_progress。
- 多线程排序：needs_you > error > working/thinking > done > idle；同级updated_at降序，再id升序。默认选首项；本地用户选中不被普通更新抢走，新增等待可用提示提醒。
- 上游断连不将所有任务改成IDLE；source.stale=true。transport失联也不伪造DONE。

adapter需按本机生成schema核验候选事件：turn/started、turn/completed、item/started、item/completed、turn/plan/updated、thread/tokenUsage/updated、account/rateLimits/read及updated，以及审批/用户输入请求和解除通知。这是实现核对清单，不能假设每个版本都提供同名方法或旁听能力。

## 4. DeviceRuntime 与测试注入

```json
{
  "battery_mv": 3590,
  "battery_valid": true,
  "usable_percent": 0,
  "charging": "unknown",
  "external_power": "unknown",
  "power_state": "critical",
  "transport": "mock",
  "link_state": "connected",
  "last_rx_monotonic_ms": 30000,
  "selected_page": "now",
  "muted_attention_id": null
}
```

这是模拟器可读的示例，不是设备下行消息。charging/external_power为yes/no/unknown；battery无效时mv与percent为null，且battery_valid=false。runtime的保护态由Power FSM计算；生产程序不开放“直接设置critical”网络接口。

测试场景使用独立JSONL：每行含at_ms（虚拟时钟）、action和payload。action可为app_state、battery_sample、key、link、advance_time。AppState的序号按业务快照走；battery_sample经与固件相同的FSM产生LOW BATTERY，不能只换一张图冒充保护已验证。

在线设备只在收到合法新seq快照时刷新last_rx。默认Bridge每15秒发送一份全量存活快照，45秒未收到有效快照标link stale；150秒无更新可标disconnected并重连。上游source.stale可更早出现。收到相同seq的重复数据不能让旧状态永远新鲜。

Presenter優先级：电池故障/CRITICAL/休眠准备强制页 > 正常业务页；链路陈旧提示独立于业务状态。恢复健康连接保留当前普通页面；深睡重启回NOW。设备等待/运行计时仅在source和link均fresh时根据本地单调增量推进，陈旧后冻结并标记最后更新。

## 5. 模块接口契约

以下为语义接口名，P0映射为实际C头文件/Python协议，不要求复杂继承层次。

| 模块 | 输入→输出 | 线程/生命周期 |
|---|---|---|
| SourceAdapter | 原始事件→NormalizedEvent；能力矩阵 | 不直接调用UI/transport；只输出脱敏最小字段 |
| StateEngine | reduce(event, monotonic_now)→AppState | 单owner串行处理；网络输出与reducer分离 |
| Transport | start(config), stop(), send(bytes,len), on_message(bytes,len), on_link(status) | 只回调完整包；send返回accepted/busy/error；无业务解析 |
| StateStore | apply_json(bytes,len)→applied/ignored/error；snapshot() | 先完整校验再原子替换；不暴露悬垂解析器内存 |
| Presenter | present(state,runtime,now)→ViewModel | 纯转换，可在PC运行 |
| UI | ui_init(display)、ui_apply(view)、ui_key(event) | 只在LVGL所属任务操作对象；无网络/ADC调用 |
| Display HAL | flush(frame,dirty_area,completion)、set_mode(mode) | completion只一次；DMA完成后才释放缓冲；错误也需回收 |
| Battery HAL | read_sample()→mv/valid/error | 统一校准；不得有无界阻塞 |
| Power FSM | step(sample,event,now)→actions | 纯逻辑；由IDF adapter执行关无线/睡眠等动作 |
| Clock/Input | now_monotonic_ms、KEY事件 | PC虚拟、设备真实；UI无需知道宿主 |

Transport回调不调用LVGL。解析任务将最新已验证快照交给UI任务，容量1的普通更新槽覆盖旧普通快照；NEEDS YOU/ERROR转换另保留一个有界优先事件槽并先呈现后续终态，避免慢链路吞掉提醒。超过预算时必须计数并重同步，不无限排队。本地低压事件不经过网络队列。

共享逻辑帧格式：400宽×300高、1bit/pixel、每行50字节、行优先、字节内MSB对应左像素、1=黑/0=白；共15000字节。该格式用于测试/SDL输出；ST7305适配器单独转换实际控制器的列组、地址窗口和位序。flush支持整帧，dirty_area是后续优化提示，v1可忽略并刷整帧。

## 6. Wi-Fi 适配

- ESP32为WSS客户端，Bridge为服务端；路径建议`/v1/state`，认证后立即发送完整快照。首版不额外实现HTTP轮询/多个usage端点。
- 本机Mock仅loopback；真机访问时绑定明确LAN接口，设备验证证书或固定公钥/证书指纹，禁止生产配置关闭证书验证。使用独立设备token，不使用Codex凭证；凭证由本地配置存储，忽略入Git。
- 每个WebSocket text message是一份完整AppState；WebSocket库处理底层分片，仍限制聚合后≤16KiB。使用成熟TLS/WebSocket实现，不自制加密。
- 连接成功清理旧链路的未完成消息，并从握手确认的epoch开始；同epoch重连保留last_seq，Bridge后续发送必须更新seq。
- 退避1/2/4/8/16/30s+抖动；鉴权/证书失败显示配置错误，不高频无限重试。主机恢复后请求/接收最新全量。

## 7. BLE 适配

- ESP32为GATT peripheral，Mac/PC Bridge为central。P0.5统一分配并提交固定128bit服务UUID和特征UUID：RX（central写入）、TX（设备通知ACK/状态）；不可由两个Agent各随机生成。
- 优先LE Secure Connections绑定，并通过可显示/按键确认的配对流程验证身份；加密绑定后才能接受业务数据。macOS权限拒绝/蓝牙关闭要给出可操作错误；若平台配对能力不能满足此方案，P0记录并修订认证设计后再部署，不能默默退为公开写入。
- 以协商ATT MTU计算每次可写长度；默认MTU23时ATT有效载荷20字节仍需可工作。不得假定能一次写16KiB。

建议冻结以下二进制帧头（小端，总16字节；P0如需修改须同步全部实现）：

| 字段 | 字节数 | 语义 |
|---|---:|---|
| version | 1 | 帧版本1 |
| type | 1 | DATA=1、ACK=2、NACK=3 |
| message_id | 4 | 本次连接内递增，重连清空接收上下文 |
| fragment_index | 2 | 从0开始 |
| fragment_count | 2 | 总片数，≥1 |
| total_len | 2 | 完整JSON字节数≤16384 |
| crc32 | 4 | 完整JSON的CRC32；只检传输错误，不替代认证 |

片有效载荷=`ATT_MTU−3−16`；要求>0。最大片数4096（MTU23时每片4字节，16KiB最多4096片）。所有片的count/len/CRC须一致；根据固定片容量计算offset，最后一片允许短片；越界/矛盾头拒绝。CRC算法冻结为标准IEEE CRC32、与Python zlib.crc32一致，并提供固定测试向量。

一次只重组1条消息，buffer上限16KiB，另有接收片位图；重复相同片忽略，不同数据的同序号片拒绝整包。3秒无分片进展丢弃；完整消息最长30秒（P0小MTU实测可调整）；断连立刻清空半包。

完成全部分片→CRC正确→JSON合法→StateStore接受后ACK。ACK回传相同message_id/header元数据、无payload；NACK用相同头、无payload表示需重发全包。收到已经应用的同epoch同seq全包应ACK但不再渲染。发送端限一次在途、最多2次重试；仍失败则重连/重新同步最新快照，旧普通状态可被最新状态替代。16bit total_len足够16KiB，不把字节数塞进8bit字段。

最小MTU吞吐可能不达2秒目标，须测量并在正常连接协商更大MTU/裁剪快照；协议正确性不能靠大MTU掩盖。记录正常MTU与最坏MTU两种指标。

## 8. 生产与测试边界、兼容性验收

- AppState只含摘要，不含ChatGPT cookie、access token、API key、完整工具输出或环境变量。命令摘要应脱敏；设备不用任何字段执行shell。
- BLE/Wi-Fi使用相同JSON字节fixtures；hash一致，渲染也一致。设备配置切换transport须stop旧适配器再start新适配器。
- Mock编译/启动显式标记，屏幕或日志可识别source=mock；生产固件禁用battery_sample输入。真机低压测试经电源/ADC进行。
- v1只发全量，不实现delta。新增可选字段允许旧端忽略；删除/改义/改类型需要主版本升级和协调部署。
- v1.2 可选增补（threads[].model / threads[].tokens / threads[].branch，2026-09-12，branch 为 ZC8 增补）：schema_version 保持 1，向后兼容——旧端按"未知附加字段一律允许"忽略（原样），旧桥+新固件=字段缺失渲染 "--"，新桥+旧固件=旧固件忽略新字段；新端对缺失字段显示 "--"，不编造。
- P0交付schema、C类型、示例及共同测试向量；P1 parser与Bridge encoder均须通过。P3验证两个transport输出同一state，P5验证远端无法解除本地critical。

## 9. P0 冻结清单

- [x] AppState/Telemetry schema与本文示例一致，所有长度/深度由两端执行。（P0.4 已冻结，2026-09-10；schema=protocol/*.schema.json，C 类型=shared/state/codex_state.h，fixtures=tests/fixtures/protocol/ 15/15）
- [x] 选定IDF、LVGL、SDL、Bridge依赖版本及官方板级示例commit。（IDF v5.5.5 / LVGL v9.3.0 / SDL2 2.30.12 / Bleak 3.0.2 / websockets 17.1，见 docs/VERSIONS.md）
- [x] 本机app-server能力矩阵与事件命名已核验；不可观察字段使用unknown。（P0.2 完成：桌面运行时不可旁听→真实桌面集成记为阻塞，P3.6 保持 blocked；额度/bridge-owned 事件流可用，见 docs/CODEX_CAPABILITIES.md）
- [x] BLE UUID、帧头、CRC测试向量、macOS配对流程确定。（P0.5 冻结，见 protocol/transport.md：UUIDv5 定值、16 字节帧头偏移实测、CRC 4 向量、SPKI pinning+加密配对流程）
- [x] Wi-Fi证书/设备token安全配置和开发loopback流程确定。（P0.5：自签 CA+SPKI SHA-256 pinning、Bearer 设备 token 独立于 Codex 凭证、401/403/证书失败=CONFIG_ERROR 不重试，见 protocol/transport.md）
- [x] 电源参数、实际ADC分压、KEY/USB可用唤醒路径明确。（P0.3 完成：GPIO4=ADC1_CH3 ×3 分压 confirmed；KEY 深睡唤醒 unverified 保底 PWR 上电；USB 检测按"无"设计，见 docs/HARDWARE.md §7 十项清单）
- [ ] 普通/优先队列、内存预算、缓冲ownership以及UI单线程边界一致。（P1.4/P2 落实后勾选）
