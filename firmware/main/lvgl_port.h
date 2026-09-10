/*
 * lvgl_port.h — 最小 LVGL 9.3 固件 port（P4.3，A2+A3；不用第三方 port）。
 *
 * 职责仅两件（照 INTERFACES §5 全帧先行模型）：
 *   1. 在 ST7305 之上创建 LVGL display：400x300、I1、LV_DISPLAY_RENDER_MODE_FULL、
 *      单个整屏绘制缓冲。LVGL I1 位语义 1=白/0=黑（P1.3 记录），cdt 契约 1=黑/0=白；
 *      flush_cb 内按行取反（I1@400 行距恰 50 字节 = cdt_frame_t 布局，整行 ~ 即可），
 *      再同步调用 st7305_flush（返回即 SPI 完成），然后 lv_display_flush_ready。
 *   2. 时基由 lv_conf.h 的 LV_TICK_CUSTOM（esp_timer 毫秒）提供，无 tick 任务；
 *      lv_timer_handler 由宿主 ui 任务以 ~10ms 周期驱动（见 main.c）。
 *
 * 不做：局部刷新（全帧先行）、双缓冲、触摸/按键 indev（P4.5）、旋转（st7305 驱动
 * 内部原生重排，P4.2 已验证）。
 */
#ifndef CDT_LVGL_PORT_H
#define CDT_LVGL_PORT_H

#include <stdint.h>

#include "cdt_frame.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * lv_init + 创建 display/绘制缓冲/flush_cb。
 * 成功后活动屏幕可用；随后宿主调 cdt_ui_init() 建页面。
 * 重复调用返回 ESP_ERR_INVALID_STATE。
 */
esp_err_t cdt_lvgl_port_display_init(void);

/*
 * 强制完整刷新：lv_obj_invalidate(活动屏幕) + lv_refr_now（FULL 模式必整屏重绘）。
 * 同步语义：返回 = 本帧已完整写入面板（flush_cb 内 st7305_flush 已返回）。
 * 此后 cdt_lvgl_port_frame() 即为本帧的逻辑帧（1=黑）。
 */
void cdt_lvgl_port_refresh(void);

/* 最近一次完整刷新的逻辑帧（cdt 语义 1=黑，15000 字节）。仅 ui 任务内读取。 */
const cdt_frame_t *cdt_lvgl_port_frame(void);

#ifdef __cplusplus
}
#endif

#endif /* CDT_LVGL_PORT_H */
