# Codex Desk Terminal

ESP32-S3-RLCD-4.2 桌面状态终端：抬眼看见 Codex 在做什么、是否需要处理、计划进度及额度。

> **文档状态：草案 v1.1（P6.3 + 整机章节，2026-09-11，A0 助理起草、待 A0 审定）。**
> 整机 Wi-Fi 实时链路已跑通（§6）；BLE 真机联调、P5.2 省电与功耗实测段**未完成**，
> 相关条目以 ⏳ 标注；其余命令均来自 docs/STATUS.md 的 A0 复跑记录或仓库脚本原文，
> 撰写期抽查复跑记录见文末说明。

## 1. 架构与当前状态

```text
Codex app-server → Bridge / State Engine → BLE 或 Wi-Fi → ESP32-S3-RLCD-4.2
                            ↓                               ↓
                       Mock / Replay                共享 State + LVGL UI
                            └───────────────────────────────┤
                                                 SDL / ST7305
```

固定路线：ESP-IDF、LVGL **9.3.0**（PC 与设备同一份 commit）、SDL2、400×300 横屏、单色输出；模拟器优先。3.600V 定义为设备可用电量 0%，持续低压后进入 Deep Sleep。BLE 与 Wi-Fi 都实现，默认传输由同条件真机测量决定（⏳ 未测）。

| 能力 | 状态 | 依据（详见 docs/STATUS.md） |
|---|---|---|
| 模拟器全链路（构建/渲染/golden 回归/check_ui/soak） | ✅ | P1.3–P2.5、P6.2 done：105 帧 golden 零像素差异、22 场景语义断言、soak 500+400 轮确定性全同 |
| Bridge 三源（mock/replay/codex adapter） | ✅ | P1.1/P1.2/P3.1/P3.2 done：pytest 90 passed；live 快照→C 端交叉验证 33/33（live 端到端老化观察记未验证） |
| 双 Transport loopback（host 层） | ✅ | P3.3–P3.5 done：WSS 54 passed、BLE host 97 PASS+CRC 4/4、集成 16/16 |
| 真机 UI（ST7305 显示 + 串口 CRC 对齐） | ✅ | P4.1 烧录三次全 Hash verified；P4.2 用户目检 2026-09-11 五图案符合；P4.3 固件=模拟器 6/6 CRC 逐位一致（用户目检与照片待补） |
| 整机运行（Wi-Fi→WSS 实时链路：mock 快照收发 + KEY 翻页 + 断连自愈） | ✅ | 整机集成 v1 + 断连重连修复（2026-09-11）：WSS TLS+token+SPKI 首帧 1.3s、快照连续应用 dropped=0、15s 存活快照与电压上屏；KEY 四页轮换+长按静音（用户实测）；3 轮杀/重启断连循环 3/3 自愈 + 10 分钟 soak 7 断连全自愈（证据 `artifacts/board/integration/`，用法见 §6） |
| 真机 BLE 上板（B0-B5） | ⏳ | NimBLE 组件编译进固件但本集成不启动；真机联调全部未验证 |
| 真机功耗实测与默认传输选择 | ⏳ | P5 逻辑（Power FSM）done；BLE/Wi-Fi 对比实验与 docs/POWER_REPORT.md 未开始 |
| 桌面实时旁听（P3.6） | **blocked** | P0.2 结论：桌面运行时为私有 stdio 子进程，无 daemon 可旁听；以真实 codex adapter 替代，不 Mock 掩盖 |

> 已知组件缺陷（esp_websocket_client 1.8.0，均已在固件侧绕过，待上游修复）：
> ① ESP-IDF transport_ws 错误路径明文打印 Authorization 头（验收后轮换 dev token）；
> ② ERROR 事件携带的 esp_tls 指针为未初始化栈值（固件已改为不读该字段、按错误类别分类重连）。

项目路径：`/Users/cui/Documents/Projects/codex-desk-terminal`（下文命令均在此目录执行）。

## 2. 环境准备（macOS arm64，无 Homebrew）

一切版本以 **docs/VERSIONS.md** 的锁定表为准，禁止"最新版"浮动。关键锁定项：LVGL 9.3.0（commit `c033a98a…`）、SDL2 2.30.12（`release-2.30.12`）、cmake 3.31.6（经 uv）、ESP-IDF **5.5.5**、codex CLI 0.152.0、Bridge CPython 3.12（uv 管理）、websockets 17.1、Bleak 3.0.2、esp_websocket_client 1.8.0、Noto Sans SC v2.004 子集。

1. **uv**（Python 版本与依赖注入，本机路径 `~/.local/bin/uv`）：按官方 https://docs.astral.sh/uv/ 安装；验证 `uv --version`。Bridge 与多数脚本经 `uv run --python 3.12 ...` 自动拉取 CPython 3.12 与依赖。
2. **cmake**（无 Homebrew 方案）：`uv tool install cmake==3.31.6`（装到 `~/.local/bin`）；`scripts/build_simulator.sh` 在缺失时会自动执行同一安装。
3. **ESP-IDF v5.5.5**，安装到固定路径 `~/esp/esp-idf-v5.5.5`（厂商要求 ≥V5.5.0，docs/HARDWARE.md §6；本机既有安装即此路径，P4.1a 以 `idf.py --version`=5.5.5 复核）：

   ```sh
   mkdir -p ~/esp
   git clone -b v5.5.5 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf-v5.5.5
   git -C ~/esp/esp-idf-v5.5.5 rev-parse v5.5.5            # 期望 tag 对象 ff1bac0aeecdd2b797b9c3a558c6bd03629bc013
   git -C ~/esp/esp-idf-v5.5.5 rev-parse v5.5.5^{commit}   # 期望 peeled commit b774170ff46c393eeb5e495ea37936038d3f4f4f（本草案 2026-09-11 实测；VERSIONS.md 同位置疑有一处转置，待 A0 勘误）
   ~/esp/esp-idf-v5.5.5/install.sh esp32s3
   source ~/esp/esp-idf-v5.5.5/export.sh && idf.py --version   # 期望 5.5.5
   ```

4. **板级 vendor（固件开发才需要）**：`sh scripts/vendor_waveshare.sh` 克隆 Waveshare 官方仓库到 `vendor/waveshare-rlcd/` 并锁定 HEAD `eb1f634…`（幂等，已存在且校验一致则跳过）。

## 3. 模拟器工作流

```sh
# 一键：vendor LVGL → 构建 SDL2（静态库）→ cmake configure + build
sh scripts/build_simulator.sh
# 分步等价（均可重复运行，已缓存则跳过）：
sh scripts/vendor_lvgl.sh    # LVGL commit tarball → vendor/lvgl/，打印并校验 sha256
sh scripts/build_sdl2.sh     # SDL2 tag tarball → third_party/sdl2-install/，校验 sha256

# 窗口模式（400×300，Esc 退出；Space/→ 短按翻页，m 长按静音）
./build/simulator/codex-display-sim

# 注入 AppState 渲染并抓逻辑单色帧（1bpp BMP，非 SDL 截图）
SDL_VIDEODRIVER=dummy ./build/simulator/codex-display-sim \
  --state tests/fixtures/protocol/valid_full.json \
  --capture-frame artifacts/ui/needs_you.bmp --quit-after-ms 500

# 场景回放 → 确定性帧 + manifest（回归入口）
SDL_VIDEODRIVER=dummy ./build/simulator/codex-display-sim \
  --scenario tests/fixtures/scenarios/S03_working.jsonl \
  --capture-dir artifacts/ui/replay

# UI 像素回归 + 语义断言（golden 只读，永无自动更新）
uv run --python 3.12 python scripts/check_ui.py \
  --golden tests/golden --actual artifacts/ui/replay

# 离屏冒烟（2 秒自动退出，验收退出码 0）
sh simulator/smoke_offscreen.sh
```

完整参数表（`--state`/`--battery-mv`/`--battery-seq`/`--link-state`/`--page`/`--fixed-clock`/`--scenario`/`--capture-dir` 等）与键盘映射见 simulator/README.md。两条红线：LOW BATTERY 强制页只能由真实 Power FSM 产出（`--battery-seq` 经与固件同源的 FSM 步进）；golden 更新只能由 A0 用 `scripts/gen_goldens.py` 显式生成——`check_ui.py` 无 `--update-golden`。

生命周期端到端与 soak：

```sh
uv run --python 3.12 python scripts/replay_lifecycle.py   # bridge replay→模拟器→check_ui 全链 + 五阶段断言
sh scripts/run_soak.sh                                    # P6.2 前置压缩 soak（默认约 3–5 分钟，overall_pass 须 true）
```

## 4. Bridge 工作流（mock / replay / codex 三源）

```sh
# mock：内置确定性场景（stdout 出 JSONL，或 --out 每快照一个 JSON 文件）
uv run --python 3.12 python -m bridge --source mock --scenario lifecycle --out DIR
uv run --python 3.12 python -m bridge --source mock --scenario lifecycle

# replay：NormalizedEvent JSONL 回放
uv run --python 3.12 python -m bridge --source replay \
  --file tests/fixtures/bridge/lifecycle_events.jsonl --out DIR

# codex：真实 app-server adapter。不带 --live 为 dry-run（只打印计划，不触碰任何东西）
uv run --python 3.12 python -m bridge --source codex --prompt "只回复 ok"
uv run --python 3.12 python -m bridge --source codex --prompt "只回复 ok" --out DIR --live
```

`--live` 安全语义一句话：仅在隔离临时 cwd 创建 **ephemeral 受控会话**（readOnly 沙箱、无网络、绝不 resume/触碰已存在 thread、绝不 approve/reject/answer，等待超时只 interrupt 自己的 turn）。退出码表与脱敏说明见 `bridge/__main__.py` 与 `bridge/redact.py`；凭证不进 State、fixtures 或日志（落盘后自动脱敏自检）。

```sh
# bridge 单元/适配器测试（A0 复跑 90 passed）
uv run --python 3.12 --with pytest --with jsonschema pytest tests/bridge -q

# live smoke：真实 codex 受控 turn + 能力探测 + 脱敏自检（不在 pytest 默认集；会真实消耗一次 turn）
python3 scripts/codex_live_smoke.py            # 支持 --interrupt-after/--prompt/--label 等，见脚本 docstring
```

## 5. 固件工作流（ESP-IDF 5.5.5）

```sh
source ~/esp/esp-idf-v5.5.5/export.sh

# 主工程：整机产品形态（display_st7305 + cdt_lvgl + Wi-Fi/WSS 全链路；整机运行见 §6）
idf.py -C firmware build
idf.py -C firmware -p /dev/cu.usbmodem1301 flash monitor

# examples 三个骨架（均编译级 done，零警告；真机行为未验证 ⏳）：
(cd firmware/examples/transport_skeleton && idf.py build)   # WSS 客户端 + NimBLE 外设骨架
(cd firmware/examples/board_io_skeleton && idf.py build)    # ADC/电池/按键 IO 骨架
(cd firmware/examples/power_skeleton && idf.py build)       # P5.3 电源执行器骨架
# 电源 FSM 纯逻辑不在 examples，而在 shared/power（host 测试）与
# firmware/components/battery 同源校准（见 §7 固件 IO 行）

# 固件组件纯逻辑 host 测试（零 esp 头，Apple cc 直接编译）
sh scripts/build_firmware_io_tests.sh
```

注意：P4.3 的 transport 组件临时排除已按裁决撤回（2026-09-11），主工程恢复全量构建（battery/input/power/transport 全部参与；BLE 编译进固件但本集成不启动）；`esp_websocket_client` 1.8.0 由 component manager 锁定；SPKI pinning 已随整机集成在 Wi-Fi 路径落地（自签 CA 验链 + 叶子证书 SPKI SHA-256 pin，任一失败 = CONFIG_ERROR 终态）；该组件已知缺陷见 §1 注脚。

**板卡恢复凭证（烧录前必读）**：板上原始固件的 16MB 全量 Flash 备份在
`artifacts/board/backup-20260910-2328/`（`full_16m.bin` + SHA-256 `54afe421…`，命令级证据见同目录 `README.md`）。恢复命令（**写操作，仅限被授权 agent 在明确任务下执行，日常不要运行**）：

```sh
/Users/cui/.local/bin/uv run --with esptool python -m esptool \
  --port /dev/cu.usbmodem1301 --baud 921600 write-flash 0x0 full_16m.bin
```

端口纪律：本项目只允许 `/dev/cu.usbmodem1301`（Espressif USB-Serial/JTAG）；`/dev/cu.usbmodem1605` 是非本项目板，**禁碰**。恢复后用 `flash-id` 复核并对账 sha256（步骤见备份 README）。

## 6. 整机运行（Wi-Fi 实时链路）

真机已验证的最小闭环：Mac 起 Bridge（mock lifecycle 循环 + 15s 存活快照）→ 设备 Wi-Fi STA → WSS（TLS + CA 验链 + SPKI pin + 设备 token）→ 快照整包应用上屏；KEY 短按翻四页 / 长按静音；断连自动重连自愈（2026-09-11 实测，证据 `artifacts/board/integration/`）。BLE 真机链路、P5.2 省电与功耗数据未完成（⏳，见 §1 与 §7 未覆盖清单）。

### 6.1 前置条件

- Mac 与板在**同一局域网**；Wi-Fi SSID 必须是 **2.4GHz**（ESP32-S3 不支持 5GHz）。
- ESP-IDF v5.5.5 环境就绪（§2 第 3 步）：`source ~/esp/esp-idf-v5.5.5/export.sh`。
- 板为端口纪律允许的 `/dev/cu.usbmodem1301`（§5）。

### 6.2 配置设备凭据（7 宏，绝不入库）

```sh
cp firmware/main/dev_net_config.h.template firmware/main/dev_net_config.h
```

按模板注释填 7 个宏（真值直接复制 §6.3 Bridge 的输出提示）：

| 宏 | 填什么 |
|---|---|
| `DEV_WIFI_SSID` / `DEV_WIFI_PASS` | 2.4G Wi-Fi 名称 / 密码 |
| `DEV_BRIDGE_HOST` / `DEV_BRIDGE_PORT` | Mac 的 LAN IP / 端口（默认 8765） |
| `DEV_DEVICE_TOKEN` | 设备 token（与 Bridge 侧 `config/local/device_token` 同一值；独立设备 token，不用 Codex 凭证） |
| `DEV_BRIDGE_CA_PEM` | `config/local/dev-certs/ca.pem` 全文（每行加双引号、`\n` 结尾、行间反斜杠续行，模板有示例） |
| `DEV_BRIDGE_SPKI_SHA256_HEX` | `config/local/dev-certs/server_spki_sha256.txt` 的 64 hex 指纹 |

红线：`firmware/main/dev_net_config.h` 与 `config/local/`（证书 + token）均已被 .gitignore 排除，**绝不提交、绝不粘贴进日志/截图/报告**。该文件缺失时主工程构建直接 `#error`（无网演示构建可用 `idf.py -C firmware build -DCDT_DEVNET_OFFLINE=1`，不编入任何凭据）。桥侧重启并重建证书后，`DEV_BRIDGE_CA_PEM` 与 `DEV_BRIDGE_SPKI_SHA256_HEX` 两值必须同步重填并重新烧录。

### 6.3 启动 Bridge（Mac 侧）

```sh
sh scripts/run_bridge_lan.sh                # 一键：探测 LAN IP → 证书 SAN 检查/重建 → 复用/生成 token → 起服务
sh scripts/run_bridge_lan.sh --setup-only   # 只做前置三步（打印设备侧 7 宏填值提示），不起服务
```

- 自动取 Mac LAN IP（`ipconfig getifaddr`，默认依次探测 en0/en1；环境变量 `CDT_IFACE` / `CDT_LAN_IP` / `CDT_PORT` 可覆盖）。
- 证书：`config/local/dev-certs/server.crt` 的 SAN 不含当前 LAN IP 时自动重建（SAN = loopback + LAN IP + `--san X` 追加项），并同步产出 SPKI 指纹文件。
- token：`config/local/device_token` 存在则复用，缺失则生成 48 hex 随机值；日志只打 SHA-256 指纹前 8 hex，不打印 token 本体。

### 6.4 烧录与观察

```sh
source ~/esp/esp-idf-v5.5.5/export.sh
idf.py -C firmware -p /dev/cu.usbmodem1301 flash monitor
```

串口日志解读（`cdt_app` tag，凭据只以指纹形式出现）：

- `[state] applied #<n> seq=… epoch=… bytes=… crc32=…` — 收到合法快照并整包应用；`epoch` 变化表示 Bridge 重启后的新代（全量替换旧快照）；`dropped` 应保持 0。
- `[link] a -> b` — 链路状态迁移（CONNECTED / STALE / DISCONNECTED）。
- `[battery] mv=4125 valid=1` — 板上 ADC 电压采样，喂 Power FSM 并上屏。
- 其余：`[net] WiFi …（ip=…）`、`[wss] link=…`、`[page]`、`[key]`、`[power]`。

### 6.5 设备操作

- **KEY 短按**：NOW → AGENTS → PLAN → USAGE 四页轮换（日志 `[key] 短按 -> page=N`；LOW BATTERY 为强制页，不进轮换）。
- **KEY 长按（≥800ms，松开判定）**：静音当前提醒——仅本地静音/已读 ACK，**绝不等于批准 Codex 操作**；新 turn 带新提醒时自动解除（日志 `[key] 长按 -> mute …`）。
- **断连行为**：断开时页面顶部出现反白提示条 `LINK DISCONNECTED - TIME FROZEN`（有历史快照但超时未刷新为 `LINK STALE - TIME FROZEN`），末帧画面保持、时间冻结；WSS 客户端按冻结退避序列 **1/2/4/8/16/30 秒、封顶 30s、±20% 抖动**（protocol/transport.md §5.4）自动重连，Bridge 恢复后以新 epoch 全量替换。已知现象：Wi-Fi 空闲 RX 停滞会引发周期性断链，自愈正常，根因排查归 P5.2 省电配置（⏳ 未做）。

### 6.6 故障排查

| 症状 | 检查 / 处置 |
|---|---|
| 设备连不上 Wi-Fi | SSID 必须为 2.4G；看 `[net] WiFi …（ip=…）` 是否取到 IP；确认 Mac 与板同网段 |
| Bridge 起不来 | 端口占用：`lsof -iTCP:8765 -sTCP:LISTEN`，结束占用进程，或 `CDT_PORT=xxxx sh scripts/run_bridge_lan.sh` 换端口（设备侧 `DEV_BRIDGE_PORT` 同步改） |
| TLS 握手失败 / 设备 CONFIG_ERROR | 证书 SAN 不含设备所连 Bridge IP（Mac 换网后常见）→ 重跑 `sh scripts/run_bridge_lan.sh` 自动重建含新 LAN IP 的证书，按提示同步重填 `DEV_BRIDGE_CA_PEM` 与 `DEV_BRIDGE_SPKI_SHA256_HEX` 并重新烧录 |
| 连上即被拒 / 无快照 | token 不一致：对照 `firmware/main/dev_net_config.h` 的 `DEV_DEVICE_TOKEN` 与 Mac 侧 `config/local/device_token` 是否同值 |
| 起服务报"未能取得 LAN IP" | Wi-Fi 未连或网卡名不同：`CDT_LAN_IP=x.x.x.x` 或 `CDT_IFACE=enN` 显式指定 |

## 7. 测试矩阵总览

| 套件 | 一条命令 | 当前通过数（A0 复跑记录） |
|---|---|---|
| 协议 fixtures | `uv run --with jsonschema python scripts/check_protocol.py` | 16/16 PASS（2026-09-11 本草案抽查复跑，exit 0） |
| 共享 state/display/power | `sh scripts/build_shared.sh` | 137 PASS（36+101；2026-09-11 P5.1 记录，含 ASan/UBSan 复验） |
| presenter + 页面/导航 | `sh scripts/build_presenter_tests.sh` | 109 PASS（42+67；2026-09-10 P2.2/P2.3） |
| bridge | `uv run --python 3.12 --with pytest --with jsonschema pytest tests/bridge -q` | 90 passed（2026-09-11 P3.2-老化C；等价短形式 `pytest tests/bridge -q`） |
| transport host（BLE 分片/重组+CRC+门禁） | `sh scripts/build_transport_tests.sh` | 97 PASS + CRC 4/4 + 门禁（2026-09-11 P3.4 记录） |
| BLE loopback 互验 | `python3 tests/transport/ble/run_loopback.py`（先跑上行脚本产出 C 测试二进制） | 18/18，cmp 逐字节一致（2026-09-11 P3.4 记录） |
| transport WSS | `pytest tests/transport/wss -q` | 54 passed（2026-09-11 P3.3；依赖 websockets==17.1） |
| P3.5 集成（reassembler→store→presenter） | `sh scripts/run_p35.sh` | 16/16（2026-09-11 P3.5 复跑） |
| 模拟器离屏冒烟 | `sh simulator/smoke_offscreen.sh` | exit 0（2026-09-10 P2.2/P2.3） |
| UI 像素回归 + 语义断言 | `uv run --python 3.12 python scripts/check_ui.py --golden tests/golden --actual <capture-dir>` | 105 帧 / 22 场景零像素差异、语义 ok（2026-09-10 P2.4） |
| check_ui 自检 | `uv run --python 3.12 python scripts/check_ui.py --self-test` | SELF-TEST: PASS（2026-09-11 本草案抽查） |
| 生命周期端到端 | `uv run --python 3.12 python scripts/replay_lifecycle.py` | 全过 exit 0（2026-09-10 P2.5） |
| soak（前置压缩） | `sh scripts/run_soak.sh` | overall_pass=true（2026-09-11 P6.2） |
| 固件 IO 纯逻辑 | `sh scripts/build_firmware_io_tests.sh` | 电池 34 + 按键 22 + 平台头门禁（2026-09-11 本草案抽查，exit 0） |
| 电源执行器纯逻辑（P5.3，双变体） | `sh scripts/build_power_tests.sh` | 141 断言全过（变体 A 68 + 变体 B 73 + 平台头门禁；2026-09-11 v1.1 撰写时复跑，exit 0） |
| 真机断连自愈（整机集成回归） | 真机记录（杀 Bridge→重启→观察重连；方法见 docs/STATUS.md「断连重连修复+KEY 实测」行） | 3 轮循环 3/3 自愈（退避档位逐拍对照冻结序列 ±20% + 新 epoch 全量替换）；10 分钟 soak 7 断连全自愈 / 116 applied / SAMPLE_GAP_RESET=0（证据 `artifacts/board/integration/a4-reconnect/` 10 件） |

pytest 短形式均取自 STATUS 中 A0 复跑原文；若新机器无全局 pytest，按 bridge 行的 uv 前缀等价形式执行（P1.1 原文），WSS 套件需补 `--with websockets==17.1`。

⏳ **未覆盖（真机段）**：BLE 真机互通（B0-B5）、24h 稳定、权限拒绝、ADC 表计对照校准（≤30mV）、按键手感去抖、低压冷启动、Wi-Fi 空闲 RX 停滞致周期断链的根因（P5.2 省电配置排查）、P5.4 真机睡眠与 BLE/Wi-Fi 功耗对比及 `docs/POWER_REPORT.md`。断连自愈已完成 3 轮循环 + 10 分钟 soak（见上表），不等价于 24h 稳定验收。SDL 结果不用于证明硬件睡眠、无线电流或 LCD 保持效果。

## 8. 协作约定（四真源）

- **docs/DEVELOPMENT_PLAN.md**：需求、任务编号、依赖与验收的唯一真源。
- **docs/INTERFACES.md**：State 与模块边界契约（v1，P0 冻结）。
- **docs/STATUS.md**：唯一实施状态台账（owner/状态/证据/验证命令），不另建 backlog。
- **AGENTS.md**：Agent 分工、实现边界与完成交付约定（UI 不碰硬件 IO、ACK≠批准、凭证不入 State 等）。

开发 Agent 从 AGENTS.md 进入，按任务编号认领；版本升级与 golden 更新由 A0 统一执行。

---

## 附：本草案的命令可复现性抽查（P6.3，2026-09-11）

撰写时在仓库根随机抽 5 条命令复跑，均退出码 0：

1. `uv run --with jsonschema python scripts/check_protocol.py` → 16/16 PASS
2. `sh scripts/build_firmware_io_tests.sh` → 电池 34 PASS + 按键 22 PASS
3. `uv run --python 3.12 python -m bridge --source mock --scenario lifecycle --out DIR` → 10 快照
4. `uv run --python 3.12 python scripts/check_ui.py --self-test` → SELF-TEST: PASS
5. `SDL_VIDEODRIVER=dummy ./build/simulator/codex-display-sim --state tests/fixtures/protocol/valid_full.json --capture-frame … --quit-after-ms 400` → 1bpp BMP 落盘

v1.1 补充（整机章节，2026-09-11）：`sh scripts/build_power_tests.sh` 撰写时复跑 exit 0（68+73=141 断言）；
新章节引用的脚本与文件（`scripts/run_bridge_lan.sh`、`scripts/bridge_serve_mock.py`、
`firmware/main/dev_net_config.h.template`、`artifacts/board/integration/a4-reconnect/` 10 件证据）
均经存在性核验；涉及烧录与起服务的命令按红线未执行（纯文档任务，不触板）。

设计依据与阶段/功耗实验细节见 docs/DEVELOPMENT_PLAN.md（§5 任务、§8 测试策略、§9 功耗实验）；计划编制日期 2026-09-10。
