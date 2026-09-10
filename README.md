# Codex Desk Terminal

ESP32-S3-RLCD-4.2 桌面状态终端：抬眼看见 Codex 在做什么、是否需要处理、计划进度及额度。

> **文档状态：草案 v1（P6.3，2026-09-11，A0 助理起草、待 A0 审定）。**
> 真机传输上板与功耗实测段**未完成**，相关章节以 ⏳ 标注；其余命令均来自 docs/STATUS.md
> 的 A0 复跑记录或仓库脚本原文，其中 5 条已在本草案撰写时于仓库根随机抽查复跑（见文末说明）。

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
| 真机传输上板（BLE/WSS：B0-B5/W2/W5/SPKI） | ⏳ | 固件骨架编译级 done，真机项全部未验证 |
| 真机功耗实测与默认传输选择 | ⏳ | P5 逻辑（Power FSM）done；BLE/Wi-Fi 对比实验与 docs/POWER_REPORT.md 未开始 |
| 桌面实时旁听（P3.6） | **blocked** | P0.2 结论：桌面运行时为私有 stdio 子进程，无 daemon 可旁听；以真实 codex adapter 替代，不 Mock 掩盖 |

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

# 主工程：P4.2/P4.3 真机 UI 演示（display_st7305 + cdt_lvgl + p43_scenes）
idf.py -C firmware build
idf.py -C firmware -p /dev/cu.usbmodem1301 flash monitor

# examples 两个骨架（均编译级 done，零警告；真机行为未验证 ⏳）：
(cd firmware/examples/transport_skeleton && idf.py build)   # WSS 客户端 + NimBLE 外设骨架
(cd firmware/examples/board_io_skeleton && idf.py build)    # ADC/电池/按键 IO 骨架
# 电源 FSM 纯逻辑不在 examples，而在 shared/power（host 测试）与
# firmware/components/battery 同源校准（见 §6 固件 IO 行）

# 固件组件纯逻辑 host 测试（零 esp 头，Apple cc 直接编译）
sh scripts/build_firmware_io_tests.sh
```

注意：transport 组件在主工程 CMakeLists 中为临时排除（P4.3 A0 裁决，合并传输时撤回）；`esp_websocket_client` 1.8.0 由 component manager 按 `firmware/examples/transport_skeleton/dependencies.lock` 锁定；SPKI pinning 未实现（W5 真机项）。

**板卡恢复凭证（烧录前必读）**：板上原始固件的 16MB 全量 Flash 备份在
`artifacts/board/backup-20260910-2328/`（`full_16m.bin` + SHA-256 `54afe421…`，命令级证据见同目录 `README.md`）。恢复命令（**写操作，仅限被授权 agent 在明确任务下执行，日常不要运行**）：

```sh
/Users/cui/.local/bin/uv run --with esptool python -m esptool \
  --port /dev/cu.usbmodem1301 --baud 921600 write-flash 0x0 full_16m.bin
```

端口纪律：本项目只允许 `/dev/cu.usbmodem1301`（Espressif USB-Serial/JTAG）；`/dev/cu.usbmodem1605` 是非本项目板，**禁碰**。恢复后用 `flash-id` 复核并对账 sha256（步骤见备份 README）。

## 6. 测试矩阵总览

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

pytest 短形式均取自 STATUS 中 A0 复跑原文；若新机器无全局 pytest，按 bridge 行的 uv 前缀等价形式执行（P1.1 原文），WSS 套件需补 `--with websockets==17.1`。

⏳ **未覆盖（真机段）**：BLE/WSS 上板互通（B0-B5/W2/W5）、24h 稳定、断连/权限拒绝、ADC 表计对照校准（≤30mV）、按键手感去抖、低压冷启动、BLE/Wi-Fi 功耗对比与 `docs/POWER_REPORT.md`。SDL 结果不用于证明硬件睡眠、无线电流或 LCD 保持效果。

## 7. 协作约定（四真源）

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

设计依据与阶段/功耗实验细节见 docs/DEVELOPMENT_PLAN.md（§5 任务、§8 测试策略、§9 功耗实验）；计划编制日期 2026-09-10。
