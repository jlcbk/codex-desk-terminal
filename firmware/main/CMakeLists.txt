# P4.2 主程序。shared 源码以相对路径引用编译（计划 §10：不复制源码）：
#   shared/display/cdt_frame.c   — 400x300 1bpp 逻辑帧原语
#   shared/transport/cdt_crc32.c — 冻结 CRC32（与 zlib.crc32 一致）
idf_component_register(
    SRCS "main.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/display/cdt_frame.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../shared/transport/cdt_crc32.c"
    INCLUDE_DIRS "."
    REQUIRES display_st7305 esp_lcd esp_driver_gpio)

# 只挂 shared/display 的 include（cdt_frame.h 显示帧头）。
# 不挂 shared/transport：其 cdt_frame.h 与显示帧头同名且 include guard 相同
# （CDT_FRAME_H），无法同 TU 共存；crc32 原型在 main.c 按冻结签名声明。
target_include_directories(${COMPONENT_LIB} PRIVATE
    "${CMAKE_CURRENT_LIST_DIR}/../../shared/display")
