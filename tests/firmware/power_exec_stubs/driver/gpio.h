/*
 * driver/gpio.h — 主机测试桩（形状对齐 IDF v5.x：gpio_num_t/gpio_mode_t、
 * gpio_set_direction/gpio_set_level）。实现与计数在 esp_stub.c。
 */
#ifndef STUB_CDT_DRIVER_GPIO_H
#define STUB_CDT_DRIVER_GPIO_H

#include <stdint.h>

typedef int gpio_num_t;
typedef int gpio_mode_t;

#define GPIO_MODE_INPUT  ((gpio_mode_t)0)
#define GPIO_MODE_OUTPUT ((gpio_mode_t)2)

#define GPIO_NUM_18 ((gpio_num_t)18) /* KEY（HARDWARE §1.2）*/
#define GPIO_NUM_46 ((gpio_num_t)46) /* PA（HARDWARE §1.5）*/

int gpio_set_direction(gpio_num_t gpio_num, gpio_mode_t mode);
int gpio_set_level(gpio_num_t gpio_num, uint32_t level);

#endif /* STUB_CDT_DRIVER_GPIO_H */
