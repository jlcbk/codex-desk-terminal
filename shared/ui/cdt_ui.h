/*
 * cdt_ui.h — 共享 LVGL UI 入口（P2.1，A2）
 *
 * 契约：docs/INTERFACES.md §5 UI 行——ui_init(display) / ui_apply(view) /
 * ui_key(event)；只在 LVGL 所属任务操作对象；无网络/ADC/文件 IO。
 * ViewModel（cdt_view_t）是 UI 唯一输入。
 *
 * 边界（任务红线）：
 *   - 本层不调用 ESP-IDF/网络/ADC/文件 IO；
 *   - 不解释 Codex 事件（那是 Bridge/Presenter 的事）；
 *   - P2.1 仅 NOW 单页，KEY 导航在 P2.2 完善。
 */
#ifndef CDT_UI_H
#define CDT_UI_H

#include <stddef.h>

#include "cdt_view.h"
#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* KEY 事件（设备端 KEY；模拟器由 SDL 键盘映射产生）*/
typedef enum {
    CDT_KEY_SHORT_PRESS = 1, /* 短按：页面轮换（P2.2） */
    CDT_KEY_LONG_PRESS = 2   /* 长按：静音当前提醒（P2.2 接 Runtime） */
} cdt_key_event_t;

/* 在当前活动屏幕上构建全部已实现页面（P2.1：NOW）。此后 ui_apply 只改属性。*/
void cdt_ui_init(void);

/* 用 ViewModel 刷新 UI（可在每次 present 后调用；只做属性设置，不重建对象）。*/
void cdt_ui_apply(const cdt_view_t *view);

/* KEY 事件骨架：P2.1 仅 NOW 单页——短按无动作，长按静音翻转留桩（P2.2 完善）。*/
void cdt_ui_key(cdt_key_event_t ev);

/*
 * 显示兜底：把 src 中的非 ASCII 码点替换为可见占位符 '?'（每码点一个）。
 * P2.1 只带 ASCII 内置字体（Noto Sans SC 子集属 P2.2）；§6 要求未知字符
 * 用"可见替代符"，不静默缺字。控制字符同样折为空格。
 * dst 与 src 可不重叠；dstsz 含结尾 NUL。
 */
void cdt_ui_ascii_safe(char *dst, size_t dstsz, const char *src);

#ifdef __cplusplus
}
#endif

#endif /* CDT_UI_H */
