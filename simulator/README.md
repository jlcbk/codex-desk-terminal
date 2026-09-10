# codex-display-sim 模拟器（P1.3 骨架 + P2.1 状态注入/渲染/抓帧）

SDL2 + LVGL 9.3.0 桌面模拟器：400×300 窗口、软件渲染、键盘模拟 KEY、可离屏（CI 无头）运行。
P1.3 只做骨架；P2.1 接入 shared/state 解析、shared/presenter、shared/ui NOW 页，
并新增逻辑单色帧抓取（P2.4 golden 流入口）。

## 版本（严格锁定，见 docs/VERSIONS.md）

| 依赖 | 版本 | 标识 | tarball sha256 |
|---|---|---|---|
| LVGL | 9.3.0 | commit `c033a98afddd65aaafeebea625382a94020fe4a7` | `9c6f8230d0b0e11141aad8f047af699322dbd3d37b2c41170b1526c12d5dfbb3` |
| SDL2 | 2.30.12 | tag `release-2.30.12` | `560da2e54dd8af933e35bd08fb1b6cf80d4f6938c67710fecf13b7e9bdd6c47e` |
| cmake（构建工具，uv 安装） | 3.31.6 | `uv tool install cmake==3.31.6` | —（待 A0 记入 VERSIONS.md） |

## 构建与运行

```sh
# 一键：vendor LVGL -> 构建 SDL2 -> cmake configure + build
scripts/build_simulator.sh
# 产物
./build/simulator/codex-display-sim
```

分步执行（脚本均可重复运行，已缓存/已安装则跳过）：

```sh
scripts/vendor_lvgl.sh        # 下载 LVGL commit tarball -> vendor/lvgl/，打印 sha256
scripts/build_sdl2.sh         # 下载 SDL2 tag tarball -> third_party/sdl2-install/（静态库），打印 sha256
cmake -S simulator -B build/simulator -DCMAKE_BUILD_TYPE=Release
cmake --build build/simulator
```

无 Homebrew：cmake 缺失时脚本用 `uv tool install cmake==3.31.6`（装到 `~/.local/bin`）。

## 运行模式

```sh
# 窗口模式（Mac 打开 400x300 窗口，Esc 或点关闭按钮退出）
./build/simulator/codex-display-sim

# P2.1：注入 AppState + DeviceRuntime 合成，渲染 NOW 页并抓逻辑单色帧
SDL_VIDEODRIVER=dummy ./build/simulator/codex-display-sim \
  --state tests/fixtures/protocol/valid_full.json \
  --capture-frame artifacts/ui/needs_you.bmp --quit-after-ms 500

# 离屏冒烟（SDL dummy 驱动，2 秒自动退出，验收退出码 0）
simulator/smoke_offscreen.sh
# 等价手工命令：
SDL_VIDEODRIVER=dummy SIM_AUTO_QUIT_MS=2000 ./build/simulator/codex-display-sim

# P2.4/P2.5：场景回放 → 确定性帧 + manifest（golden/回归入口）
SDL_VIDEODRIVER=dummy ./build/simulator/codex-display-sim \
  --scenario tests/fixtures/scenarios/S03_working.jsonl \
  --capture-dir artifacts/ui/replay
uv run --python 3.12 python scripts/check_ui.py \
    --golden tests/golden --actual artifacts/ui/replay
```

### 命令行参数（P2.1；P2.2/P2.3 追加标注）

| 参数 | 含义 |
|---|---|
| `--state <file.json>` | 启动读入 AppState JSON（shared/state 有界解析器；坏文件/非法 JSON 打印错误并以退出码 2 结束，先于 SDL 初始化） |
| `--battery-mv <N>` | 合成 DeviceRuntime 电池电压（默认 3900）；usable_percent = clamp((mv−3600)/600×100)（§7.1）；charging/external_power 恒为 unknown（不根据电压猜充电） |
| `--battery-seq "<mv>@<ms>,..."` | （P2.3）电池采样序列：逐个经与固件相同的 Power FSM（shared/power，P5.1）步进，`runtime.power_state` 只取 FSM 终态——LOW BATTERY 强制页由真实保护逻辑驱动，不提供直画低压页的捷径 |
| `--link-state <connected\|stale\|disconnected>` | 链路状态（默认 connected）；快照视为进程启动时刻收到（last_rx=0），fresh 时 ELAPSED/WAITING 随单调时间推进，stale/disconnected 时 presenter 冻结 |
| `--page <now\|agents\|plan\|usage>` | （P2.2）初始普通页（写 DeviceRuntime.selected_page）；LOW BATTERY 强制页只能由 Power FSM 经 presenter 仲裁产出，`--page` 无法触达 |
| `--capture-frame <out.bmp>` | 退出前把 400×300 逻辑帧经 `cdt_frame_t`（shared/display 公共单色格式：1bpp、行 50 字节、MSB=左、1=黑）存 1bpp BMP（调色板 0=白 1=黑）。供 P2.4 golden 流使用，**不是 SDL 截图** |
| `--quit-after-ms <N>` | N 毫秒后自动退出（旧环境变量 SIM_AUTO_QUIT_MS 仍生效） |
| `--fixed-clock <ms>` | 虚拟单调时钟恒为 N ms（时长不随真实时间变化；跨机器 golden 抓帧确定性，P2.4 用） |
| `--scenario <file.jsonl>` | （P2.4 集成，A6）注入 JSONL 回放（INTERFACES §4：app_state/battery_sample/key/link/advance_time；先全量预检再回放，违规退出码 1 不出帧；虚拟时钟由 at_ms/advance_time 驱动，确定性）。电池采样经真实 Power FSM；45s/150s 无快照自然老化（link 动作只提前注入）；与 `--fixed-clock`/`--state`/`--battery-seq` 互斥 |
| `--capture-dir <dir>` | （P2.4 集成，A6）`--scenario` 必配：帧与 manifest 输出到 `<dir>/<场景主名>/`。每条 action 后 ViewModel 有像素变化才出帧（400×300 逻辑单色 PNG，无压缩差异字节确定）；manifest.jsonl 每行 frame/scenario/frame_index/at_ms/action/seq + 扩展 tag/view 字段（页面/状态词/时长/链路/静音/AGENTS 排序/PLAN 计数/USAGE 行）供 check_ui 语义断言 |

环境变量：

| 变量 | 含义 |
|---|---|
| `SIM_AUTO_QUIT_MS` | 运行 N 毫秒后自动退出（0/未设 = 常驻） |
| `SIM_CAPTURE_PATH` | 退出前把 SDL 渲染器帧存 BMP（人工证据；与 `--capture-frame` 的公共单色逻辑帧不同源但同图） |

抓帧流程：每次 apply 后整屏失效、单次 400px 宽整帧刷新（与 v1 显示模型一致：
flush 支持整帧，dirty_area 仅是优化提示，INTERFACES §5），退出时 `lv_refr_now`
保证帧含最后一次 apply，再 RenderReadPixels 回读黑/白两色像素，按公共单色帧
重新打包为 `cdt_frame_t` 后写 1bpp BMP。`--fixed-clock` 下两次运行帧逐字节一致
（已实测 500ms 与 1500ms 运行 `cmp` 相同）。

## 键盘映射（KEY 导航，P2.2 接线完成）

| 按键 | 语义 |
|---|---|
| Space / → | KEY 短按：AGENTS/PLAN 子页先推进，末子页再切下一主页面（NOW→AGENTS→PLAN→USAGE 轮换；`cdt_nav_key` 纯逻辑，宿主写回 `runtime.selected_page`） |
| m | KEY 长按：静音当前提醒（写 `runtime.muted_attention_id`；只静音，不切页、不解除 LOW BATTERY 强制页） |
| Esc | 退出 |
| 窗口关闭按钮 | LVGL `LV_SDL_DIRECT_EXIT` 路径退出（`SDL_Quit`+`lv_deinit`+`exit(0)`） |

LOW BATTERY 强制页期间短按被 `cdt_nav` 拒绝（页面/子页均不变），长按仍只记静音。

## 渲染与单色

- `LV_COLOR_DEPTH=1`（I1）。LVGL 9.3.0 的 SDL 驱动原生支持：flush 内部做 I1→ARGB8888 转换，
  条件是 `LV_SDL_RENDER_MODE=LV_DISPLAY_RENDER_MODE_PARTIAL`（见 simulator/lv_conf.h，源码中有 `#error` 守卫）。
  实测深度 1 可正常出图，**未**退到 32 位。
- I1 索引语义：SDL 驱动把索引 1 渲染为白、0 渲染为黑；`--capture-frame` 输出的
  公共单色帧语义为 1=黑（cdt_frame_t），BMP 调色板已做对应映射（索引 0=白、1=黑）。
- 每次刷新整屏失效 → 单个 400px 宽刷新区（400 为 8 的倍数，I1 打包确定），帧逐字节可复现。

## NOW 页布局（P2.1，shared/ui/cdt_ui_now.c）

400×300，外边距 8px，纯黑白两色：标题栏 28px（项目名 + 电压）、链路提示条 20px
（fresh 时隐藏）、主状态区 52px（28px 状态词；needs_you/LOW BATTERY 反白，error 3px 粗框）、
正文 16px（活动摘要、ELAPSED/WAITING、PLAN、额度、PENDING；超长由 presenter 列预算
截断 + LV_LABEL_LONG_DOT 像素兜底，UTF-8 码点安全）、底栏 24px（页面指示 + 静音文字标签）。
内置字体为 ASCII Montserrat（14/16/28，lv_conf.h 打开）；非 ASCII 码点显示为 `?`
占位（cdt_ui_ascii_safe），Noto Sans SC 子集属 P2.2。

## P2.1 渲染证据（artifacts/ui/，git 忽略）

| 文件 | 内容 |
|---|---|
| `{idle,thinking,working,needs_you,done,error,cancelled}.json/.bmp/.png` | 六状态注入包与逻辑帧（无任务 IDLE、取消=IDLE+CANCELLED） |
| `long_ascii.json/.bmp` | project 96B / activity 192B 顶满上限的 ASCII 截断用例 |
| `long_cjk.json/.bmp` | valid_max_sizes.json（CJK 顶满上限）截断用例 |
| `extra_stale_lowbat.bmp/.png` | `--link-state stale --battery-mv 3590`：链路提示条 + 3.59V + 冻结时长 |
| `*.log` | 每次运行的宿主日志 |

对应 presenter 单元测试：`scripts/build_presenter_tests.sh`（tests/shared/test_presenter.c，
42 用例：优先级 / "--" 显示 / fresh-frozen 计时 / mm:ss 与 hh:mm:ss / UTF-8 码点安全截断）。

## 证据（artifacts/sim/，git 忽略）

| 文件 | 内容 |
|---|---|
| `build_simulator.log` | cmake configure + build 日志（0 warning） |
| `smoke_offscreen.log` | 离屏冒烟日志（退出码 0） |
| `run_window.log` | 窗口模式运行日志（退出码 0，auto-quit） |
| `p1.3_window.png` | 真实窗口桌面截图（Retina 800×600；需终端有屏幕录制权限） |
| `p1.3_window_frame.{bmp,png}` | 窗口模式 LVGL 帧缓冲抓帧 |
| `p1.3_offscreen_frame.{bmp,png}` | dummy 驱动下的 LVGL 帧缓冲抓帧（CI 可复现） |

## 已知限制

- 占位画面仅为证明渲染（无 `--state` 时白底 + 黑矩形 + 内置 Montserrat 14 文本）。
- KEY 事件只打印/转桩不入队；鼠标/滚轮 indev 未创建；窗口可变性已禁用。
- 本任务未引入字库文件：中文以 `?` 占位显示（可见替代符），CJK 字体落地在 P2.2。
- LOW BATTERY 强制页 P2.1 只输出页面标记与基本字段（电压/状态词），完整页面在 P2.3；
  电池 critical 的产生必须经 Power FSM（P5.1），模拟器不伪造 power_state=critical 的入口。
- `leaks` 泄漏检查在本机沙箱环境无法附加进程（"Couldn't get task port"），泄漏断言未验证；
  退出路径为 `lv_display_delete` → `lv_sdl_quit` → `lv_deinit`，多次运行均退出码 0、无崩溃。
- SDL2 为静态库（`libSDL2.a`），无运行期 dylib 路径问题；如需共享库构建改 `scripts/build_sdl2.sh` 内开关。
