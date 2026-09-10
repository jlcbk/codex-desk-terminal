# Codex Desk Terminal 开发计划

版本：计划 v1，2026-09-10。本文的阈值、耗时、指标标有“初值/目标”时是项目设计值，不是厂家承诺或已测结果。当前所有开发任务均未开始。

## 1. 目标、范围与不可变约束

交付一个电池供电的 400×300 单色桌面终端，显示本机 Codex 状态。先用 Mac/PC 模拟器完整验证 UI 和状态，再复用同一套代码移植真机。

| 项目 | v1 必须交付 |
|---|---|
| 数据链路 | Codex app-server → Bridge/State Engine → 可替换 BLE/Wi-Fi → ESP32 |
| GUI | LVGL 9.3.0，SDL2 400×300 模拟器与 ST7305 共用 State/UI 源码 |
| 页面 | NOW / AGENTS / PLAN / USAGE；LOW BATTERY 为设备强制页面；断连与错误有可见提示 |
| 状态 | IDLE、THINKING、WORKING、NEEDS YOU、DONE、ERROR；PLAN UPDATE 是内容事件，不增加业务状态 |
| 输入 | KEY 短按轮换四页；长按本地 acknowledge/mute；BOOT 保留下载用途 |
| 电池 | GPIO4 BAT_ADC，电压始终可查看；百分比仅为可用电量估算；3.600V=0% |
| 电源 | 持续低压 Deep Sleep；Light Sleep、唤醒和重连状态机；整板功耗实测 |
| 传输 | BLE 和 Wi-Fi 均可运行，同一业务快照；可配置切换；同条件功耗对比 |
| 自动验证 | Mock 生命周期、事件回放、协议校验、自动截图、UI regression、真机验收记录 |

暂不纳入：远程批准/执行 Codex 命令、语音助手、麦克风、产品内电流测量硬件、精确库仑计、OTA、云服务、多终端管理、复杂动画。蜂鸣提示可后续补充，首版以屏幕提醒和静音状态为准。已有对话中的图片不作为像素真源：当前未获取原始图片资产，先按页面要求生成可审核 golden。

## 2. 技术事实与 P0 必须排除的风险

### 2.1 app-server 不等于已开放的桌面全局事件端口

官方文档描述 app-server 的结构化会话接口、初始化与事件机制；这不足以证明另起一个进程能旁听正在运行的桌面任务。P0 必须分别验证：读取历史、读取当前状态、订阅实时事件、读取额度、审批等待是否可观察。使用本机锁定版本生成协议并保存脱敏样本。[官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)

若只能观察由 Bridge 自己启动/管理的会话，这仅能证明受控会话链路；不能宣布“已连接当前桌面 Codex”。在可行性报告列出限制和下一步，保持 Mock 与硬件工作继续，真实桌面集成验收保持阻塞。不要通过 resume/start 悄悄改变用户正在运行的任务。

### 2.2 硬件、电池与显示

Waveshare 将此板列为 ESP32-S3、反射 LCD，带 KEY、BOOT、PWR 和电池座。官方配置示例给出 ST7305 400×300、GPIO4 电池检测以及三倍分压。实施时仍需核对实际 PCB 修订、原理图和示例，不把网页引脚当作所有批次保证。[板卡资料](https://docs.waveshare.com/ESP32-S3-RLCD-4.2) · [显示与 ADC 配置示例](https://docs.waveshare.com/ESP32-ESPHome-Tutorials/Example-RLCD-Voice)

RLCD 不是断电保图的电子纸。MCU 停止刷新后能否保留 LOW BATTERY，取决于 LCD 供电和控制器模式；P4/P5 必须实测。显示保持若显著增加消耗，优先电池保护，允许最后显示提示后关闭屏幕，并在操作说明中写明。

3.600V 是用户选择的设备可用范围下限，不是电芯化学 SOC=0，也不能代替电池硬件保护。保留校准参数；不沿用官方例子的 2.5V 百分比下限。

### 2.3 睡眠与无线

显式 Light Sleep/Deep Sleep 不能默认保持无线连接；要保持连接须按所选 IDF/无线栈验证 modem-sleep 与自动 Light Sleep。Deep Sleep 后按重新启动处理，不复用失效网络或 RAM 指针。[ESP32-S3 睡眠文档](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/system/sleep_modes.html)

不要把芯片数据手册的 µA 数字写成整板耗电，不提前承诺 BLE 一定比 Wi-Fi 省电或续航数周。

## 3. 架构与职责

```text
Mac/PC
  app-server adapter ─┐
  mock/replay source ─┴→ State Engine → 有界快照队列 → Transport sender
                                      │                   │ BLE / Wi-Fi
                                      ▼                   ▼
                                SDL 注入适配器       Device receiver
                                      └──────────┬────────┘
                                          State decoder/store
                                                 │
  本地时钟、KEY、ADC → DeviceRuntime → Presenter → 共享 LVGL UI
                         │                          │
                      Power FSM          公共单色转换/逻辑帧缓冲
                         │                          │
                    Board power HAL           SDL / ST7305 flush
```

Bridge 负责 Codex 语义、事件聚合、裁剪和脱敏；固件只理解终端协议。DeviceRuntime 负责电池、连接新鲜度、当前页及静音。Presenter 合并远端快照和本地运行状态；低压优先权不可被远端覆盖。Transport 只移动完整消息，重连总是取得全量快照。

推荐 Bridge 使用 Python 3.11+，标准库承担 JSON、Mock、回放和纯状态转换；BLE central 可在实施时验证后引入 Bleak，Wi-Fi WSS 用一个成熟 WebSocket 库。P0 锁定可安装版本与 macOS 权限流程。固件/共享 UI 使用 C，ESP-IDF 组件化、CMake 构建；PC 复用可移植 C 模块。不要为共享语言另造 RPC 或引入第二套 GUI。

## 4. Agent 分工与并行边界

下表是建议角色，不要求同时启动全部角色。只有 3–4 个 Agent 时可轮换执行；本次仅创建计划，不启动固件开发任务。

| Agent | 主责与写入范围（待实施创建） | 输入 | 禁止跨界 |
|---|---|---|---|
| A0 集成/契约负责人 | docs、protocol、顶层构建、版本锁、集成状态 | 用户要求、各组证据 | 不把未测任务标完成 |
| A1 Bridge/State | bridge、tests/bridge、脱敏事件 fixtures | 冻结 schema 与能力探针 | 不改设备电池逻辑/页面 |
| A2 共享 UI/模拟器 | shared/ui、shared/presenter、simulator、字体 | State/Runtime 契约、固定 fixtures | 不调用网络/ESP-IDF |
| A3 板级/功耗 | firmware/components/board、display_st7305、power、battery | 引脚表、帧缓冲与电源接口 | 不自行修改 UI/上游事件 |
| A4 Transport | bridge/transports、firmware/components/transport、传输测试 | 完整消息契约、版本与限制 | 不解释业务状态、不写 UI |
| A5 测试/回归 | tests/ui、tests/integration、scripts 测试入口、证据 | fixtures 与 build 接口 | 不自行更新 golden 掩盖回归 |

并行碰撞约束：A1 不写 A4 的 Bridge transport 目录；A2 提出字体/渲染配置变更由 A0 合并公共配置；A3 提供完整帧 HAL 后独立推进 ST7305；A5 可在模拟器就绪前准备场景与断言，但 golden 必须来自实际渲染。

接口变更流程：owner 提出字段/函数及兼容影响 → A0 更新契约和版本 → 消费方迁移 → 联合测试 → 合并。没有接口冻结，不允许两个 Agent 各造一种 State JSON。

## 5. 阶段、子任务、依赖与验收

任务状态使用 todo / doing / blocked / review / done。每项 done 都要附本地可复现证据。人日估计只用于排期，不代表 Agent 墙钟耗时；硬件到货、仪器和上游权限单独计入等待。

### P0：事实确认与契约冻结（约 1–2 人日）

| ID / Owner | 子任务与交付 | 依赖 | 验收 |
|---|---|---|---|
| P0.1 / A0 | 建立 Git、版本清单、构建宿主信息；锁 LVGL 9.3.0 tag/commit、SDL2、字体；IDF 选厂家示例兼容版本后锁定 | 无 | 版本清单可追溯，不使用 latest 浮动依赖 |
| P0.2 / A1 | app-server 能力探针：初始化、只读任务与额度、实时事件归属；生成本机协议；记录脱敏样本 | 无 | 矩阵分别说明 desktop-observed / bridge-owned / unsupported；证据可重放 |
| P0.3 / A3 | 原理图/PCB 修订/官方 ST7305 示例审计；BAT_ADC、LCD、KEY、电源、USB 检测、RTC 唤醒引脚表 | 无 | 每个引脚有来源；未确认项明确标注，不能猜测烧录 |
| P0.4 / A0 | 将 INTERFACES 草案落成 JSON Schema、C 类型、合法/非法 fixtures | P0.2 的已知能力 | schema 校验样本；确认 null/unknown、上限、序号和本地电池边界 |
| P0.5 / A4 | macOS BLE central + ESP32 peripheral/WSS 技术小样计划；锁定传输头、服务 UUID、证书配置流程 | P0.4 | 两种适配器能引用同一接口；权限/依赖列表明确 |

P0 Gate：协议冻结；若桌面实时源不可接，记录为真实集成阻塞，P1/P2/P4仍可推进。不得以 Mock 掩盖阻塞。

### P1：State、Mock 与模拟器基础（约 2–3 人日）

| ID / Owner | 子任务与交付 | 依赖 | 验收 |
|---|---|---|---|
| P1.1 / A1 | 纯 reducer；按 thread/turn 隔离；排序、选中任务、计划与额度转换 | P0.4 | 相同事件序列给相同快照；旧 turn 不污染新 turn |
| P1.2 / A1 | Mock Bridge 与 JSONL replay；固定时钟、确定性 seq；完整演示场景 | P1.1 | WORKING→PLAN UPDATE→NEEDS YOU→DONE；电池 trace 独立驱动 LOW BATTERY |
| P1.3 / A2 | SDL2+LVGL9.3.0 CMake 构建；400×300 窗口、键盘模拟 KEY、软件渲染 | P0.1 | Mac 能打开窗口、退出无崩溃；CI 宿主可离屏执行 |
| P1.4 / A0 | 共享解码/store/runtime/单色帧接口的最小实现 | P0.4 | 有界解析、整包替换；拒绝坏 JSON/超大包，保留最后合法状态 |
| P1.5 / A5 | 定义测试清单、输出目录和回放命令契约 | P0.4 | 合法/非法/边界 fixture 编号全部可映射到验收项 |

P1 Gate：Mock State 可注入 SDL，无硬件依赖；共享模块无 ESP-IDF/SDL 头文件泄漏。

### P2：完整页面与自动回归（约 3–4 人日）

| ID / Owner | 子任务与交付 | 依赖 | 验收 |
|---|---|---|---|
| P2.1 / A2 | NOW 六状态、标题栏、项目、时长、计划摘要、电压位置 | P1.3–P1.4 | 状态醒目；长文截断不覆盖；unknown 显示 -- |
| P2.2 / A2 | AGENTS、PLAN、USAGE，KEY 导航和等待提醒 | P2.1 | 空数据、多页和选中任务切换正确；额度按实际窗口命名 |
| P2.3 / A2 | LOW BATTERY 强制页、断连/陈旧覆盖、恢复顺序 | P2.1 | 电池优先于 NEEDS YOU；低压页面不可被普通页切换覆盖 |
| P2.4 / A5 | 固定时钟截图、400×300 单色输出、golden/actual/diff 比较 | P2.1 | 修改一个像素能令测试失败；失败返回非零并保留图 |
| P2.5 / A1+A2+A5 | 完整生命周期回放并自动断言页面/文本/时长 | P1.2、P2.2–P2.4 | 五个阶段均有快照；PLAN UPDATE 保持 WORKING；最终低压页出现 |

P2 Gate：无需刷机即可自动验证所有必需页面；A0 首次审查截图后建立 golden。模拟器是同一 UI 的宿主，不是浏览器重画的替代品。[LVGL 9.3 SDL 驱动](https://lvgl.io/docs/open/9.3/details/integration/driver/sdl)

### P3：真实 Codex 与双 Transport（约 3–5 人日，可与 P2 后段/P4 并行）

| ID / Owner | 子任务与交付 | 依赖 | 验收 |
|---|---|---|---|
| P3.1 / A1 | 锁定版 app-server adapter、初始化/重连/能力检测/脱敏 | P0.2、P1.1 | 真实事件驱动 SDL；真实 source 明确；无认证信息泄漏 |
| P3.2 / A1 | 审批与用户输入 pending 集合、plan、usage、错误、取消映射 | P3.1 | 等待只在明确解除后消除；失败不显示 DONE |
| P3.3 / A4 | Wi-Fi：Bridge WSS server、设备 client；认证、证书验证、快照与重连 | P0.5、P1.4 | 断网重连取得最新快照；拒绝未授权连接及超大帧 |
| P3.4 / A4 | BLE：设备 GATT peripheral、Mac central；配对、分包/重组/ACK/重试 | P0.5、P1.4 | 最小 MTU/大包/断连半包不会破坏 store；重连重发全量 |
| P3.5 / A4+A5 | 共同故障测试：重复、乱序、超时、Bridge 重启、切换 transport | P3.3–P3.4 | UI 不回退；一次仅一个活动 transport；连接状态可观测 |
| P3.6 / A1+A5 | 当前桌面任务端到端验收 | P3.1–P3.2 | 用户在桌面推进的任务可观察；若仅受控会话可用，该项保持 blocked |

先用 loopback/mock receiver 测传输语义，再接真机。A4 可先完成 Wi-Fi 后 BLE；有额外 Agent 时拆为 A4-W/A4-B，仅 A0 修改公共头。Wi-Fi 与 BLE 不要求同时保持连接，避免无意增加功耗。

### P4：真机显示、电池与输入（约 2–4 人日）

| ID / Owner | 子任务与交付 | 依赖 | 验收 |
|---|---|---|---|
| P4.1 / A3 | 官方板级最小示例复现、供电/PSRAM/Flash 配置、可恢复烧录流程 | P0.1、P0.3 | 输出板卡修订与启动日志；可复现干净构建 |
| P4.2 / A3 | ST7305 init/flush、旋转、位序/stride/对齐、忙状态/超时 | P4.1、P1.4 | 四角标记、棋盘、横竖线、文字方向正确，无花屏 |
| P4.3 / A2+A3 | 共享 UI 接入；全帧先行，实测后再考虑局部刷新 | P2 Gate、P4.2 | 与单色 golden 内容一致；记录照片及 frame hash |
| P4.4 / A3 | GPIO4 ADC oneshot、校准、分压系数/偏置、批量采样、故障检测 | P0.3、P4.1 | 3.5/3.6/3.7/4.0/4.2V 对照外部表计，建议误差 ≤30mV；超出需校准或阻塞低压发布 |
| P4.5 / A3 | KEY 去抖、长短按；USB/充电状态只有可检测才上报 | P0.3、P4.1 | 单次按键仅一动作；未知充电态显示 unknown，不根据高电压猜充电 |

P4 Gate：真实屏、键、电池电压成立；校准数据落 NVS 的稳定配置区，ADC 每次采样不写 Flash。此处不要求假装可测电流。

### P5：低功耗和低压保护（约 3–5 人日）

| ID / Owner | 子任务与交付 | 依赖 | 验收 |
|---|---|---|---|
| P5.1 / A3 | 可在 PC 测试的纯 Power FSM、注入式电压与单调时钟 | P1.4、P4.4 | 第 7 节边界/持续低压/反弹轨迹全部通过 |
| P5.2 / A3+A4 | 无线空闲策略、PM 锁、自动 Light Sleep；显式离线睡眠与重连 | P3.3–P3.4、P5.1 | 无 flush 中途睡眠；连接维护或重建行为与所选模式一致 |
| P5.3 / A3 | CRITICAL→最后一帧→外设关闭→Deep Sleep；唤醒门禁 | P4.3、P5.1 | 低压启动不开无线；低压反弹不反复启动；显示失败也可睡眠 |
| P5.4 / A3+A5 | 整板休眠测量、LCD 保持/关闭对照、USB 拔插与 KEY 唤醒实测 | P5.2–P5.3 | 报告给出电池侧平均/峰值与仪器；真实唤醒来源已验证 |

P5 Gate：3.600V 策略在断连和网络繁忙时仍生效。Deep Sleep 电流目标先定 ≤100µA（整板电池端、USB 断开）；若硬件固定消耗导致未达标，必须分项测量、记录可达到值与原因，不能以芯片规格替代通过。

### P6：BLE/Wi-Fi 对比、稳定性与交接（约 2–3 人日，另加 24h soak）

| ID / Owner | 子任务与交付 | 依赖 | 验收 |
|---|---|---|---|
| P6.1 / A3+A4 | 按第 9 节同条件 A/B 实验，形成 CSV、曲线和默认选择结论 | P5 Gate | 两种传输均有至少 3 次同脚本结果；有耗电也有延迟 |
| P6.2 / A5 | 24h 运行、100 次断连重连、模拟主机睡眠/唤醒、重启 | P3、P5 | 无崩溃/死锁/持续内存增长；状态最终收敛；结果可追溯 |
| P6.3 / A0 | README 使用指南、版本/固件哈希、烧录/配对/校准/恢复流程 | 全部 Gate | 新 Agent 仅按文档可运行模拟器并部署到板卡 |
| P6.4 / A0 | 最终需求覆盖审计 | P6.1–P6.3 | 必需项逐项有证据；未通过项显式列出，不发全完成声明 |

最短关键路径：P0.4 → P1 → P2 → P4.3 → P5 → P6。真实 app-server 是另一条发布关键路径：P0.2 → P3.1 → P3.6。板级资料审计与协议可并行；UI 不必等 BLE；功耗对比必须等两条真机链路均稳定。

## 6. UI 规格与可读性验收

400×300 逻辑坐标、仅黑白最终输出，建议外边距 8px、标题栏 28px、底栏 24px；主状态 28–36px、正文 16–20px 为起始设计值，按真机观看结果调整。不要逐秒动画，运行时长按页面需求每 10–30 秒更新；紧急状态立即更新。

| 页面 | 内容与行为 | 边界要求 |
|---|---|---|
| NOW | 项目、六状态之一、当前活动摘要、持续时间、计划摘要、额度摘要、电压 | 无任务 IDLE；取消显示 IDLE+已取消；错误文本裁剪；等待状态黑白粗框 |
| AGENTS | NEEDS YOU→ERROR→WORKING/THINKING→DONE→IDLE 排序；最多 4 行/页 | 同优先级按更新时间降序、ID 打破平局；显示总数/裁剪标记；按选中任务生成其他页 |
| PLAN | 当前任务的步骤、完成数、当前步骤标记 | 只数 completed；长计划分页；新 turn 清旧计划；无数据显示“暂无计划” |
| USAGE | 实际窗口长度、usedPercent、reset 倒计时、上下文（若可得） | 不硬编码 5h/周；额度缺失 --；token 总量不能冒充 context；已过 reset 不猜 0% |
| LOW BATTERY | 电压、设备可用电量 0%、低压提示、充电/唤醒说明 | 保护优先；不要求睡眠后持续倒计时；USB 自动唤醒未证实前不承诺“插线即恢复” |

KEY 初值：去抖 30ms，长按 800ms；在松开时判定，长按不得再触发短按。短按 NOW→AGENTS→PLAN→USAGE；AGENTS/PLAN 超一页时可先推进子页再切换下一主页面，P2 固定这一行为并测试。长按只静音当前提醒；新 pending request 可再次提醒。所有图标具备文字标签，不依赖颜色。

项目名/摘要允许中文；锁定有授权的字体和常用 CJK 覆盖策略，未知字符用可见替代符。增加真实中文、混合英文、长路径、emoji 的测试；换行/省略不能切断 UTF-8。较大的 CJK 字库若超出预算，优先裁减字库并记录可用字符范围，不静默缺字。

## 7. 电池采样、映射与电源状态机

### 7.1 电压是设备本地真源

公式：`battery_mv = calibrated_adc_mv × divider_ratio × gain + offset_mv`。三倍分压为待本板核验的默认值；增益默认 1、偏置默认 0。用 IDF 支持的 ADC 校准方案和合适量程覆盖分压后约 1.2–1.4V；确认 GPIO4 对应 ADC 单元/通道。高阻分压的稳定时间与采样误差需实测。

初始算法：每秒采样一批 9 次，取中位数；在接近阈值时持续 1Hz 检查。正常高电量可每 10 秒采样，接近 ≤3.75V 转入 1Hz。显示用平滑值，保护使用当前有效批次中位数，不能让长时间平均值推迟保护。无线发射产生低值时尽快补测安静窗口，但不得因网络一直繁忙而无限延后。

电压有效范围初值 2500–4500mV；范围外/驱动失败标记 unknown。连续 3 次失败进入 BATTERY_FAULT：关闭高功耗活动并提示；10 秒仍不可恢复则走受控休眠（没有已验证的外部供电时），避免把 unknown 当满电无限运行。

可用电量初期只做单调线性估算 `clamp((mv-3600)/600×100,0,100)`，标注估算；在真实电芯放电曲线可得后替换为单调查表。硬约束为 ≤3600mV →0%、≥4200mV →100%；不要把旧对话的中间百分比当成测量值。UI 优先显示电压。

### 7.2 初始参数（集中配置，可校准）

| 参数 | 初值 | 含义 |
|---|---:|---|
| low_enter_mv | 3700 | 低电量警告 |
| low_exit_mv | 3750 | 警告解除，连续稳定 10 秒 |
| critical_mv | 3600 | ≤此值计持续低压时间 |
| critical_hold_ms | 30000 | 连续有效 1Hz 样本低压，30 秒后必须准备睡眠 |
| recovery_mv | 3700 | Deep Sleep 后允许恢复正常的门槛，稳定 10 秒 |
| final_frame_timeout_ms | 1000 | 末帧显示最多等待 1 秒，失败也要休眠 |
| reconnect_backoff | 1/2/4/8/16/30s + jitter | 无线断连退避；低压时取消 |

连续定义：任一有效样本 >3600mV 重置 critical 计时；连续有效样本间隔须 ≤2 秒，间隔超限转入采样故障检查，不能把缺测时间算作持续低压。30 秒条件与阈值都用单调时钟和毫伏整数测试。进入 CRITICAL 后锁存决定，不能因卸载后电压反弹立即撤销。3.55V 不作为必须等到的第二截止点；若以后增加快速保护线，须单独定义更短持续时间并校准，不将“两条下降阈值”误称为恢复迟滞。

### 7.3 状态转移

| 状态 | 进入/退出 | 动作 |
|---|---|---|
| BOOT_CHECK | 开机/深睡唤醒必经；正常启动健康电压可运行，低压唤醒要求 recovery 条件 | 先 ADC 后无线；取 RTC 保留的低压原因；无效读数走故障策略 |
| ACTIVE | 有输入/新状态/待 flush | 处理更新；持有必要 PM 锁；结束后释放 |
| CONNECTED_IDLE | 无脏画面/无任务 | 验证后的 modem-sleep+自动 Light Sleep；心跳保持，不主动高频刷新 |
| OFFLINE_LIGHT_SLEEP | 主动离线节能策略，关闭无线 | 定时/按键唤醒后重连拿全量；不能标成持续在线 |
| LOW_WARN | ≤3700mV，但尚未 critical 持续成立 | 电压警告、快速采样；业务继续；退出需 3750mV 稳定 |
| CRITICAL | ≤3600mV 连续30秒，或电池故障安全路径 | 抢占普通任务、取消重连、冻结末帧、进入 SLEEP_PREP |
| SLEEP_PREP | CRITICAL 后 | LOW BATTERY/故障页；等待 flush≤1s；关闭音频/无线/外设，设置经验证唤醒源 |
| DEEP_SLEEP | 末帧完成或超时后；正常情况下 critical 成立后2s内进入 | 保留最小原因；无定时联网；只通过经验证的 KEY/USB/电源重启路径恢复 |

低压 Deep Sleep 默认不周期唤醒，以免慢性耗电；若 KEY 不是可用 RTC 唤醒脚，P0.3 必须确定真实替代路径（PWR 重新上电等）。不要在不可用 GPIO 上配置“理论唤醒”。USB 检测不明时明确告知插线后需按电源键。若确有外部供电状态，是否允许 USB 下运行由 A3 单独确认供电路径；未确认前不豁免低压策略。

停止顺序需在真机验证：阻止新更新 → 完成/放弃末帧 → 停止 transport/无线 → 关闭不需要的音频、传感器、SD 等 → 配置 LCD 保持或关闭 → GPIO 安全电平/避免反向供电 → 配置唤醒 → Deep Sleep。NVS 不保存每次快照；低压原因优先 RTC 数据，不能因写盘失败无限等待。

## 8. 测试策略与验收指标

目标初值：Mock 注入至 framebuffer p95≤200ms；真机已连接状态事件至刷新 p95≤2s；主机恢复可用后30s内重连成功（测试环境内）；这些指标需记录测量边界，不含未测网络环境。普通更新合并到最高1Hz；NEEDS YOU/ERROR 在队列里保留优先级，低压本地事件直接抢占。

| 测试层 | 必测案例 | 方法与证据 |
|---|---|---|
| State/reducer | 新 turn、重复 completion、迟到事件、多 pending、错误/取消、无计划/额度 | 固定事件JSONL → 期望State；纯单元检查 |
| 协议 | null/缺字段/未知版本、超长字符串/数组、非法 UTF-8、超大JSON/嵌套 | schema+设备有界解析；拒绝后状态不变 |
| 传输 | 最小MTU、缺片/重复片/乱序/CRC坏、断连中包、旧epoch、重启 | mock链路故障注入；BLE/Wi-Fi共同收敛断言 |
| UI | 六状态、四页、低压、陈旧、多Agent、长计划、中文、0/100/unknown | 原尺寸单色golden；语义断言+像素比较 |
| Power FSM | 3601/3600/3599mV、29.999/30s、短低压后恢复、反弹、缺测、ADC错误 | 虚拟单调时钟+电压trace；无需等待真实30s |
| 真机 | 字节位序/方向、ADC校准、按键、低压冷启动、flush失败、USB拔插、深睡唤醒 | 表计/可编程电源、照片、串口日志、波形 |
| 稳定性 | 24h、100次重连、主机睡眠、无线权限拒绝、Bridge重启、证书失效 | 计数、最低空闲堆/峰值内存、失败日志 |

截图实现：固定软件字体、DPI、LVGL 版本、随机种子、输入和单调时钟；禁用真实墙钟、光标闪烁和动画。导出完整 display framebuffer 经公共单色转换后的逻辑400×300图，而非桌面窗口截图；若使用 LVGL snapshot，先验证它与最终 flush 输出一致。[LVGL 9.3 snapshot API](https://lvgl.io/docs/open/9.3/API/others/snapshot/lv_snapshot)

固定 CI 宿主上默认像素零差异；跨系统差异先统一字体/软件渲染，不用大容差放过布局问题。输出 actual.png、expected.png、diff.png 和差异像素数；同时检查关键控件坐标不越界、文本存在、页面优先级。A0审核 deliberate UI变化才更新golden；禁止CI自动接受。

最小场景集合：idle、thinking、working、plan_update、needs_you、done、error、cancelled、low_battery、battery_unknown、disconnected、stale、multi_agents、empty_plan、long_plan、long_project_name、unicode、usage_missing、usage_0、usage_100、bridge_restart。生命周期须检查中间每一步，不只最后一帧。

以下是实施时需要提供的命令接口，**目前尚不存在**；A0 可调整最终名称，但必须同步 README 与 CI：

```sh
python -m bridge --source mock --scenario lifecycle
cmake -S simulator -B build/simulator
cmake --build build/simulator
./build/simulator/codex-display-sim --scenario tests/fixtures/lifecycle.jsonl --fixed-clock --capture-dir artifacts/ui
python scripts/check_ui.py --golden tests/golden --actual artifacts/ui
idf.py -C firmware build
```

测试产物写 artifacts/（Git忽略），黄金图写 tests/golden/（受版本控制）；功耗原始CSV和发布验收报告保留版本/日期/仪器信息。

## 9. BLE 与 Wi-Fi 真机功耗实验

不增加产品传感器。外部电流分析仪或电源+表计在电池输入端测整板；正常测量断开USB，避免充电/旁路影响读数。模拟电池供电测试须避免与真实电池并联或反向灌电，供电方式按板卡电路确认。

固定条件：同一块板/固件commit、3.8V供电（另测4.2/3.6V）、同一400×300内容和刷新策略、相同距离/射频环境/事件脚本；音频关闭；记录CPU频率、PSRAM、LCD模式、BLE连接间隔/MTU、Wi-Fi省电模式/RSSI及AP。BLE和Wi-Fi轮换测试顺序，每组预热2分钟后测至少10分钟、重复3次。

| 场景 | 两种传输都执行 |
|---|---|
| 在线静止 | 10分钟无业务更新，只保留一致的存活要求 |
| 常规工作 | 每10秒一条相同大小的快照 |
| 密集工作 | 1Hz更新5分钟，检查合并和峰值 |
| 等待/完成 | 同一NEEDS YOU→DONE序列，测响应与能耗 |
| 断连 | 主机失联10分钟，测广播/扫描/退避成本 |
| 恢复 | 主机唤醒、重连、大快照；测恢复能量和延迟 |
| 深睡 | 同样的LCD保持/关闭方案，至少30分钟，USB断开 |

CSV列：run_id、commit、board_rev、transport、scenario、supply_mv、duration_s、mean_ma、peak_ma、energy_mwh、event_latency_p50_ms、event_latency_p95_ms、reconnect_s、lost_states、lcd_mode、instrument、sample_rate、notes。采样带宽须覆盖无线峰值；设备日志只辅助计时，不用ADC电压反推电流。

计算：平均电流用积分电荷/时间；能量用电压×电流积分；单次刷新/重连可报增量能量。续航只能用“实测3.6V以上可用容量/场景加权平均电流”估算，报告工作/空闲/离线权重；不能直接用电芯标称容量假设全部可用。

选择规则：先达到可靠性与延迟指标，再比较日场景加权能耗。差异在测量误差内时保留两者、按连接便利性选默认，不宣称节电胜出。结论写 docs/POWER_REPORT.md，含原始数据位置、均值/范围、限制和推荐默认配置。

## 10. 推荐目录结构（按任务建立，当前不生成空源码骨架）

```text
codex-desk-terminal/
  README.md
  AGENTS.md
  docs/
    DEVELOPMENT_PLAN.md       # 本计划
    INTERFACES.md             # 本次接口草案
    STATUS.md                 # P0创建，唯一实施状态台账
    VERSIONS.md               # P0锁定依赖和协议生成版本
    HARDWARE.md               # 引脚/原理图/校准/唤醒证据
    CODEX_CAPABILITIES.md     # 桌面观察能力及真实样本结论
    POWER_REPORT.md
  protocol/
    state.schema.json
    telemetry.schema.json
    transport.md             # P0冻结具体UUID/帧头后生成
  shared/
    state/                   # C解码/store，无网络依赖
    presenter/               # AppState+Runtime→UI
    ui/                      # 页面、字体、输入语义
    display/                 # 公共单色转换、帧格式
    power/                   # 可在PC测试的纯FSM
  bridge/
    sources/                 # mock/replay/codex
    state/                   # reducer、裁剪/脱敏
    transports/              # BLE central、WSS server
  simulator/                 # CMake、SDL/时钟/设备注入适配
  firmware/
    main/                    # 组装/事件循环
    components/
      board/
      display_st7305/
      battery/
      power/                 # ESP-IDF执行适配
      transport/
  tests/
    fixtures/                # State、Codex事件、电压trace
    bridge/
    protocol/
    ui/
    integration/
    golden/
  scripts/                   # 构建/截图对比入口，按需建立
  artifacts/                 # 临时测试输出，忽略
```

公共 UI、State、Power FSM 由 CMake 两端引用同一源目录；不复制进入 firmware。LVGL/JSON解析库使用同一锁定依赖来源；IDF与PC构建差异封装在构建适配层。整帧单色逻辑缓冲为15000字节，不代表ST7305线缆打包格式；LVGL绘图缓冲及双缓冲另算并测峰值。初期明确上限，不用PSRAM大小掩盖无限数组和队列。

## 11. 可直接派发的首轮任务

A0 建立 P0.1/P0.4 并冻结协议后发布以下任务书。执行 Agent 完成一个边界内任务即可交付，不擅自扩成整个项目。

| 派发对象 | 任务书内容 |
|---|---|
| A1 | 完成P0.2与P1.1/P1.2；读INTERFACES，写Bridge状态与Mock；输出真实接入能力矩阵、固定回放、测试命令；不改UI/硬件 |
| A2 | 完成P1.3、P2.1–P2.3；只消费冻结State/Runtime；先能运行400×300 SDL，后补页面；提交窗口截图和可重放命令；不改协议 |
| A3 | 完成P0.3、P4.1/P4.2；先记录板卡/引脚证据，再运行显示图案；保留ADC和电源校准入口；不声称已有低功耗验收 |
| A4 | 完成P0.5及P3.3/P3.4；用同一消息测试Wi-Fi/BLE；明确Mac权限、配对、安全配置和丢包恢复；不生成业务JSON |
| A5 | 完成P1.5/P2.4；准备生命周期/边界fixtures与截图差异测试；验证故意改图必失败；不自行认可golden |

首轮可用4个Agent时：A0负责集成，A1 Bridge，A2 UI，A3硬件；P1 Gate后A1转真实Codex，A3完成基础显示后安排Transport角色，A0/空闲角色执行A5。不要为凑并行提前做依赖未冻结的实现。

## 12. 最终完成定义

- 从干净环境按文档构建模拟器和固件；两端确认为LVGL9.3.0，共用UI。
- Mock全流程自动截图通过；四页、六状态、低电量和异常场景都有回归证据。
- 真实本机Codex任务可观察，来源/能力限制公开；额度未知不编造；没有自动审批。
- Wi-Fi与BLE在真机分别运行并通过断连/重启测试；默认选择有功耗和延迟数据支持。
- ST7305显示方向/单色转换/刷新正确；GPIO4电压经表计校准。
- 3.600V持续低压独立触发Deep Sleep，低压重启与反弹不形成耗电循环；低功耗值按整板实测报告。
- 交付包含源码、锁文件、配置示例（无凭证）、构建/烧录/配对/校准/恢复说明和完整验收记录。

## 13. 官方资料索引与使用边界

资料核对日期为2026-09-10；链接随版本可能变化，实施时把所用commit、文档版本和下载校验和记入VERSIONS/HARDWARE。

| 来源 | 用途 |
|---|---|
| [Codex App Server](https://learn.chatgpt.com/docs/app-server) | 初始化、任务/事件、版本化协议与真实能力探针 |
| [Waveshare板卡](https://docs.waveshare.com/ESP32-S3-RLCD-4.2) | 资源、文档入口、核对板卡修订 |
| [Waveshare显示/电池示例](https://docs.waveshare.com/ESP32-ESPHome-Tutorials/Example-RLCD-Voice) | ST7305、GPIO4、三倍分压起点；不能照搬其电量下限 |
| [LVGL 9.3 SDL](https://lvgl.io/docs/open/9.3/details/integration/driver/sdl) | PC驱动接入，项目强制400×300 |
| [LVGL 9.3 Snapshot](https://lvgl.io/docs/open/9.3/API/others/snapshot/lv_snapshot) | 截图API参考；最终以单色显示帧为准 |
| [ESP-IDF ESP32-S3 Sleep](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/system/sleep_modes.html) | 睡眠/无线/唤醒约束；stable页面不作为项目锁版本 |

本文中的任务安排、接口大小、刷新频率、电压持续时间、UI布局和验收门槛是项目设计，不是以上资料已经替项目验证的结论。
