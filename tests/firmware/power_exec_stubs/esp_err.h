/*
 * esp_err.h — 主机测试桩（P5.3，tests/firmware/power_exec_stubs/）
 *
 * 仅用于把 firmware/components/power/cdt_power_exec.c 编进宿主 cc：
 * 提供 esp_err_t/ESP_OK 形状。IDF 构建不经过此目录。
 */
#ifndef STUB_CDT_ESP_ERR_H
#define STUB_CDT_ESP_ERR_H

typedef int esp_err_t;
#define ESP_OK 0

#endif /* STUB_CDT_ESP_ERR_H */
