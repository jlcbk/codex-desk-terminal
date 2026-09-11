/*
 * esp_stub.h — 主机测试桩计数器（P5.3 链接桩断言用）。
 * 记录 cdt_power_exec.c 在宿主构建中对平台调用点的调用情况：
 *   - esp_sleep_enable_ext0_wakeup（KEY 深睡唤醒配置，宏关闭必须为 0）
 *   - esp_deep_sleep_start（深睡入口必须恰好一次/流程）
 *   - gpio_set_direction/gpio_set_level（PA=46 安全电平）
 */
#ifndef STUB_CDT_ESP_STUB_H
#define STUB_CDT_ESP_STUB_H

typedef struct {
    int deep_sleep_start_calls;
    int ext0_calls;
    int ext0_last_gpio;
    int ext0_last_level;
    int gpio_set_direction_calls;
    int gpio_set_level_calls;
    int pa46_low_calls; /* GPIO46 且 level==0 的调用次数 */
} cdt_esp_stub_counts_t;

extern cdt_esp_stub_counts_t g_esp_stub;

void cdt_esp_stub_reset(void);

#endif /* STUB_CDT_ESP_STUB_H */
