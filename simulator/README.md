# codex-display-sim 模拟器（P1.3）

SDL2 + LVGL 9.3.0 桌面模拟器骨架：400×300 窗口、软件渲染、键盘模拟 KEY、可离屏（CI 无头）运行。
只做骨架——LVGL 初始化、窗口、事件循环、按键打印、占位画面、干净退出；页面/State/Presenter 由后续任务实现。

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

# 离屏冒烟（SDL dummy 驱动，2 秒自动退出，验收退出码 0）
simulator/smoke_offscreen.sh
# 等价手工命令：
SDL_VIDEODRIVER=dummy SIM_AUTO_QUIT_MS=2000 ./build/simulator/codex-display-sim
```

环境变量：

| 变量 | 含义 |
|---|---|
| `SIM_AUTO_QUIT_MS` | 运行 N 毫秒后自动退出（0/未设 = 常驻） |
| `SIM_CAPTURE_PATH` | 退出前把当前帧存为 BMP（渲染证据；Esc/自动退出路径有效） |

## 键盘映射（KEY 骨架）

| 按键 | 语义 |
|---|---|
| Space / → | KEY 短按（当前仅打印 `[sim] KEY event: short_press`） |
| Esc | 退出 |
| 窗口关闭按钮 | LVGL `LV_SDL_DIRECT_EXIT` 路径退出（`SDL_Quit`+`lv_deinit`+`exit(0)`） |

KEY 事件打印即后续接入 `ui_key()`/DeviceRuntime 的暂存点；LVGL 侧的 SDL 事件轮询由驱动内部定时器完成，
应用通过 `SDL_AddEventWatch` 观察事件，不与 LVGL 抢事件。

## 渲染与单色

- `LV_COLOR_DEPTH=1`（I1）。LVGL 9.3.0 的 SDL 驱动原生支持：flush 内部做 I1→ARGB8888 转换，
  条件是 `LV_SDL_RENDER_MODE=LV_DISPLAY_RENDER_MODE_PARTIAL`（见 simulator/lv_conf.h，源码中有 `#error` 守卫）。
  本骨架实测深度 1 可正常出图，**未**退到 32 位。
- I1 索引语义：SDL 驱动把索引 1 渲染为白、0 渲染为黑。
- docs/INTERFACES.md §5 的共享逻辑帧（400×300、1bpp、每行 50 字节、MSB=左像素、1=黑/0=白）
  属 shared/display 后续任务，本骨架未实现。

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

- 占位画面仅为证明渲染（白底 + 黑矩形 + 内置 Montserrat 14 文本 "codex-desk-terminal sim"）。
- KEY 事件只打印不入队；鼠标/滚轮 indev 未创建；窗口可变性已禁用。
- DEVELOPMENT_PLAN §8 的命令行接口（`--scenario`、`--fixed-clock`、`--capture-dir`）在 P2 实现环境变量为临时形态。
- macOS `screencapture` 截窗口需屏幕录制权限；无权限时拍到纯壁纸。帧缓冲 BMP 抓帧不依赖该权限，是更可靠的回归证据。
- `leaks` 泄漏检查在本机沙箱环境无法附加进程（"Couldn't get task port"），泄漏断言未验证；
  退出路径为 `lv_display_delete` → `lv_sdl_quit` → `lv_deinit`，多次运行均退出码 0、无崩溃。
- SDL2 为静态库（`libSDL2.a`），无运行期 dylib 路径问题；如需共享库构建改 `scripts/build_sdl2.sh` 内开关。
