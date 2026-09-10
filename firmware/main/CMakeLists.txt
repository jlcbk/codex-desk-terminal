# P4.3 主程序 — LVGL + shared/ui 完整页面上真屏（A2+A3）。
# shared 源码以相对路径引用编译（计划 §10：不复制源码，照 simulator/CMakeLists 做法）：
#   shared/state:      cdt_json.c（有界解析器）、cdt_parser.c（AppState）
#   shared/display:    cdt_frame.c（400x300 1bpp 逻辑帧原语）
#   shared/presenter:  cdt_presenter.c（AppState+Runtime → ViewModel）
#   shared/ui:         cdt_ui.c + 五页 + 字体 + 导航（LVGL 页面层）
#   shared/transport:  cdt_crc32.c（冻结 CRC32，与 zlib.crc32 一致）
idf_component_register(
    SRCS "main.c"
         "lvgl_port.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/state/cdt_json.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/state/cdt_parser.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/display/cdt_frame.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/presenter/cdt_presenter.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_font_noto_sc.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_now.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_internal.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_agents.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_plan.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_usage.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_lowbat.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_nav.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/transport/cdt_crc32.c"
    INCLUDE_DIRS "."
    REQUIRES display_st7305 cdt_lvgl esp_lcd esp_driver_gpio)

# include 面：
#   shared/display（cdt_frame.h，display_st7305 已 PUBLIC 传递）
#   shared/state + shared/presenter（cdt_parser/codex_state/cdt_view/cdt_runtime）
#   shared/ui（cdt_ui/cdt_nav 等）
# 不挂 shared/transport：其 cdt_frame.h 与显示帧头同名且 include guard 相同
# （CDT_FRAME_H），无法同 TU 共存；crc32 原型在 main.c 按冻结签名声明（P4.2 先例）。
target_include_directories(${COMPONENT_LIB} PRIVATE
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/state"
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/presenter"
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui")

# shared/presenter/cdt_presenter.c 在 xtensa GCC 下触发两类格式告警（Apple clang
# 无此告警，模拟器构建不受影响），固件侧仅降级为告警不改动 shared 源码——
# ①L168/L169 %u 对 uint32_t（xtensa 上为 unsigned long）；②L522/L525
# -Wformat-truncation（实际安全：trunc_cols 已按列预算截断）。
# 待 A0 裁决 shared 层最小 patch（建议改用 PRIu32/显式 unsigned 转换）后撤除。
set_source_files_properties(
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/presenter/cdt_presenter.c"
    PROPERTIES COMPILE_OPTIONS "-Wno-error=format;-Wno-error=format-truncation")
