/*
 * esp_log.h — 主机测试桩：日志宏退化为空（顺序/计数断言经轨迹与桩计数完成）。
 */
#ifndef STUB_CDT_ESP_LOG_H
#define STUB_CDT_ESP_LOG_H

#define ESP_LOGI(tag, ...) ((void)(tag))
#define ESP_LOGW(tag, ...) ((void)(tag))
#define ESP_LOGE(tag, ...) ((void)(tag))

#endif /* STUB_CDT_ESP_LOG_H */
