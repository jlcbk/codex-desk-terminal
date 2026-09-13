# Codex Desk Terminal

ESP32-S3-RLCD-4.2 桌面状态终端：一块 400×300 反射屏，放在显示器旁边，抬眼就能看到你的 AI 编码助手正在做什么——工作中 / 思考中 / 等你审批 / 完成 / 出错，以及计划进度、token 用量和额度状态。

> 反射式墨水屏质感、常亮、超低功耗、一个实体按键。数据源可插拔：当前支持 **OpenAI Codex（受控会话）** 与 **ZCode（桌面实时旁听）** 两种工作模式，设备固件完全通用。

## 六屏一览

| # | 页面 | 内容 |
|---|---|---|
| 1 | NOW | 大状态词（● WORKING / THINKING / DONE / ERROR）+ 当前活动 + RUNNING FOR 时长 + CURRENT PLAN 计划面板 + 上下文/额度信息条 |
| 1' | NEEDS YOU（高优覆盖） | 反白警报：`PERMISSION REQUIRED` + 待审批命令独立框（如 `$ git push origin main`）+ 等待计时 + `HOLD KEY = MUTE` |
| 2 | AGENTS | 多会话列表（两行式：项目+活动），needs_you 置顶、行尾耗时、分页与总数 |
| 3 | PLAN | 任务计划清单：编号、完成标记、当前步反白、底部当前步详情面板 |
| 4 | USAGE | 额度窗口（图形进度条 + 重置倒计时）+ CONTEXT / INPUT / OUTPUT / CACHED token 表 |
| 5 | DETAILS | 选中会话明细：PROJECT / BRANCH / MODEL / STATUS / DURATION / CONTEXT / 三项 token 累计 |

另有 LOW BATTERY 强制保护页（Power FSM 驱动，不可被页面导航触达）。

## 两种工作模式：怎么选

**选择发生在电脑上的 Bridge 侧，设备完全不感知**——固件烧一次，两种模式通用。切换模式 = 换启动哪个 Bridge 程序。

| | Codex 模式（受控会话） | ZCode 模式（桌面旁听） |
|---|---|---|
| 适合谁 | 想把任务**交给** AI 后台跑，屏幕看进度 | 想让屏幕**镜像**自己正在进行的 ZCode 会话 |
| 前置条件 | 本机安装并登录 `codex` CLI | 跑一次 hook 安装器（写入 ZCode hooks） |
| 启动命令 | `scripts/bridge_serve_codex.py serve ...` | `scripts/bridge_serve_zcode.py ...` |
| 日常使用 | 用 CLI 把任务 `submit` 给 Bridge，屏幕实时跟踪该任务 | 无需任何操作：正常使用 ZCode，屏幕自动镜像 |
| 数据深度 | 精确事件流（含真实 PLAN 事件与额度双窗口百分比） | hook 相位 + rollout 文件旁听（额度百分比上游不提供，诚实留空） |

两个模式**不建议同时运行**（同一端口同一 Bridge 实例），切换 = 停一个启另一个。

> 说明：Codex 桌面版（ChatGPT Desktop）的会话目前无法被第三方旁听（上游未开放接口，详见 `docs/P3.6_DESKTOP_OBSERVATION.md`），因此 Codex 模式采用"Bridge 自建受控会话"工作流；ZCode 模式则是对本机 ZCode 会话文件的实时只读观察 + hooks 事件触发。

## 架构

```text
┌─ 数据源（电脑侧，按需选择）────────────┐
│ mock 场景（开发/回归）                  │
│ codex app-server adapter（受控会话）    │
│ ZCode 观察器（hooks + rollout JSONL）   │
└──────────────┬───────────────────────┘
               ▼
   Bridge：StateEngine（事件归一化 → AppState 快照，seq/epoch 语义）
               │  WSS（TLS + CA 验链 + Bearer token）
               ▼
   ESP32-S3-RLCD-4.2（ST7305 反射屏，LVGL 9.3.0 同构 UI）
               ├── Power FSM：低压保护 / 深睡 / LCD 保持
               └── KEY：短按翻页 / 长按静音提醒
```

- **协议**：AppState JSON（schema 冻结 v1 + v1.1/v1.2 可选增补），字符串上限/嵌套深度/整包 16384B 由两端解析器强制执行；未知字段前向兼容，未知枚举整包拒绝（fail-closed）。
- **golden 回归**：模拟器逐像素回归（106 帧 / 22 场景零差异）+ 语义断言；固件与模拟器渲染 CRC 逐位一致。
- **安全边界**：凭证不进协议、fixtures、日志；审批摘要脱敏；ZCode 观察器只读（零写入、零网络外联）。

## 硬件

- 主控：ESP32-S3 N16R8（16MB Flash / 8MB PSRAM）
- 屏：Waveshare 4.2" RLCD（ST7305 驱动，400×300）
- 电池：锂电 + ADC 分压采样（GPIO4=ADC1_CH3，×3 分压）；3.600V = 0%
- 按键：BOOT 键复用（短按翻页 / 长按静音 / 深睡唤醒）

引脚分配、分区表、采样参数详见 `docs/HARDWARE.md`。

## 快速开始

### 1. 设备（一次性）

```sh
# 环境：ESP-IDF v5.5.5（版本锁见 docs/VERSIONS.md）
cp firmware/main/dev_net_config.h.template firmware/main/dev_net_config.h
#   填入 7 个宏：WiFi SSID/密码、Bridge IP/端口、设备 token、CA 证书（详见模板注释）
cd firmware && idf.py build && idf.py -p /dev/cu.usbmodem1301 flash monitor
```

### 2. Bridge（电脑侧，二选一）

**ZCode 模式**

```sh
# 一次性：安装 ZCode hooks（自动备份、--uninstall 可撤、与已有 hooks 共存）
python3 scripts/install_zcode_hooks.py --install

# 启动观察服务（证书/token 见 scripts 内 --help 与 docs/HARDWARE.md）
uv run --python 3.12 --with websockets==17.1 python3 scripts/bridge_serve_zcode.py \
  --host <LAN_IP> --port 8765 \
  --cert config/local/dev-certs/server.crt --key config/local/dev-certs/server.key \
  --token-file config/local/device_token
```

同一观察源也可走 BLE（可选，Mac 为 central 推给设备；配对加密即信道安全，无 token/证书）：`uv run --python 3.12 --with bleak==3.0.2 python3 scripts/bridge_serve_ble.py --device-name CodexDT`（真机接入待 P3.4 B2 验证）。

**Codex 模式**

```sh
# 需要 codex CLI 已登录；任务经 unix socket 提交
python3 scripts/bridge_serve_codex.py serve --host <LAN_IP> --port 8765 \
  --cert config/local/dev-certs/server.crt --key config/local/dev-certs/server.key \
  --token-file config/local/device_token
python3 scripts/bridge_serve_codex.py submit --prompt "你的任务"
```

证书签发：`scripts/gen_dev_certs.sh`（自签 CA + SAN，设备侧预置 CA 验链；设备 token 首次烧录时写入配置头文件）。

### 3. 开发与回归

```sh
sh scripts/build_shared.sh            # 共享层单测（State/Power/协议解析）
sh scripts/build_presenter_tests.sh   # Presenter/UI 视图模型单测
uv run --python 3.12 --with pytest --with jsonschema --with websockets \
  python -m pytest tests/bridge -q    # Bridge 全量
python3 scripts/check_protocol.py     # 协议 fixtures + schema 校验（需 jsonschema）
sh simulator/smoke_offscreen.sh       # 模拟器离屏冒烟
uv run --python 3.12 --with pillow python3 scripts/check_ui.py \
  --golden tests/golden --actual artifacts/ui/replay   # 像素回归 22/22
```

## 仓库导览

| 路径 | 内容 |
|---|---|
| `firmware/` | ESP32 固件（main + display/input/battery/power/transport 组件） |
| `bridge/` | 数据源 adapter、StateEngine、WSS 服务 |
| `shared/` | 跨端共享：协议解析、StateStore、Presenter、LVGL UI 页面、Power FSM |
| `simulator/` | SDL 离屏模拟器（golden 回归入口） |
| `protocol/` | 冻结 schema、传输契约、CRC 向量 |
| `tests/` | shared/presenter/bridge/transport 测试 + golden 基线 + fixtures |
| `docs/` | STATUS（实施台账）、INTERFACES（协议契约）、HARDWARE、VERSIONS 等 |

## 当前状态与已知边界

- ✅ 已验证：双源端到端（ZCode 观察源真机实时镜像；Codex 受控会话 live turn 全链）、Wi-Fi/WSS 整机、六屏 UI、低压保护、断连自愈、像素回归体系
- ⏳ 未验证/未实现：BLE 真机联调（组件已编译入固件）、整机功耗实测、Codex 桌面版旁听（上游 blocked）、多源同屏聚合（架构留口，未实现）
- 🔒 诚实显示原则：上游不提供的字段（如 ZCode 额度百分比）界面显示 `--`，绝不编造

## 文档

- 实施状态台账：`docs/STATUS.md`（每个任务的可复现命令与证据）
- 协议契约：`docs/INTERFACES.md`；传输与安全：`protocol/transport.md`
- ZCode 观察设计依据：`docs/P3.6_DESKTOP_OBSERVATION.md`
- 历史版本 README（2026-09-11 草案，含完整测试复跑表）：`docs/README-20260911-draft.md`

## License

MIT（见 LICENSE）。第三方字体/组件遵循其各自许可（见 `third_party/`）。
