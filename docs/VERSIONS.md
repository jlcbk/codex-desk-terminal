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
| LVGL（PC 与设备同一份） | 9.3.0 | tag `v9.3.0`，commit `c033a98afddd65aaafeebea625382a94020fe4a7`（tag 对象 `108e5aff3c90cc4d969331ed61aff2bbd365d430`），发布 2025-06-03 | github.com/lvgl/lvgl，`git ls-remote` 与 release 页双核对 |
| SDL2（模拟器宿主） | 2.30.12 | tag `release-2.30.12`（`8236e01a9f758d15927624925c6043f84d8a261f`），2.30.x 线共 13 个 tag 中的最新 | github.com/libsdl-org/SDL，`git ls-remote` |
| codex CLI（协议探针与 Bridge 上游） | 0.152.0 | `codex --version` = codex-cli 0.152.0；本机路径 /Users/cui/.local/lib/node-v24.19.0-darwin-arm64/bin/codex | 本机安装；app-server 协议由该版本 generate-json-schema 生成存档（见 docs/proto-samples/） |
| Bridge Python | CPython 3.12.x | 经 uv 安装并固定（`uv python install 3.12` + 项目内 lock）；标准库优先，第三方包逐个审批入库 | python.org 经 uv 分发 |
| 字体（CJK） | Noto Sans SC（SIL OFL 1.1） | 源：github.com/notofonts/noto-cjk（OFL 授权）；经 LVGL v9 官方 font converter 生成子集；子集字符表在 P2.1 冻结并记录可用范围 | 授权允许嵌入分发 |
| ESP-IDF（固件构建） | 5.5.5 | tag `v5.5.5`（`ff1bac0aeecdd2b797b9c3a558c6bd03629bc013`） | 厂商 wiki 要求 ≥V5.5.0（docs/HARDWARE.md §6）；vendor 仓库自带 sdkconfig 由 5.4.3 生成（已记录冲突），P4.1 须用本锁定版重建官方最小例验证 |
| Bleak（macOS BLE central） | 3.0.2 | PyPI 实查 2026-09-10（pip index+项目页双源）；MIT；requires_python ≥3.10，兼容 Bridge CPython 3.12 | P0.5 选型；3.x 与 1.x 的 API 差异以 P3.4 小样实测为准 |
| websockets（Bridge WSS server） | 17.1 | PyPI 实查 2026-09-10，2026-08-26 发布；BSD-3-Clause；requires_python ≥3.11 | P0.5 选型 |

## 待锁定（阻塞于并行任务结果）

| 依赖 | 计划 | 阻塞于 |
|---|---|---|
| JSON 解析库（C，共享/固件） | 候选收紧上限的流式解析器（P1.4 评审后锁定） | P1.4 |

## 宿主缺口与既定方案（无 Homebrew）

- **cmake**：P1.3 起，经 uv 环境安装 PyPI cmake 发行包（或 cmake.org 官方 arm64 tarball），版本写入本表后不再浮动。
- **SDL2**：从 github.com/libsdl-org/SDL 以锁定的 `release-2.30.12` tag 源码构建（Apple clang），或官方 release 的 arm64 dmg；安装方式与路径在 P1.3 定稿并回填本表。禁止"最新版"措辞。
- **ESP-IDF**：届时经 Espressif 官方安装器/git tag 安装（不装 latest 浮动分支），版本回填本表。

## 变更规则

- 任何版本升级 = A0 单方变更 + 两端（PC/固件）同步重建 + 回归通过后才可合并。
- LVGL 9.3.0 为不可变约束：PC 与设备必须同版本同 commit，不独立升级任一端。
- 协议（AppState schema）版本独立于依赖版本，见 protocol/ 与 docs/INTERFACES.md。
