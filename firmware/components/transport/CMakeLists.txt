# Transport 骨架组件（P3.3/P3.4 固件侧骨架，A4；纯编译级验证——W5/B0-B5 真机项未验证）
#
# shared/transport 引擎源码以相对路径引用编译（项目惯例 §10：不复制源码）：
#   cdt_crc32.c      — 冻结 CRC32 + cdt_frame_encode/decode/validate（P3.4 交付）
#   cdt_reassembler.c— BLE DATA 接收侧重组引擎（P3.4 交付）
#   cdt_fragmenter.c — 发送侧分片器 + ACK/NACK 决策辅助（P3.4 交付）
idf_component_register(
    SRCS "wss_client.c"
         "ble_peripheral.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../../shared/transport/cdt_crc32.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../../shared/transport/cdt_reassembler.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../../shared/transport/cdt_fragmenter.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../../shared/transport"
    REQUIRES esp_wifi nvs_flash esp_event esp_timer
    PRIV_REQUIRES bt esp-tls esp_websocket_client mbedtls)

# 公开 shared/transport 头（cdt_ble_peripheral.h 引用 cdt_fragmenter.h 的
# cdt_apply_result_t 与 ACK/NACK 构造接口）。
# 注意：shared/display/cdt_frame.h 与 shared/transport/cdt_frame.h 同名且
# include guard 相同（CDT_FRAME_H，见 display_st7305/CMakeLists.txt 备注）——
# 两者不得进同一编译单元；本工程不含 display 组件，无冲突。
target_include_directories(${COMPONENT_LIB} PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/../../../shared/transport")
