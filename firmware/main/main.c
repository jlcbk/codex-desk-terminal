/*
 * main.c — P4.2（A3）ST7305 真机测试图案循环。
 *
 * 验收目标（docs/DEVELOPMENT_PLAN.md P4.2 行）：四角标记、棋盘、横竖线、
 * 文字方向正确，无花屏。屏幕图案循环持续运行，目检由用户执行；本程序
 * 串口侧打印每个图案帧名 + 整帧 CRC32（真源 shared/transport/cdt_crc32.c，
 * 与 zlib.crc32 逐位一致），同图案两次循环 CRC 相同即绘制确定性成立。
 *
 * 图案（每图案 4 秒，循环）：
 *   ① corners  四角 20x20 实心块 + 中心十字（臂长 ±20px）
 *   ② checker16 16px 棋盘格（25 x 18.75 块，左上块白）
 *   ③ lines30  横线 y=0,30..270 + 竖线 x=0,30..390（各 1px）
 *   ④ text4dir 内置 8x16 点阵 "CODEX" 按四方向绘制 + 1px 边框；正常横向居中
 *   ⑤ bwflash  全黑/全白各 1 秒交替，共 4 帧
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

#include "cdt_frame.h"
#include "st7305.h"

/*
 * CRC32：shared/transport/cdt_frame.h:91 冻结签名
 *   uint32_t cdt_crc32(const uint8_t *data, size_t len);
 * 该头与 shared/display/cdt_frame.h 同名且 include guard 相同（CDT_FRAME_H），
 * 同一翻译单元无法同时包含，故此处按冻结签名本地声明；实现链接
 * shared/transport/cdt_crc32.c（见 main/CMakeLists.txt）。
 */
uint32_t cdt_crc32(const uint8_t *data, size_t len);

#define TAG "p42"

#define PATTERN_MS 4000
#define FLASH_MS   1000

/* ------------------------------------------------------------------ */
/* 内置 8x16 点阵（手写最小集：C O D E X），行优先 16 行，MSB=最左列     */
/* ------------------------------------------------------------------ */
static const uint8_t k_font_c[16] = {
    0x00, 0x00, 0x00, 0x3C, 0x66, 0x42, 0x40, 0x40,
    0x40, 0x42, 0x66, 0x3C, 0x00, 0x00, 0x00, 0x00,
};
static const uint8_t k_font_o[16] = {
    0x00, 0x00, 0x00, 0x3C, 0x66, 0x42, 0x42, 0x42,
    0x42, 0x42, 0x66, 0x3C, 0x00, 0x00, 0x00, 0x00,
};
static const uint8_t k_font_d[16] = {
    0x00, 0x00, 0x00, 0x7C, 0x46, 0x43, 0x43, 0x43,
    0x43, 0x43, 0x46, 0x7C, 0x00, 0x00, 0x00, 0x00,
};
static const uint8_t k_font_e[16] = {
    0x00, 0x00, 0x00, 0x7E, 0x40, 0x40, 0x40, 0x7C,
    0x40, 0x40, 0x40, 0x7E, 0x00, 0x00, 0x00, 0x00,
};
static const uint8_t k_font_x[16] = {
    0x00, 0x00, 0x00, 0x42, 0x42, 0x24, 0x18, 0x18,
    0x18, 0x24, 0x42, 0x42, 0x00, 0x00, 0x00, 0x00,
};

static const uint8_t *font_glyph(char c)
{
    switch (c) {
    case 'C': return k_font_c;
    case 'O': return k_font_o;
    case 'D': return k_font_d;
    case 'E': return k_font_e;
    case 'X': return k_font_x;
    default:  return NULL;
    }
}

/* ------------------------------------------------------------------ */
/* 图案绘制（纯函数：同参数必产生同一帧 → CRC 可复现）                   */
/* ------------------------------------------------------------------ */

static void draw_rect(cdt_frame_t *f, int x0, int y0, int w, int h)
{
    for (int dy = 0; dy < h; dy++) {
        for (int dx = 0; dx < w; dx++) {
            cdt_frame_set(f, x0 + dx, y0 + dy, 1);
        }
    }
}

/* ① 四角 20x20 实心块 + 中心十字 */
static void draw_corners(cdt_frame_t *f, int frame_idx)
{
    (void)frame_idx;
    cdt_frame_clear(f, 0);
    draw_rect(f, 0, 0, 20, 20);
    draw_rect(f, CDT_FRAME_WIDTH - 20, 0, 20, 20);
    draw_rect(f, 0, CDT_FRAME_HEIGHT - 20, 20, 20);
    draw_rect(f, CDT_FRAME_WIDTH - 20, CDT_FRAME_HEIGHT - 20, 20, 20);
    for (int i = -20; i <= 20; i++) {
        cdt_frame_set(f, 200 + i, 150, 1); /* 横臂 41px */
        cdt_frame_set(f, 200, 150 + i, 1); /* 竖臂 41px */
    }
}

/* ② 16px 棋盘格：25 x 18.75 块，((x/16)+(y/16)) 偶 = 黑 */
static void draw_checker16(cdt_frame_t *f, int frame_idx)
{
    (void)frame_idx;
    cdt_frame_clear(f, 0);
    for (int y = 0; y < CDT_FRAME_HEIGHT; y++) {
        for (int x = 0; x < CDT_FRAME_WIDTH; x++) {
            if (((x >> 4) + (y >> 4)) & 1) {
                cdt_frame_set(f, x, y, 1);
            }
        }
    }
}

/* ③ 横线 y=0,30,...,270（10 条）+ 竖线 x=0,30,...,390（14 条） */
static void draw_lines30(cdt_frame_t *f, int frame_idx)
{
    (void)frame_idx;
    cdt_frame_clear(f, 0);
    for (int y = 0; y < CDT_FRAME_HEIGHT; y += 30) {
        for (int x = 0; x < CDT_FRAME_WIDTH; x++) {
            cdt_frame_set(f, x, y, 1);
        }
    }
    for (int x = 0; x < CDT_FRAME_WIDTH; x += 30) {
        for (int y = 0; y < CDT_FRAME_HEIGHT; y++) {
            cdt_frame_set(f, x, y, 1);
        }
    }
}

/* "CODEX" 5 字符 x 8px = 40 宽、16 高；rot: 0/90/180/270（顺时针） */
#define WORD_W 40
#define WORD_H 16

static void draw_text_rot(cdt_frame_t *f, const char *s, int x0, int y0, int rot)
{
    int wx = 0;
    for (const char *p = s; *p != '\0'; p++, wx += 8) {
        const uint8_t *g = font_glyph(*p);
        if (g == NULL) {
            continue;
        }
        for (int gy = 0; gy < WORD_H; gy++) {
            uint8_t bits = g[gy];
            for (int gx = 0; gx < 8; gx++) {
                int dx, dy;
                if (!((bits >> (7 - gx)) & 1)) {
                    continue;
                }
                switch (rot) {
                case 90:  /* 顺时针 90°：占 16 宽 x 40 高 */
                    dx = x0 + (WORD_H - 1 - gy);
                    dy = y0 + wx + gx;
                    break;
                case 180:
                    dx = x0 + (WORD_W - 1 - (wx + gx));
                    dy = y0 + (WORD_H - 1 - gy);
                    break;
                case 270: /* 逆时针 90°：占 16 宽 x 40 高 */
                    dx = x0 + gy;
                    dy = y0 + (WORD_W - 1 - (wx + gx));
                    break;
                default: /* 0 = 正常横向阅读方向 */
                    dx = x0 + wx + gx;
                    dy = y0 + gy;
                    break;
                }
                cdt_frame_set(f, dx, dy, 1);
            }
        }
    }
}

/* ④ 文字方向：四方向各一条 + 居中正常横向 + 1px 边框 */
static void draw_text4dir(cdt_frame_t *f, int frame_idx)
{
    (void)frame_idx;
    cdt_frame_clear(f, 0);
    for (int x = 0; x < CDT_FRAME_WIDTH; x++) {
        cdt_frame_set(f, x, 0, 1);
        cdt_frame_set(f, x, CDT_FRAME_HEIGHT - 1, 1);
    }
    for (int y = 0; y < CDT_FRAME_HEIGHT; y++) {
        cdt_frame_set(f, 0, y, 1);
        cdt_frame_set(f, CDT_FRAME_WIDTH - 1, y, 1);
    }
    draw_text_rot(f, "CODEX", 180, 142, 0);   /* 居中，正常横向 */
    draw_text_rot(f, "CODEX", 180, 40, 0);    /* 上方，正常横向 */
    draw_text_rot(f, "CODEX", 340, 130, 90);  /* 右侧，顺转 90° */
    draw_text_rot(f, "CODEX", 180, 244, 180); /* 下方，倒置 */
    draw_text_rot(f, "CODEX", 44, 130, 270);  /* 左侧，逆转 90° */
}

/* ⑤ 全黑/全白交替：帧 0/2 = 全黑，帧 1/3 = 全白 */
static void draw_bwflash(cdt_frame_t *f, int frame_idx)
{
    cdt_frame_clear(f, (frame_idx & 1) == 0);
}

typedef struct {
    const char *name;
    int frames;
    int frame_ms;
    void (*draw)(cdt_frame_t *f, int frame_idx);
} pattern_t;

static const pattern_t k_patterns[] = {
    { "corners",   1, PATTERN_MS, draw_corners },
    { "checker16", 1, PATTERN_MS, draw_checker16 },
    { "lines30",   1, PATTERN_MS, draw_lines30 },
    { "text4dir",  1, PATTERN_MS, draw_text4dir },
    { "bwflash",   4, FLASH_MS,  draw_bwflash },
};

#define PATTERN_COUNT (sizeof k_patterns / sizeof k_patterns[0])

void app_main(void)
{
    static cdt_frame_t frame; /* 15000 B BSS，避免任务栈占用 */

    ESP_LOGI(TAG, "P4.2 ST7305 test-pattern loop: logic 400x300@1bpp (1=black), "
                  "native 300x400 packed, SPI %d MHz, settle %d ms",
             ST7305_PCLK_HZ / 1000000, ST7305_SETTLE_MS);

    st7305_config_t cfg = st7305_default_config();
    esp_err_t err = st7305_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "st7305_init failed: %s — pattern loop aborted",
                 esp_err_to_name(err));
        return;
    }

    uint32_t cycle = 0;
    while (true) {
        for (size_t p = 0; p < PATTERN_COUNT; p++) {
            const pattern_t *pat = &k_patterns[p];
            for (int fi = 0; fi < pat->frames; fi++) {
                pat->draw(&frame, fi);
                uint32_t crc = cdt_crc32(frame.px, CDT_FRAME_BYTES);
                ESP_LOGI(TAG, "cycle=%lu pattern=%s frame=%d/%d bytes=%d crc32=0x%08lx",
                         (unsigned long)cycle, pat->name, fi + 1, pat->frames,
                         (int)CDT_FRAME_BYTES, (unsigned long)crc);
                err = st7305_flush(&frame);
                if (err != ESP_OK) {
                    ESP_LOGE(TAG, "flush failed: %s (pattern=%s frame=%d)",
                             esp_err_to_name(err), pat->name, fi + 1);
                }
                vTaskDelay(pdMS_TO_TICKS(pat->frame_ms));
            }
        }
        cycle++;
    }
}
