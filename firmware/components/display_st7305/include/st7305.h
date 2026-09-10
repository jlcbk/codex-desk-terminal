/*
 * st7305.h — ST7305（4.2" 反射式单色 LCD）板级驱动，P4.2（A3）。
 *
 * 语义对齐 docs/INTERFACES.md §5 Display HAL（v1）：
 *   - 整帧刷新；flush 同步阻塞，SPI 传输（DMA）真正完成后才返回，
 *     一次 flush 恰好一次完成语义（内部 esp_lcd color-trans-done 回调只
 *     在最后一个分片触发一次，见 IDF v5.5.5 esp_lcd_panel_io_spi.c）。
 *   - 本板无 TE、无 busy 脚（docs/HARDWARE.md §1.1 冲突#10）：不读 busy、
 *     不依赖 TE；每帧写完后加固定 settle 延时兜底。
 *
 * 显示映射（docs/HARDWARE.md §3.1，官方 display_bsp.cpp 时序参照，只照抄
 * 时序/命令/打包公式，不照抄代码结构）：
 *   - 输入：shared/display cdt_frame_t，400x300 逻辑帧，行优先，字节内
 *     MSB=最左像素，1=黑/0=白（INTERFACES §5 冻结格式）。
 *   - 控制器原生按 300x400（竖）寻址；横屏为软件重排，不是控制器命令：
 *       inv_y   = 299 - y
 *       byte_x  = x >> 1            (0..199)
 *       block_y = inv_y >> 2        (0..74)
 *       index   = byte_x * 75 + block_y
 *       bit     = 7 - ((inv_y & 3) << 1 | (x & 1))
 *   - 极性：控制器 RAM 位 1=白、0=黑（官方例 ColorWhite=0xFF 全填后推帧
 *   得白屏），因此 cdt 的 1（黑）落为 RAM 0，0（白）落为 RAM 1。
 *   - 位序 MSB-first；整帧 RAM 15000 字节，一次 0x2C 连续写完。
 *
 * SPI 时钟：保守起步 10MHz（官方 IDF 例同值；HARDWARE 冲突#3：上限未标定，
 * 爬升属后续任务，须实测记录）。
 */
#ifndef CDT_ST7305_H
#define CDT_ST7305_H

#include <stdbool.h>
#include <stdint.h>

#include <driver/spi_master.h>
#include <esp_err.h>

#include "cdt_frame.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 板级引脚（docs/HARDWARE.md §1.1，全部 confirmed） */
#define ST7305_PIN_MOSI 12
#define ST7305_PIN_SCLK 11
#define ST7305_PIN_CS   40
#define ST7305_PIN_DC   5
#define ST7305_PIN_RST  41

/* SPI3_HOST（官方同配置）；MISO 不接（-1）；起始时钟 10MHz */
#define ST7305_SPI_HOST SPI3_HOST
#define ST7305_PCLK_HZ  (10 * 1000 * 1000)

/* 整帧写完后的兜底延时（无 busy 脚，HARDWARE 冲突#10）。 */
#define ST7305_SETTLE_MS 20

typedef struct {
    int mosi_gpio;
    int sclk_gpio;
    int cs_gpio;
    int dc_gpio;
    int rst_gpio;
    spi_host_device_t spi_host;
    int pclk_hz;
} st7305_config_t;

/* 返回上层可读的默认配置（引脚/主机/时钟）。 */
st7305_config_t st7305_default_config(void);

/*
 * 总线 + panel IO 初始化、硬件复位、官方时序 init 序列、推一整帧白底。
 * 成功后屏幕应为全白。重复调用前须先 deinit。
 */
esp_err_t st7305_init(const st7305_config_t *cfg);

/*
 * 整帧刷新：把 400x300 1bpp 逻辑帧重排到 300x400 原生打包并经 SPI 写出。
 * 同步语义：返回 = SPI 传输完成（含 ST7305_SETTLE_MS 兜底延时）；
 * completion 恰好一次（同步返回即完成，无二次回调）。frame 在返回后即可
 * 复用/改写。frame==NULL 或未 init 返回 ESP_ERR_INVALID_ARG/STATE。
 */
esp_err_t st7305_flush(const cdt_frame_t *frame);

/* 释放总线与缓冲（幂等）。 */
void st7305_deinit(void);

#ifdef __cplusplus
}
#endif

#endif /* CDT_ST7305_H */
