/*
 * lvgl_port.c — 最小 LVGL 固件 port 实现（P4.3），契约见 lvgl_port.h。
 *
 * 位序/取反依据：
 *   - LVGL I1（LV_COLOR_DEPTH=1）缓冲：行距 = lv_draw_buf_width_to_stride(400, I1)
 *     = 50 字节（LV_DRAW_BUF_STRIDE_ALIGN 默认 1，与模拟器同 vendor 同默认），
 *     MSB=左像素、1=白/0=黑；
 *   - cdt_frame_t：行距 50 字节、MSB=左像素、1=黑/0=白（INTERFACES §5 冻结）；
 *   → 逐行按字节取反即完成语义转换（400px=50B 整除，无 pad 位）。
 * FULL 模式下 LVGL 保证失效区折叠为整屏（vendor/lvgl/src/core/lv_refr.c
 * invalidate 折叠逻辑），flush_cb 走整帧快速路径；非整屏 area 走通用逐位
 * 路径兜底（正常不会触发）。
 */
#include "lvgl_port.h"

#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "lvgl.h"
#include "st7305.h"

#define TAG "lvgl_port"

/* LVGL 绘制缓冲：整屏 I1 像素 15000B + 头部冗余（静态，避免运行期分配不确定性）。
 * 冗余量必须覆盖 lv_draw_buf_reshape 的 _calculate_draw_buf_size：
 * stride*h + I1 调色板 2*4 + LV_DRAW_BUF_ALIGN(4) = 15012B；不足时 reshape 返回
 * NULL，layer_reshape_draw_buf 的 LV_ASSERT_NULL 在本配置下编译为死循环（实测
 * 复现：task WDT 反复触发，栈顶 layer_reshape_draw_buf 自旋）。渲染仍只写前
 * 15000B（stride 50 * 300），flush_cb 快速路径按 15000B 取反，冗余区不参与。
 * 整机集成（真机 1301）：内部 RAM 紧张（esp-aes DMA 弹跳缓冲曾分配失败），
 * 本缓冲仅 CPU 访问（LVGL 渲染 + flush_cb 逐行取反）→ 移入 PSRAM 静态区；
 * SPI DMA 源（s_logical，st7305_flush 输入）保持内部 RAM。 */
#define LVGL_PORT_BUF_EXTRA 64
EXT_RAM_BSS_ATTR static uint8_t s_draw_buf[CDT_FRAME_BYTES + LVGL_PORT_BUF_EXTRA] __attribute__((aligned(64)));
/* flush 落地的 cdt 逻辑帧（CRC 与面板 flush 共用同一份；SPI DMA 源，留内部 RAM） */
static cdt_frame_t s_logical;
static bool s_inited;

/* I1 缓冲前 8 字节为调色板区（LVGL 9.3 约定：索引格式的像素从 data+palette
 * 开始——lv_draw_buf_goto_xy 渲染寻址时跳过调色板，官方 lv_sdl_window.c:259
 * flush 同样跳过；px_map 直指缓冲首字节，必须先 +8 再按像素行解析） */
#define I1_PALETTE_BYTES (LV_COLOR_INDEXED_PALETTE_SIZE(LV_COLOR_FORMAT_I1) * (int)sizeof(lv_color32_t))

/* 通用路径：任意 area 的 LVGL I1（1=白）→ cdt（1=黑）。
 * src 行距按 area 宽重算（与 LVGL flush 传入缓冲的实际布局一致）。 */
static void convert_area(const lv_area_t *a, const uint8_t *src)
{
    const int32_t w = a->x2 - a->x1 + 1;
    const uint32_t stride = (uint32_t)((w + 7) / 8);

    for (int32_t y = a->y1; y <= a->y2; y++) {
        const uint8_t *srow = src + (size_t)(y - a->y1) * stride;
        uint8_t *drow = &s_logical.px[(size_t)y * CDT_FRAME_STRIDE];
        for (int32_t x = a->x1; x <= a->x2; x++) {
            const int32_t i = x - a->x1;
            const int white = (srow[i >> 3] >> (7 - (i & 7))) & 1;
            if (white) {
                drow[x >> 3] &= (uint8_t)~(0x80u >> (x & 7));
            }
            else {
                drow[x >> 3] |= (uint8_t)(0x80u >> (x & 7));
            }
        }
    }
}

static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map)
{
    const uint8_t *src = px_map + I1_PALETTE_BYTES; /* 跳过调色板区到像素数据 */

    if (area->x1 == 0 && area->y1 == 0 &&
        area->x2 == CDT_FRAME_WIDTH - 1 && area->y2 == CDT_FRAME_HEIGHT - 1) {
        /* 整帧快速路径：逐行取反（1=白 → 1=黑） */
        for (int y = 0; y < CDT_FRAME_HEIGHT; y++) {
            const uint8_t *srow = src + (size_t)y * CDT_FRAME_STRIDE;
            uint8_t *dst = &s_logical.px[(size_t)y * CDT_FRAME_STRIDE];
            for (int i = 0; i < CDT_FRAME_STRIDE; i++) {
                dst[i] = (uint8_t)~srow[i];
            }
        }
    }
    else {
        convert_area(area, src); /* 防御路径（FULL 模式不应出现） */
    }

    esp_err_t err = st7305_flush(&s_logical); /* 同步：返回即 SPI 完成 */
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "st7305_flush failed: %s", esp_err_to_name(err));
    }
    lv_display_flush_ready(disp);
}

esp_err_t cdt_lvgl_port_display_init(void)
{
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    lv_init();
    lv_display_t *disp = lv_display_create(CDT_FRAME_WIDTH, CDT_FRAME_HEIGHT);
    if (disp == NULL) {
        return ESP_FAIL;
    }
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_I1);
    lv_display_set_flush_cb(disp, flush_cb);
    lv_display_set_buffers(disp, s_draw_buf, NULL, (uint32_t)sizeof(s_draw_buf),
                           LV_DISPLAY_RENDER_MODE_FULL);

    /* 初帧白底（cdt 全 0；flush 前面板已由 st7305_init 推过白帧） */
    memset(&s_logical, 0, sizeof(s_logical));
    s_inited = true;
    ESP_LOGI(TAG, "LVGL %d.%d.%d display 400x300 I1 FULL-mode, draw buf %u B "
                  "(pixels 15000 + reshape/palette slack, stride 50, "
                  "1=white -> cdt 1=black)",
             lv_version_major(), lv_version_minor(), lv_version_patch(),
             (unsigned)sizeof(s_draw_buf));
    return ESP_OK;
}

void cdt_lvgl_port_refresh(void)
{
    lv_obj_invalidate(lv_screen_active());
    lv_refr_now(lv_display_get_default()); /* 完整刷新 + 同步 flush */
}

const cdt_frame_t *cdt_lvgl_port_frame(void)
{
    return &s_logical;
}
