# Codex Desk Terminal

ESP32-S3-RLCD-4.2 桌面状态终端：抬眼看见 Codex 在做什么、是否需要处理、计划进度及额度。

当前状态：**开发计划已建立，尚未实现固件、Bridge 或模拟器，也尚未完成真机验证。**

## 从这里开始

1. 阅读 [完整开发计划](docs/DEVELOPMENT_PLAN.md)：范围、阶段、任务依赖、验收、功耗实验及资料。
2. 阅读 [接口契约草案](docs/INTERFACES.md)：统一 State JSON、传输边界、设备本地状态和版本规则。
3. 开发 Agent 阅读 [协作约定](AGENTS.md)，按计划中的任务编号认领工作。

```text
Codex app-server → Bridge / State Engine → BLE 或 Wi-Fi → ESP32-S3-RLCD-4.2
                            ↓                               ↓
                       Mock / Replay                共享 State + LVGL UI
                            └───────────────────────────────┤
                                                 SDL / ST7305
```

固定路线：ESP-IDF、LVGL **9.3.0**、SDL2、400×300 横屏、单色输出；模拟器优先。3.600V 定义为设备可用电量 0%，持续低压后进入 Deep Sleep。BLE 与 Wi-Fi 都实现，默认传输由同条件真机测量决定。

项目路径：`/Users/cui/Documents/Projects/codex-desk-terminal`

计划编制日期：2026-09-10。设计依据为用户当前要求及《调研Codex终端设计》对话；官方资料用于校核能力边界，协议细节以实施时锁定版本为准。
