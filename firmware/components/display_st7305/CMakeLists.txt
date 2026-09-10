# ST7305 驱动组件（P4.2）。shared/display 以相对路径引用（计划 §10：不复制源码）。
idf_component_register(
    SRCS "st7305.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_lcd esp_driver_gpio)

# 公开 shared/display/include（cdt_frame.h：400x300 1bpp 逻辑帧）。
# 注意：shared/transport/cdt_frame.h 与之同名且 include guard 相同（CDT_FRAME_H），
# 本组件只暴露 display 一侧，避免双头冲突；CRC 声明由调用方按冻结签名自取。
target_include_directories(${COMPONENT_LIB} PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/../../../shared/display")
