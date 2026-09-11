# 版本清单（P0.1）

锁定日期：2026-09-10。原则：不使用浮动依赖；每个条目给出精确 tag/commit/版本号与来源；升级须由 A0 统一变更并同步两端构建。

## 构建宿主（当前记录）

| 项 | 值 |
|---|---|
| OS | macOS 15.x（darwin 24.6.0，arm64） |
| 编译器 | Apple clang 17.0.0（clang-1700.0.13.5） |
| git | 2.39.5 (Apple Git-154) |
| Node | v24.19.0 |
| Python | 系统 3.9.6（仅探针脚本）；Bridge 运行时用 uv 管理的 CPython 3.12（见下） |
| uv | /Users/cui/.local/bin/uv（Python 版本与依赖注入） |
| Homebrew | **未安装**（SDL2/cmake 获取方案见下） |

## 已锁定依赖

| 依赖 | 锁定版本 | 精确标识 | 来源 |
|---|---|---|---|
| LVGL（PC 与设备同一份） | 9.3.0 | tag `v9.3.0`，commit `c033a98afddd65aaafeebea625382a94020fe4a7`（tag 对象 `108e5aff3c90cc4d969331ed61aff2bbd365d430`），发布 2025-06-03；tarball sha256 `9c6f8230d0b0e11141aad8f047af699322dbd3d37b2c41170b1526c12d5dfbb3`（scripts/vendor_lvgl.sh 每次校验） | github.com/lvgl/lvgl，`git ls-remote` 与 release 页双核对 |
| SDL2（模拟器宿主） | 2.30.12 | tag `release-2.30.12`（`8236e01a9f758d15927624925c6043f84d8a261f`），2.30.x 线共 13 个 tag 中的最新；tarball sha256 `560da2e54dd8af933e35bd08fb1b6cf80d4f6938c67710fecf13b7e9bdd6c47e`；源码构建静态库至 third_party/sdl2-install/ | github.com/libsdl-org/SDL，`git ls-remote` |
| cmake（构建宿主，经 uv） | 3.31.6 | `uv tool install cmake==3.31.6`（故意不取 PyPI 4.x：其 min-required<3.5 策略拒绝 SDL 2.30 工程） | PyPI 经 uv |
| codex CLI（协议探针与 Bridge 上游） | 0.152.0 | `codex --version` = codex-cli 0.152.0；本机路径 /Users/cui/.local/lib/node-v24.19.0-darwin-arm64/bin/codex | 本机安装；app-server 协议由该版本 generate-json-schema 生成存档（见 docs/proto-samples/） |
| Bridge Python | CPython 3.12.x | 经 uv 安装并固定（`uv python install 3.12` + 项目内 lock）；标准库优先，第三方包逐个审批入库 | python.org 经 uv 分发 |
| 字体栈（2026-09-11 终稿） | wqy 16×16 全量单字体 | 主字体=文泉驿点阵宋 wenquanyi_12pt.bdf（16×16 1bpp，41,293 码点：ASCII+CJK 统一区 99.8%+扩展 A 99.8%+假名+韩文全表+希腊/西里尔；sha256 `eb8e295f…`，源 third_party/dl/wqy-bitmapsong/，GPLv2+字体嵌入例外）；大字状态词=Montserrat 28；ASCII 正文=wqy 自带（Liberation 同源，同字重无接缝）；fallback=wqy→替代符（无字体层）；BDF→LVGL 转换器 scripts/gen_font_wqy.py（确定性+--check）；固件分区 factory 4M（app 2.5MB 余 40%） | 用户终选：观感三连败者出局（4bpp AA→毛糙、矢量硬边→锯齿），点阵 16×16 胜出；Noto 生成器留仓不进构建 |
| ESP-IDF（固件构建） | 5.5.5 | tag `v5.5.5`：tag 对象 `ff1bac0aeecdd2b797b9c3a558c6bd03629bc013`（PGP 签名），peeled commit `b774170ff46c393eeb5e495ea37936038d3f4f4f`（实测 checkout HEAD；P4.1a 经 ls-remote `^{}` 双向核对） | 厂商 wiki 要求 ≥V5.5.0（docs/HARDWARE.md §6）；随仓 sdkconfig 版本头实为混合（5.4.3/5.5.1/5.5.2），重建流程（删配置→defaults 重生成）不受影响 |
| esp_websocket_client（固件 WSS，IDF component manager） | 1.8.0 | espressif registry 锁定 `espressif/esp_websocket_client==1.8.0`（component_hash dc00233a…，仅要求 idf>=5.0；firmware/examples/transport_skeleton/dependencies.lock） | 传输骨架任务选型；SPKI pinning 需定制 verify 回调（W5 真机项） |
| Bleak（macOS BLE central） | 3.0.2 | PyPI 实查 2026-09-10（pip index+项目页双源）；MIT；requires_python ≥3.10，兼容 Bridge CPython 3.12 | P0.5 选型；3.x 与 1.x 的 API 差异以 P3.4 小样实测为准 |
| websockets（Bridge WSS server） | 17.1 | PyPI 实查 2026-09-10，2026-08-26 发布；BSD-3-Clause；requires_python ≥3.11 | P0.5 选型 |

## 待锁定（阻塞于并行任务结果）

（当前无待锁条目；下方为已收编决策记录）

| 决策 | 结论 |
|---|---|
| JSON 解析（C，共享/固件） | **自研有界解析器**（shared/state/cdt_json.c，零动态分配，深度/字节/UTF-8 约束内建，36/36 测试含 ASan）——不引入第三方库，两端同源，无版本漂移风险 |

## 宿主缺口与既定方案（无 Homebrew）

- **cmake**：P1.3 起，经 uv 环境安装 PyPI cmake 发行包（或 cmake.org 官方 arm64 tarball），版本写入本表后不再浮动。
- **SDL2**：从 github.com/libsdl-org/SDL 以锁定的 `release-2.30.12` tag 源码构建（Apple clang），或官方 release 的 arm64 dmg；安装方式与路径在 P1.3 定稿并回填本表。禁止"最新版"措辞。
- **ESP-IDF**：届时经 Espressif 官方安装器/git tag 安装（不装 latest 浮动分支），版本回填本表。

## 变更规则

- 任何版本升级 = A0 单方变更 + 两端（PC/固件）同步重建 + 回归通过后才可合并。
- LVGL 9.3.0 为不可变约束：PC 与设备必须同版本同 commit，不独立升级任一端。
- 协议（AppState schema）版本独立于依赖版本，见 protocol/ 与 docs/INTERFACES.md。
