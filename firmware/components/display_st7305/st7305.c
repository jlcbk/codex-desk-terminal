/*
 * st7305.c — P4.2（A3）实现，见 include/st7305.h 头注。
 *
 * 命令/时序真源：Waveshare 官方 ESP-IDF 例 display_bsp.cpp RLCD_Init()
 * （vendor/waveshare-rlcd/02_Example/ESP-IDF/09_LVGL_V9_Test/components/
 * port_bsp/display_bsp.cpp，HEAD eb1f634）。逐条命令与延时照抄时序，
 * 不复制其类结构；传输用 IDF v5.5.5 esp_lcd panel IO（与官方例同一后端）。
 *
 * 红线（docs/HARDWARE.md §7 冲突#10、任务书）：不做 TE 依赖、不读 busy、
 * 时钟不冒进（10MHz 起步）；写后延时兜底。
 */
#include "st7305.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_types.h"
#include "esp_log.h"

#define TAG "st7305"

/* 原生帧：300x400 打包后 15000 字节（= 400*300/8，与逻辑帧同长） */
#define NATIVE_BYTES CDT_FRAME_BYTES
/* 打包块：byte_x 0..199，block_y 0..74（每字节覆盖 2 列 x 4 行逻辑像素） */
#define NATIVE_COL_BLOCKS 200u /* 400/2 */
#define NATIVE_BLOCK_ROWS 75u  /* 300/4 */

/* 官方窗口常量：列 0x12..0x2A、页 0x00..0xC7（display_bsp.cpp:166-172/191-197） */
#define WIN_COL_START 0x12u
#define WIN_COL_END   0x2Au
#define WIN_PAGE_START 0x00u
#define WIN_PAGE_END   0xC7u

/* 完成等待上限：15000B @10MHz 约 12ms，留 40 倍裕量 */
#define TRANS_DONE_TIMEOUT_MS 500

static spi_host_device_t s_host = ST7305_SPI_HOST;
static esp_lcd_panel_io_handle_t s_io;
static uint8_t *s_native;            /* 15000B，DMA 可达内存 */
static SemaphoreHandle_t s_trans_done;

st7305_config_t st7305_default_config(void)
{
    st7305_config_t cfg = {
        .mosi_gpio = ST7305_PIN_MOSI,
        .sclk_gpio = ST7305_PIN_SCLK,
        .cs_gpio   = ST7305_PIN_CS,
        .dc_gpio   = ST7305_PIN_DC,
        .rst_gpio  = ST7305_PIN_RST,
        .spi_host  = ST7305_SPI_HOST,
        .pclk_hz   = ST7305_PCLK_HZ,
    };
    return cfg;
}

/* color 分片传完（最后一分片）回调一次；IDF spi_master post_cb 在中断上下文 */
static bool trans_done_cb(esp_lcd_panel_io_handle_t io,
                          esp_lcd_panel_io_event_data_t *edata, void *ctx)
{
    (void)io; (void)edata; (void)ctx;
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_trans_done, &woken);
    return woken == pdTRUE;
}

static esp_err_t cmd(uint8_t reg)
{
    return esp_lcd_panel_io_tx_param(s_io, reg, NULL, 0);
}

static esp_err_t cmd_data(uint8_t reg, const uint8_t *data, size_t len)
{
    return esp_lcd_panel_io_tx_param(s_io, reg, data, len);
}

/* 复位时序照抄官方 RLCD_Reset：高 50ms → 低 20ms → 高 50ms */
static void hw_reset(int rst_gpio)
{
    gpio_set_level((gpio_num_t)rst_gpio, 1);
    vTaskDelay(pdMS_TO_TICKS(50));
    gpio_set_level((gpio_num_t)rst_gpio, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level((gpio_num_t)rst_gpio, 1);
    vTaskDelay(pdMS_TO_TICKS(50));
}

/* 设窗口 + 0x2C + 整帧连续写（官方 RLCD_Display 同序）。命令走 polling
 * 同步；颜色数据走队列，完成后由 trans_done_cb 放信号量。 */
static esp_err_t push_native_frame(void)
{
    static const uint8_t col_win[2] = { WIN_COL_START, WIN_COL_END };
    static const uint8_t page_win[2] = { WIN_PAGE_START, WIN_PAGE_END };

    esp_err_t err = cmd_data(0x2A, col_win, sizeof(col_win));
    if (err != ESP_OK) return err;
    err = cmd_data(0x2B, page_win, sizeof(page_win));
    if (err != ESP_OK) return err;

    xSemaphoreTake(s_trans_done, 0); /* 清陈旧信号，本次传输完成只认一次 */
    err = cmd(0x2C);
    if (err != ESP_OK) return err;
    err = esp_lcd_panel_io_tx_color(s_io, -1, s_native, NATIVE_BYTES);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "tx_color failed: %s", esp_err_to_name(err));
        return err;
    }
    if (xSemaphoreTake(s_trans_done, pdMS_TO_TICKS(TRANS_DONE_TIMEOUT_MS)) != pdTRUE) {
        ESP_LOGE(TAG, "flush completion timeout (%d ms)", TRANS_DONE_TIMEOUT_MS);
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

esp_err_t st7305_init(const st7305_config_t *cfg)
{
    if (cfg == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_io != NULL || s_native != NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    s_host = cfg->spi_host;
    s_trans_done = xSemaphoreCreateBinary();
    if (s_trans_done == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* RST 脚：输出 + 上拉（官方构造函数同配置） */
    gpio_config_t io_conf = {
        .intr_type = GPIO_INTR_DISABLE,
        .mode = GPIO_MODE_OUTPUT,
        .pin_bit_mask = 1ULL << cfg->rst_gpio,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    esp_err_t err = gpio_config(&io_conf);
    if (err != ESP_OK) goto fail;

    /* 写-only 总线：MISO=-1，DMA 自动通道；max_transfer 覆盖整帧 */
    spi_bus_config_t buscfg = {
        .miso_io_num = -1,
        .mosi_io_num = cfg->mosi_gpio,
        .sclk_io_num = cfg->sclk_gpio,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = (int)NATIVE_BYTES,
    };
    err = spi_bus_initialize(s_host, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) goto fail;

    esp_lcd_panel_io_spi_config_t io_cfg = {
        .dc_gpio_num = cfg->dc_gpio,
        .cs_gpio_num = cfg->cs_gpio,
        .pclk_hz = cfg->pclk_hz,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .spi_mode = 0,
        .trans_queue_depth = 10,
        .on_color_trans_done = trans_done_cb,
        .user_ctx = NULL,
    };
    err = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)s_host, &io_cfg, &s_io);
    if (err != ESP_OK) goto fail_bus;

    /* 整帧缓冲优先内部 DMA RAM（确定性 DMA 可达）；不足再退 PSRAM */
    s_native = heap_caps_malloc(NATIVE_BYTES, MALLOC_CAP_DMA | MALLOC_CAP_8BIT);
    if (s_native == NULL) {
        ESP_LOGW(TAG, "internal DMA alloc failed, falling back to PSRAM");
        s_native = heap_caps_malloc(NATIVE_BYTES, MALLOC_CAP_SPIRAM);
    }
    if (s_native == NULL) { err = ESP_ERR_NO_MEM; goto fail_io; }

    hw_reset(cfg->rst_gpio);

    /* ---- init 序列：逐条对照官方 RLCD_Init() ---- */
    static const uint8_t d6[]  = {0x17, 0x02};
    static const uint8_t d1[]  = {0x01};
    static const uint8_t c0[]  = {0x11, 0x04};
    static const uint8_t c1[]  = {0x69, 0x69, 0x69, 0x69};
    static const uint8_t c2[]  = {0x19, 0x19, 0x19, 0x19};
    static const uint8_t c4[]  = {0x4B, 0x4B, 0x4B, 0x4B};
    static const uint8_t c5[]  = {0x19, 0x19, 0x19, 0x19};
    static const uint8_t d8[]  = {0x80, 0xE9};
    static const uint8_t b2[]  = {0x02};
    static const uint8_t b3[]  = {0xE5, 0xF6, 0x05, 0x46, 0x77, 0x77, 0x77, 0x77, 0x76, 0x45};
    static const uint8_t b4[]  = {0x05, 0x46, 0x77, 0x77, 0x77, 0x77, 0x76, 0x45};
    static const uint8_t r62[] = {0x32, 0x03, 0x1F};
    static const uint8_t b7[]  = {0x13};
    static const uint8_t b0[]  = {0x64};
    static const uint8_t r36[] = {0x48};
    static const uint8_t r3a[] = {0x11};
    static const uint8_t b9[]  = {0x20};
    static const uint8_t b8[]  = {0x29};
    static const uint8_t r35[] = {0x00};
    static const uint8_t d0[]  = {0xFF};

    struct { uint8_t cmd; const uint8_t *data; size_t len; } seq[] = {
        {0xD6, d6, sizeof d6},   /* NVM Load Control */
        {0xD1, d1, sizeof d1},   /* Booster Enable */
        {0xC0, c0, sizeof c0},   /* Gate Voltage Control */
        {0xC1, c1, sizeof c1},   /* VSHP Setting */
        {0xC2, c2, sizeof c2},
        {0xC4, c4, sizeof c4},
        {0xC5, c5, sizeof c5},
        {0xD8, d8, sizeof d8},
        {0xB2, b2, sizeof b2},
        {0xB3, b3, sizeof b3},
        {0xB4, b4, sizeof b4},
        {0x62, r62, sizeof r62},
        {0xB7, b7, sizeof b7},
        {0xB0, b0, sizeof b0},
    };
    for (size_t i = 0; i < sizeof seq / sizeof seq[0]; i++) {
        err = cmd_data(seq[i].cmd, seq[i].data, seq[i].len);
        if (err != ESP_OK) goto fail_io;
    }

    err = cmd(0x11); /* Sleep Out；官方后延 200ms */
    if (err != ESP_OK) goto fail_io;
    vTaskDelay(pdMS_TO_TICKS(200));

    static const uint8_t c9[] = {0x00};
    err = cmd_data(0xC9, c9, sizeof c9);
    if (err != ESP_OK) goto fail_io;
    err = cmd_data(0x36, r36, sizeof r36); /* MADCTL */
    if (err != ESP_OK) goto fail_io;
    err = cmd_data(0x3A, r3a, sizeof r3a); /* 像素格式（mono 2-dot） */
    if (err != ESP_OK) goto fail_io;
    err = cmd_data(0xB9, b9, sizeof b9);
    if (err != ESP_OK) goto fail_io;
    err = cmd_data(0xB8, b8, sizeof b8);
    if (err != ESP_OK) goto fail_io;
    err = cmd(0x21); /* Display Inversion On */
    if (err != ESP_OK) goto fail_io;
    err = cmd_data(0x35, r35, sizeof r35);
    if (err != ESP_OK) goto fail_io;
    err = cmd_data(0xD0, d0, sizeof d0);
    if (err != ESP_OK) goto fail_io;
    err = cmd(0x38);
    if (err != ESP_OK) goto fail_io;
    err = cmd(0x29); /* Display On */
    if (err != ESP_OK) goto fail_io;

    /* 初屏全白（RAM 位 1=白，与官方 ColorClear(ColorWhite) 等效） */
    memset(s_native, 0xFF, NATIVE_BYTES);
    err = push_native_frame();
    if (err != ESP_OK) goto fail_io;
    vTaskDelay(pdMS_TO_TICKS(ST7305_SETTLE_MS));

    ESP_LOGI(TAG, "init ok: SPI%d @%d MHz, native 300x400, frame %u B",
             (int)s_host + 1, cfg->pclk_hz / 1000000, (unsigned)NATIVE_BYTES);
    return ESP_OK;

fail_io:
    if (s_io != NULL) {
        esp_lcd_panel_io_del(s_io);
        s_io = NULL;
    }
fail_bus:
    spi_bus_free(s_host);
fail:
    if (s_native != NULL) { heap_caps_free(s_native); s_native = NULL; }
    if (s_trans_done != NULL) { vSemaphoreDelete(s_trans_done); s_trans_done = NULL; }
    ESP_LOGE(TAG, "init failed: %s", esp_err_to_name(err));
    return err;
}

/* 逻辑帧(400x300,1=黑) → 原生 300x400 打包（公式见头注）。
 * 按原生字节遍历：每字节覆盖 byte_x 的 2 列 x block_y 的 4 行。 */
static void convert_frame(const cdt_frame_t *frame)
{
    for (uint32_t byte_x = 0; byte_x < NATIVE_COL_BLOCKS; byte_x++) {
        uint8_t *out = &s_native[byte_x * NATIVE_BLOCK_ROWS];
        for (uint32_t block_y = 0; block_y < NATIVE_BLOCK_ROWS; block_y++) {
            uint8_t b = 0;
            for (uint32_t local_y = 0; local_y < 4; local_y++) {
                uint32_t inv_y = block_y * 4u + local_y;
                uint32_t y = 299u - inv_y;
                const uint8_t *row = &frame->px[y * CDT_FRAME_STRIDE];
                for (uint32_t local_x = 0; local_x < 2; local_x++) {
                    uint32_t x = byte_x * 2u + local_x;
                    uint32_t bit = 7u - ((local_y << 1) | local_x);
                    /* cdt:1=黑 → RAM 0；cdt:0=白 → RAM 1 */
                    if (!((row[x >> 3] >> (7u - (x & 7u))) & 1u)) {
                        b |= (uint8_t)(1u << bit);
                    }
                }
            }
            out[block_y] = b;
        }
    }
}

esp_err_t st7305_flush(const cdt_frame_t *frame)
{
    if (frame == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_io == NULL || s_native == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    convert_frame(frame);

    esp_err_t err = push_native_frame();
    if (err != ESP_OK) {
        return err;
    }
    /* 无 busy/TE：写后固定延时兜底（HARDWARE 冲突#10） */
    vTaskDelay(pdMS_TO_TICKS(ST7305_SETTLE_MS));
    return ESP_OK;
}

void st7305_deinit(void)
{
    if (s_io != NULL) {
        esp_lcd_panel_io_del(s_io);
        s_io = NULL;
    }
    spi_bus_free(s_host);
    if (s_native != NULL) {
        heap_caps_free(s_native);
        s_native = NULL;
    }
    if (s_trans_done != NULL) {
        vSemaphoreDelete(s_trans_done);
        s_trans_done = NULL;
    }
}
