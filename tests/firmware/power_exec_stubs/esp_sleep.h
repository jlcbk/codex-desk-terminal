/*
 * esp_sleep.h — 主机测试桩（形状对齐 IDF v5.x 所用子集）。
 * 实现与计数在 esp_stub.c：KEY 深睡唤醒宏（CDT_CFG_KEY_DEEP_WAKE）关闭时，
 * 测试断言 esp_sleep_enable_ext0_wakeup 调用数为 0。
 */
#ifndef STUB_CDT_ESP_SLEEP_H
#define STUB_CDT_ESP_SLEEP_H

#include "esp_err.h"
#include "driver/gpio.h"

void esp_deep_sleep_start(void);
esp_err_t esp_sleep_enable_ext0_wakeup(gpio_num_t gpio_num, int level);

#endif /* STUB_CDT_ESP_SLEEP_H */
