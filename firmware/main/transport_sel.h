/*
 * transport_sel.h — 产品主流程传输选择（ZC11：WiFi/WSS 与 BLE peripheral 二选一）
 *
 * 契约真源：protocol/transport.md §1（同一时刻只有一个活动 transport）、
 * §2（GATT/配对冻结值）、§3（16B 帧头/ACK 语义）。选择经 firmware/main/Kconfig
 * 的 CDT_TRANSPORT_SEL 编译期冻结（0=WiFi/WSS 默认，1=BLE），运行期不再切换。
 *
 * 组织（ZC11 任务书）：选择逻辑全部封装在本模块，main.c 只留精确小改的调用
 * 点，降低并行任务在同文件的冲突面。
 *
 * 回归红线（SEL=0 默认路径）：本模块 WIFI 侧实现全部为常量折叠的 no-op，
 * 不引用任何 BLE 符号——不把 ble_peripheral.o 拉进默认固件链接，main.c 的
 * WiFi/WSS 行为零变化。
 *
 * 红线（SEL=1 BLE 路径）：BLE 收包与 WSS on_text 走同一 app_inbox 管线下游
 * （互斥短临界区/优先提醒槽语义不变，R4）；本模块不解析任何业务字段；
 * ACK/NACK 由 ble_peripheral 依 on_message 三值结果经 TX Notify 自动回发
 * （§3.4）；断连清半包由组件内部完成（§3.3 接收方 6）。
 */
#ifndef CDT_TRANSPORT_SEL_H
#define CDT_TRANSPORT_SEL_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 编译期选定（Kconfig CDT_TRANSPORT_SEL）：true=BLE peripheral 模式。
 * 返回编译期常量，调用点分支可被优化器折叠。 */
bool cdt_transport_sel_is_ble(void);

/* 运行链路名（Runtime.transport："wifi"/"ble"；cdt_runtime.h §1a 枚举 ≤16B）。 */
const char *cdt_transport_sel_name(void);

/* BLE 模式：启动 NimBLE 外设（冻结 UUID 广播，transport.md §2.1/§2.2）。
 * WIFI 模式为 no-op。幂等：重复调用不重复启动（运行期 ALLOW_RADIO_START
 * 可能重复到达；组件对二次 start 本身也返回 INVALID_STATE）。 */
void cdt_transport_sel_ble_start(void);

/* 电池保护停无线路径（main cb_stop_radio → do_sleep_sequence §7.3）：
 * BLE 模式停外设（断连即清半包）；WIFI 模式 no-op——wss/dev_net 仍由
 * main 照旧直停，WIFI 行为零变化。 */
void cdt_transport_sel_radio_stop(void);

/* BLE 链路是否已连接（GAP 连接事件镜像；WIFI 模式恒 false，调用方不使用）。 */
bool cdt_transport_sel_ble_link_up(void);

/* 主循环巡检：BLE 模式驱动重组器 3s 进展/30s 整包超时（NACK 由组件发出，
 * §3.3 接收方 4）；WIFI 模式 no-op。 */
bool cdt_transport_sel_poll(int64_t now_ms);

#ifdef __cplusplus
}
#endif

#endif /* CDT_TRANSPORT_SEL_H */
