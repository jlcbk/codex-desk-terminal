/*
 * cdt_frame.h — 共享单色逻辑帧（P1.4，A0）
 *
 * 契约：INTERFACES §5 共享逻辑帧格式——400 宽×300 高、1bit/像素、
 * 行优先、每行 50 字节、字节内 MSB 对应左像素、1=黑/0=白，共 15000 字节。
 * 该格式用于测试/SDL 输出；ST7305 适配器另行转换（P4.2）。
 * 越界坐标一律忽略（防御性，不崩溃）。
 */
#ifndef CDT_FRAME_H
#define CDT_FRAME_H

#include <stdint.h>

#define CDT_FRAME_WIDTH 400
#define CDT_FRAME_HEIGHT 300
#define CDT_FRAME_STRIDE 50 /* (WIDTH+7)/8 */
#define CDT_FRAME_BYTES (CDT_FRAME_STRIDE * CDT_FRAME_HEIGHT) /* 15000 */

typedef struct {
    uint8_t px[CDT_FRAME_BYTES];
} cdt_frame_t;

/* value：非 0 = 黑（置 1），0 = 白（清 0）。*/
void cdt_frame_clear(cdt_frame_t *f, int black);
void cdt_frame_set(cdt_frame_t *f, int x, int y, int black);
int cdt_frame_get(const cdt_frame_t *f, int x, int y); /* 返回 0/1；越界返回 -1 */

#endif /* CDT_FRAME_H */
