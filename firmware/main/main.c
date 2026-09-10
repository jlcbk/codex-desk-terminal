/*
 * main.c — P4.3（A2+A3）LVGL + shared/ui 完整页面上真屏（板 1301）。
 *
 * 验收目标（docs/DEVELOPMENT_PLAN.md P4.3 行）：与单色 golden 内容一致；记录
 * 照片及 frame hash。设备侧形式：**同输入帧 CRC 与模拟器完全一致**——
 *   固件：内嵌场景 AppState（p43_scenes.h，与 golden 同源 fixture）+ 合成
 *         DeviceRuntime（3900mV/connected/last_rx=0，与模拟器 synth_runtime 同值）
 *         → cdt_present → cdt_ui_apply_nav → 整屏刷新（同步 flush 落面板）
 *         → 对 cdt 逻辑帧（15000B，1=黑）算 cdt_crc32 并串口打印。
 *   模拟器：同一 JSON 文本经 --state 路径 + --fixed-clock 渲染 --capture-frame，
 *         Mac 侧对 BMP 重打包 15000B 后算 zlib.crc32（firmware/tools/p43_align.py）。
 * 两端同 vendor/lvgl（commit c033a98）、同渲染面 lv_conf（见 components/cdt_lvgl/lv_conf.h）。
 *
 * 页面每 6 秒自动切换循环；KEY（长按静音/导航）归 P4.5，本任务不实现。
 * 不含网络/ADC/按键；电池为合成注入（生产构建禁远端电池注入的红线不变，
 * 此处 3900mV 是演示 Runtime，与模拟器对齐路径一致）。
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

#include "cdt_nav.h"
#include "cdt_parser.h"
#include "cdt_presenter.h"
#include "cdt_ui.h"
#include "cdt_view.h"
#include "lvgl.h"
#include "lvgl_port.h"
#include "p43_scenes.h"
#include "st7305.h"

/*
 * CRC32：shared/transport/cdt_frame.h:91 冻结签名
 *   uint32_t cdt_crc32(const uint8_t *data, size_t len);
 * 该头与 shared/display/cdt_frame.h 同名且 include guard 相同（CDT_FRAME_H），
 * 同一翻译单元无法同时包含，故此处按冻结签名本地声明；实现链接
 * shared/transport/cdt_crc32.c（见 main/CMakeLists.txt，P4.2 先例）。
 */
uint32_t cdt_crc32(const uint8_t *data, size_t len);

#define TAG "p43"

#define SCENE_PERIOD_MS 6000
#define UI_TASK_STACK_BYTES (24 * 1024)
#define UI_TASK_PERIOD_MS 10

/* 场景表：now_ms 为 cdt_present 的固定单调时钟（对应模拟器 --fixed-clock）。
 * NOW 场景取 1ms（时长显示 00:00，同 golden f001 的 0ms 显示）；USAGE 场景取
 * 3000ms（与 golden f004 的 at_ms=3000 对齐，RST 倒计时同值）。 */
typedef struct {
    const char *name;      /* 串口场景名（=对齐脚本键） */
    const char *json;      /* AppState JSON（p43_scenes.h 生成） */
    cdt_page_t page;       /* DeviceRuntime.selected_page（KEY 未接，由场景指定） */
    uint32_t now_ms;       /* cdt_present 固定时钟 */
    const char *golden;    /* 视觉对照 golden 帧名（tests/golden，非像素目标） */
} p43_scene_t;

static const p43_scene_t k_scenes[] = {
    { "F01_idle",       P43_SCENE_IDLE_JSON,       CDT_PAGE_NOW,   1,
      "S01_idle__f001_idle.png" },
    { "F03_working",    P43_SCENE_WORKING_JSON,    CDT_PAGE_NOW,   1,
      "S03_working__f001_working.png" },
    { "F05_needs_you",  P43_SCENE_NEEDS_YOU_JSON,  CDT_PAGE_NOW,   1,
      "S05_needs_you__f001_needs_you.png" },
    { "F19_usage_0",    P43_SCENE_USAGE0_JSON,     CDT_PAGE_USAGE, 3000,
      "S19_usage_0__f004_usage_page.png" },
    { "F20_usage_100",  P43_SCENE_USAGE100_JSON,   CDT_PAGE_USAGE, 3000,
      "S20_usage_100__f004_usage_page.png" },
    { "F06_valid_full", P43_SCENE_VALID_FULL_JSON, CDT_PAGE_NOW,   1,
      "(no golden; NOW needs_you+plan, CJK)" },
};

#define SCENE_COUNT (sizeof k_scenes / sizeof k_scenes[0])

/* 大对象静态化：不占 ui 任务栈（app_state/view 均为 KB 级） */
static cdt_app_state_t s_state;
static cdt_runtime_t s_runtime;
static cdt_view_t s_view;
static cdt_nav_t s_nav;

/* ---- 合成 DeviceRuntime：与模拟器 synth_runtime（--state 路径）同值 ----
 * 电池 3900mV（usable 50%，§7.1 线性估算）、link connected、last_rx=0、
 * 充电/外接 unknown（不根据高电压猜充电）、transport "mock"、power ACTIVE。 */
static void synth_runtime(cdt_page_t page)
{
    memset(&s_runtime, 0, sizeof(s_runtime));
    s_runtime.battery_valid = true;
    s_runtime.battery_mv = 3900;
    s_runtime.usable_percent = (uint8_t)((3900 - 3600) * 100 / 600);
    s_runtime.charging = CDT_PRESENCE_UNKNOWN;
    s_runtime.external_power = CDT_PRESENCE_UNKNOWN;
    s_runtime.power_state = CDT_POWER_ACTIVE;
    snprintf(s_runtime.transport, sizeof(s_runtime.transport), "mock");
    s_runtime.link_state = CDT_LINK_CONNECTED;
    s_runtime.last_rx_monotonic_ms = 0;
    s_runtime.selected_page = page;
}

/* ---- 应用一个场景：解析→present→apply→整屏刷新（同步 flush）→CRC 打印 ---- */
static void apply_scene(const p43_scene_t *sc)
{
    cdt_parse_result_t r =
        cdt_state_parse(sc->json, strlen(sc->json), &s_state);
    if (r != CDT_PARSE_OK) {
        ESP_LOGE(TAG, "scene=%s ERROR: AppState rejected by parser (code %d)",
                 sc->name, (int)r);
        return;
    }

    synth_runtime(sc->page);
    cdt_nav_init(&s_nav, sc->page);
    /* 与模拟器 --state 路径同序：present → apply_nav（clamp 在 apply 内） */
    cdt_present(&s_state, &s_runtime, sc->now_ms, &s_view);
    cdt_ui_apply_nav(&s_view, &s_nav);

    cdt_lvgl_port_refresh(); /* 整屏重绘 + 同步 flush 落面板 */
    const cdt_frame_t *f = cdt_lvgl_port_frame();
    uint32_t crc = cdt_crc32(f->px, CDT_FRAME_BYTES);

    ESP_LOGI(TAG, "scene=%s crc32=0x%08lx page=%d now_ms=%lu status=\"%s\" "
                  "elapsed=\"%s\" voltage=\"%s\" bytes=%d golden=%s",
             sc->name, (unsigned long)crc, (int)s_view.page,
             (unsigned long)sc->now_ms, s_view.status_label,
             s_view.elapsed_text, s_view.voltage_text, (int)CDT_FRAME_BYTES,
             sc->golden);
}

static void ui_task(void *arg)
{
    (void)arg;
    size_t i = 0;

    ESP_LOGI(TAG, "P4.3 shared-ui on ST7305: %u scenes, %d ms period, "
                  "logic 400x300@1bpp (1=black), LVGL %d.%d.%d",
             (unsigned)SCENE_COUNT, SCENE_PERIOD_MS,
             lv_version_major(), lv_version_minor(), lv_version_patch());

    st7305_config_t cfg = st7305_default_config();
    esp_err_t err = st7305_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "st7305_init failed: %s — UI loop aborted",
                 esp_err_to_name(err));
        vTaskDelete(NULL);
        return;
    }

    err = cdt_lvgl_port_display_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "lvgl display init failed: %s", esp_err_to_name(err));
        vTaskDelete(NULL);
        return;
    }

    cdt_ui_init(); /* 五页构建（字体先于页面），初始 NOW */
    apply_scene(&k_scenes[0]);

    TickType_t last_switch = xTaskGetTickCount();
    for (;;) {
        lv_timer_handler(); /* LVGL 时基来自 LV_TICK_CUSTOM(esp_timer)，无需 tick 任务 */
        if ((xTaskGetTickCount() - last_switch) >=
            pdMS_TO_TICKS(SCENE_PERIOD_MS)) {
            last_switch = xTaskGetTickCount();
            i = (i + 1) % SCENE_COUNT;
            apply_scene(&k_scenes[i]);
        }
        vTaskDelay(pdMS_TO_TICKS(UI_TASK_PERIOD_MS));
    }
}

void app_main(void)
{
    BaseType_t ok = xTaskCreate(ui_task, "p43_ui", UI_TASK_STACK_BYTES / sizeof(StackType_t),
                                NULL, tskIDLE_PRIORITY + 5, NULL);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "create ui task failed");
    }
}
