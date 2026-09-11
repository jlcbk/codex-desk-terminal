/*
 * esp_stub.c — 主机测试桩实现：记录平台调用点计数（见 esp_stub.h）。
 */
#include "esp_stub.h"

#include "driver/gpio.h"
#include "esp_sleep.h"

cdt_esp_stub_counts_t g_esp_stub;

void cdt_esp_stub_reset(void)
{
    g_esp_stub.deep_sleep_start_calls = 0;
    g_esp_stub.ext0_calls = 0;
    g_esp_stub.ext0_last_gpio = -1;
    g_esp_stub.ext0_last_level = -1;
    g_esp_stub.gpio_set_direction_calls = 0;
    g_esp_stub.gpio_set_level_calls = 0;
    g_esp_stub.pa46_low_calls = 0;
}

void esp_deep_sleep_start(void)
{
    g_esp_stub.deep_sleep_start_calls++;
}

esp_err_t esp_sleep_enable_ext0_wakeup(gpio_num_t gpio_num, int level)
{
    g_esp_stub.ext0_calls++;
    g_esp_stub.ext0_last_gpio = (int)gpio_num;
    g_esp_stub.ext0_last_level = level;
    return ESP_OK;
}

int gpio_set_direction(gpio_num_t gpio_num, gpio_mode_t mode)
{
    (void)gpio_num;
    (void)mode;
    g_esp_stub.gpio_set_direction_calls++;
    return 0;
}

int gpio_set_level(gpio_num_t gpio_num, uint32_t level)
{
    g_esp_stub.gpio_set_level_calls++;
    if ((int)gpio_num == 46 && level == 0u) {
        g_esp_stub.pa46_low_calls++;
    }
    return 0;
}
