/*
 * cdt_battery.c — GPIO4 电池 ADC oneshot 驱动（P4.4 固件侧，A3；仅编译级验证）
 *
 * 配置时序照官方 03_ADC_Test 的 adc_bsp（HARDWARE §2 逐行审计）：
 *   adc_cali_create_scheme_curve_fitting(ADC_UNIT_1, 12dB, 12bit)
 *   → adc_oneshot_new_unit(ADC_UNIT_1) → adc_oneshot_config_channel(CH3)；
 * 读取：adc_oneshot_read → adc_cali_raw_to_voltage → ×3 分压还原。
 *
 * 未验证（诚实标注）：真机表计对照校准、高阻分压稳定时间、连续采样噪声
 * ——全部留到 P4.4 真机任务；本文件不包含任何校准测量结论，参数默认恒等。
 */
#include <inttypes.h>
#include <string.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

#include "cdt_battery.h"

#define TAG "cdt_battery"

/* 单实例（板上仅 BAT_ADC 一路模拟输入，见 cdt_battery.h） */
static adc_oneshot_unit_handle_t s_adc1;
static adc_cali_handle_t s_cali;
static cdt_battery_config_t s_cfg;
static bool s_inited;
static esp_err_t s_last_err;

esp_err_t cdt_battery_init(const cdt_battery_config_t *cfg)
{
    esp_err_t rc;
    adc_oneshot_unit_init_cfg_t unit_cfg;
    adc_oneshot_chan_cfg_t chan_cfg;
    adc_cali_curve_fitting_config_t cali_cfg;

    if (cfg == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    /* 批量校验：0→默认 9；超上限拒绝（纯层同规则） */
    if (cfg->batch_n > CDT_BATTERY_BATCH_N_MAX) {
        return ESP_ERR_INVALID_ARG;
    }

    s_cfg = *cfg;
    if (s_cfg.batch_n == 0u) {
        s_cfg.batch_n = CDT_BATTERY_BATCH_N_DEFAULT; /* §7.1：每批 9 次 */
    }
    if (s_cfg.divider_permille == 0u) {
        s_cfg.divider_permille = CDT_BATTERY_DIVIDER_PERMILLE_DEFAULT; /* ×3 */
    }

    /* 曲线拟合校准：ADC1 + 12dB + 12bit（vendor adc_bsp.cpp:9-14 时序） */
    memset(&cali_cfg, 0, sizeof(cali_cfg));
    cali_cfg.unit_id = ADC_UNIT_1;
    cali_cfg.atten = ADC_ATTEN_DB_12;
    cali_cfg.bitwidth = ADC_BITWIDTH_12;
    rc = adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "curve fitting 校准创建失败 rc=%s", esp_err_to_name(rc));
        return rc;
    }

    /* oneshot 单元 + GPIO4=ADC1_CH3 通道（vendor adc_bsp.cpp:16-22 时序） */
    memset(&unit_cfg, 0, sizeof(unit_cfg));
    unit_cfg.unit_id = ADC_UNIT_1;
    unit_cfg.ulp_mode = ADC_ULP_MODE_DISABLE;
    rc = adc_oneshot_new_unit(&unit_cfg, &s_adc1);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "ADC1 oneshot 单元创建失败 rc=%s", esp_err_to_name(rc));
        (void)adc_cali_delete_scheme_curve_fitting(s_cali);
        s_cali = NULL;
        return rc;
    }
    memset(&chan_cfg, 0, sizeof(chan_cfg));
    chan_cfg.bitwidth = ADC_BITWIDTH_12;
    chan_cfg.atten = ADC_ATTEN_DB_12;
    rc = adc_oneshot_config_channel(s_adc1, ADC_CHANNEL_3, &chan_cfg);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "ADC1_CH3 通道配置失败 rc=%s", esp_err_to_name(rc));
        (void)adc_oneshot_del_unit(s_adc1);
        (void)adc_cali_delete_scheme_curve_fitting(s_cali);
        s_adc1 = NULL;
        s_cali = NULL;
        return rc;
    }

    s_last_err = ESP_OK;
    s_inited = true;
    ESP_LOGI(TAG, "ADC1_CH3 就绪：batch=%u divider=%" PRIu32 "‰ gain=%" PRId32 "ppm offset=%" PRId32 "mV",
             (unsigned)s_cfg.batch_n, s_cfg.divider_permille,
             s_cfg.params.cal_gain_ppm, s_cfg.params.cal_offset_mv);
    return ESP_OK;
}

esp_err_t cdt_battery_sample_batch(int64_t now_ms, cdt_power_sample_t *out)
{
    cdt_battery_reading_t reads[CDT_BATTERY_BATCH_N_MAX];
    uint8_t n;
    uint8_t ok_count;
    esp_err_t rc;
    esp_err_t first_err;
    int raw;
    int pin_mv;
    uint8_t i;

    if (!s_inited || out == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    n = s_cfg.batch_n;
    ok_count = 0u;
    first_err = ESP_OK;

    for (i = 0u; i < n; i++) {
        reads[i].ok = false;
        reads[i].battery_mv = 0u;
        raw = 0;
        rc = adc_oneshot_read(s_adc1, ADC_CHANNEL_3, &raw);
        if (rc != ESP_OK) {
            s_last_err = rc;
            if (first_err == ESP_OK) {
                first_err = rc;
            }
            continue;
        }
        pin_mv = 0;
        rc = adc_cali_raw_to_voltage(s_cali, raw, &pin_mv);
        if (rc != ESP_OK) {
            s_last_err = rc;
            if (first_err == ESP_OK) {
                first_err = rc;
            }
            continue;
        }
        reads[i].ok = true;
        reads[i].battery_mv =
            cdt_battery_pin_to_battery_mv((uint16_t)pin_mv, s_cfg.divider_permille);
        ok_count++;
    }

    /* 批次→样本决策（中位/整批 unknown/范围判定）全部在纯层，主机端可测 */
    if (!cdt_battery_batch_to_sample(reads, (size_t)n, &s_cfg.params, now_ms, out)) {
        return ESP_ERR_INVALID_STATE; /* 不可达：n 已在 init 校验 */
    }

    if (ok_count == 0u) {
        /* 全失败：把驱动级错误暴露给调度层（样本已置 unknown） */
        return first_err;
    }
    return ESP_OK; /* 部分失败时 out->valid=false（§7.1 保守整批 unknown）*/
}

esp_err_t cdt_battery_last_error(void)
{
    return s_last_err;
}

bool cdt_battery_should_sample_fast(uint16_t last_valid_mv)
{
    return cdt_battery_should_sample_fast_mv(last_valid_mv, CDT_BATTERY_FAST_BELOW_MV);
}
