/*
 * cdt_key.c — KEY/BOOT GPIO 轮询接线（P4.5 固件侧，A3；仅编译级验证）
 *
 * 接线模式照官方 10_FactoryProgram 的 button_bsp（HARDWARE §1.2 审计）：
 * 两脚 GPIO_MODE_INPUT + 内部上拉 + 中断禁用，周期 tick 轮询（官方 5ms），
 * 事件判定在本组件纯状态机 cdt_key_pure（主机端单测覆盖，真机按压实测留
 * 后续交互任务）。
 *
 * 未验证（诚实标注）：真机按压手感/去抖窗口是否 30ms 充分、GPIO0 strapping
 * 上电态对首轮采样的影响——全部留真机清单；本文件不含任何实测结论。
 */
#include <string.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/gpio.h"

#include "cdt_key.h"

#define TAG "cdt_key"

#define CDT_KEY_POLL_MS_DEFAULT 5u

static esp_timer_handle_t s_timer;
static cdt_key_config_t s_cfg;
static cdt_key_pure_sm_t s_sm_key;  /* KEY = GPIO18 */
static cdt_key_pure_sm_t s_sm_boot; /* BOOT = GPIO0 */
static bool s_started;

/* GPIO 配置：输入 + 内部上拉 + 关中断（vendor button_bsp.c:64-72 同款） */
static esp_err_t key_gpio_init(void)
{
    gpio_config_t conf;
    memset(&conf, 0, sizeof(conf));
    conf.intr_type = GPIO_INTR_DISABLE;
    conf.mode = GPIO_MODE_INPUT;
    conf.pull_down_en = GPIO_PULLDOWN_DISABLE;
    conf.pull_up_en = GPIO_PULLUP_ENABLE;
    conf.pin_bit_mask = (1ULL << CDT_KEY_GPIO_KEY) | (1ULL << CDT_KEY_GPIO_BOOT);
    return gpio_config(&conf);
}

static void feed_and_dispatch(cdt_key_pure_sm_t *sm, gpio_num_t pin, bool is_key)
{
    cdt_key_pure_event_t e;
    cdt_key_event_t out;

    e = cdt_key_pure_feed(sm, (int)gpio_get_level(pin),
                          (int64_t)(esp_timer_get_time() / 1000));
    if (e == CDT_KEY_PURE_EVT_NONE) {
        return;
    }
    out = CDT_KEY_EVENT_NONE;
    if (is_key) {
        out = (e == CDT_KEY_PURE_EVT_SHORT) ? CDT_KEY_EVENT_KEY_SHORT
                                            : CDT_KEY_EVENT_KEY_LONG;
    } else {
        out = (e == CDT_KEY_PURE_EVT_SHORT) ? CDT_KEY_EVENT_BOOT_SHORT
                                            : CDT_KEY_EVENT_BOOT_LONG;
    }
    if (s_cfg.on_event != NULL) {
        s_cfg.on_event(s_cfg.user, out);
    }
}

static void key_poll_cb(void *arg)
{
    (void)arg;
    feed_and_dispatch(&s_sm_key, (gpio_num_t)CDT_KEY_GPIO_KEY, true);
    feed_and_dispatch(&s_sm_boot, (gpio_num_t)CDT_KEY_GPIO_BOOT, false);
}

const char *cdt_key_event_name(cdt_key_event_t ev)
{
    switch (ev) {
    case CDT_KEY_EVENT_KEY_SHORT:
        return "KEY_SHORT";
    case CDT_KEY_EVENT_KEY_LONG:
        return "KEY_LONG";
    case CDT_KEY_EVENT_BOOT_SHORT:
        return "BOOT_SHORT";
    case CDT_KEY_EVENT_BOOT_LONG:
        return "BOOT_LONG";
    case CDT_KEY_EVENT_NONE:
    default:
        return "NONE";
    }
}

esp_err_t cdt_key_start(const cdt_key_config_t *cfg)
{
    esp_err_t rc;
    esp_timer_create_args_t targs;
    uint32_t period_ms;

    if (cfg == NULL || cfg->on_event == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_started) {
        return ESP_ERR_INVALID_STATE;
    }

    s_cfg = *cfg;
    period_ms = (s_cfg.poll_period_ms == 0u) ? CDT_KEY_POLL_MS_DEFAULT
                                             : s_cfg.poll_period_ms;

    rc = key_gpio_init();
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "GPIO 配置失败 rc=%s", esp_err_to_name(rc));
        return rc;
    }

    /* 两键均为低有效（HARDWARE §1.2 confirmed）：active_level = 0；
     * 去抖/长按参数 0 → 纯层取 §6 初值（30/800）。 */
    cdt_key_pure_init(&s_sm_key, s_cfg.debounce_ms, s_cfg.long_press_ms, 0);
    cdt_key_pure_init(&s_sm_boot, s_cfg.debounce_ms, s_cfg.long_press_ms, 0);

    memset(&targs, 0, sizeof(targs));
    targs.callback = &key_poll_cb;
    targs.arg = NULL;
    targs.name = "cdt_key_poll";
    rc = esp_timer_create(&targs, &s_timer);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "轮询定时器创建失败 rc=%s", esp_err_to_name(rc));
        return rc;
    }
    rc = esp_timer_start_periodic(s_timer, (uint64_t)period_ms * 1000u);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "轮询定时器启动失败 rc=%s", esp_err_to_name(rc));
        (void)esp_timer_delete(s_timer);
        s_timer = NULL;
        return rc;
    }

    s_started = true;
    ESP_LOGI(TAG, "KEY=GPIO%u BOOT=GPIO%u 轮询 %ums 启动（去抖/长按判定在纯层）",
             (unsigned)CDT_KEY_GPIO_KEY, (unsigned)CDT_KEY_GPIO_BOOT,
             (unsigned)period_ms);
    return ESP_OK;
}

esp_err_t cdt_key_stop(void)
{
    esp_err_t rc;
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    rc = esp_timer_stop(s_timer);
    if (rc == ESP_OK) {
        (void)esp_timer_delete(s_timer);
        s_timer = NULL;
    }
    s_started = false;
    return rc;
}
