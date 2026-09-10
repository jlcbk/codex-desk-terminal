# cdt_lvgl — LVGL 9.3.0 组件化接入（P4.3，A2+A3）。
#
# 源码真源：vendor/lvgl（锁 commit c033a98，scripts/vendor_lvgl.sh；与模拟器同
# 一份 vendor，不复制）。照 vendor/lvgl/env_support/cmake/os_desktop.cmake 的
# 做法 file(GLOB_RECURSE) 全量 src/*.c/*.S——被 lv_conf 关闭的可选功能（SDL/
# demo/example/ThorVG 等）在源文件内部由 #if 编译为空，不参与链接。
#
# 配置注入与桌面 CMake 同款：LV_KCONFIG_IGNORE（跳过 lv_conf_kconfig.h）+
# LV_CONF_PATH 指向本组件 lv_conf.h（固件版：LV_COLOR_DEPTH=1、关 SDL、
# LV_TICK_CUSTOM=esp_timer）。两个宏必须 PUBLIC——shared/ui 等引用方 include
# lvgl.h 时也要走同一份 lv_conf.h，保证与模拟器逐宏一致（帧哈希对齐前提）。
set(LVGL_ROOT_DIR "${CMAKE_CURRENT_LIST_DIR}/../../../vendor/lvgl")

if(NOT EXISTS "${LVGL_ROOT_DIR}/lv_version.h")
    message(FATAL_ERROR "vendor/lvgl 不存在或无效，请先运行: scripts/vendor_lvgl.sh")
endif()

file(GLOB_RECURSE LVGL_SOURCES
    "${LVGL_ROOT_DIR}/src/*.c"
    "${LVGL_ROOT_DIR}/src/*.S")

idf_component_register(
    SRCS ${LVGL_SOURCES}
    INCLUDE_DIRS "${LVGL_ROOT_DIR}"
    REQUIRES esp_timer)

target_compile_definitions(${COMPONENT_LIB} PUBLIC
    LV_KCONFIG_IGNORE
    LV_CONF_PATH="${CMAKE_CURRENT_LIST_DIR}/lv_conf.h")

# LVGL 源码自带告警面较大，不动全局告警等级（不加 -Werror）。
