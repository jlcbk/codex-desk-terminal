# 整机产品形态主程序（整机集成 v1，A3+A4 合并）。
# shared 源码以相对路径引用编译（计划 §10：不复制源码，照 simulator/CMakeLists 做法）：
#   shared/state:      cdt_json.c（有界解析器）、cdt_parser.c（AppState）
#   shared/display:    cdt_frame.c（400x300 1bpp 逻辑帧原语）
#   shared/presenter:  cdt_presenter.c（AppState+Runtime → ViewModel）
#   shared/ui:         cdt_ui.c + 五页 + 字体 + 导航（LVGL 页面层）
# 注：shared/transport/cdt_crc32.c 不再进 main（P4.3 旧例已改）——组件
#     components/transport 的 archive 已编译同一文件（wss/ble 引用），重复
#     编入会产生重复符号；main.c 按冻结签名本地声明 cdt_crc32 原型（P4.2 先例）。
idf_component_register(
    SRCS "main.c"
         "app_inbox.c"
         "dev_net.c"
         "lvgl_port.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/state/cdt_json.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/state/cdt_parser.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/state/cdt_store.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/display/cdt_frame.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/presenter/cdt_presenter.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_font_unifont16.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_now.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_internal.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_agents.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_plan.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_usage.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_ui_lowbat.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui/cdt_nav.c"
    INCLUDE_DIRS "."
    REQUIRES display_st7305 cdt_lvgl battery input power transport
             esp_wifi nvs_flash esp_event esp_netif esp_timer esp_driver_gpio esp_lcd)

# include 面：
#   shared/display（cdt_frame.h，display_st7305 已 PUBLIC 传递）
#   shared/state + shared/presenter（cdt_parser/codex_state/cdt_view/cdt_runtime）
#   shared/ui（cdt_ui/cdt_nav 等）
#   shared/power（cdt_power.h，经 battery/power 组件 PUBLIC 传递）
#
# 头文件防撞（整机集成新增）：shared/display/cdt_frame.h 与 shared/transport/
# cdt_frame.h 同名且 include guard 相同（CDT_FRAME_H）。全量构建引入 transport
# 组件后，两个 include 目录同时进入 main 的搜索路径——用 BEFORE 把 shared/display
# 钉在搜索序最前，保证显示帧头必胜出（transport 的 cdt_frame.h 仅经
# cdt_wss_client.h 间接可用，它本身不 include cdt_frame.h，无冲突）。
target_include_directories(${COMPONENT_LIB} PRIVATE BEFORE
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/display")
target_include_directories(${COMPONENT_LIB} PRIVATE
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/state"
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/presenter"
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/ui")

# 离线演示构建：idf.py -C firmware build -DCDT_DEVNET_OFFLINE=1
# （不编入任何凭据；运行时链路如实显示 DISCONNECTED。红线：默认凭据绝不编译。）
set(CDT_DEVNET_OFFLINE "" CACHE STRING "离线演示构建（1=无网络凭据，DISCONNECTED 页）")
if(CDT_DEVNET_OFFLINE)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CDT_DEVNET_OFFLINE=1)
endif()

# shared/presenter/cdt_presenter.c 在 xtensa GCC 下触发两类格式告警（Apple clang
# 无此告警，模拟器构建不受影响），固件侧仅降级为告警不改动 shared 源码——
# ①L168/L169 %u 对 uint32_t（xtensa 上为 unsigned long）；②L522/L525
# -Wformat-truncation（实际安全：trunc_cols 已按列预算截断）。
# 待 A0 裁决 shared 层最小 patch（建议改用 PRIu32/显式 unsigned 转换）后撤除。
set_source_files_properties(
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/presenter/cdt_presenter.c"
    PROPERTIES COMPILE_OPTIONS "-Wno-error=format;-Wno-error=format-truncation")
