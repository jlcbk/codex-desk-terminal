# P4.4+P4.5 board IO 骨架 — 独立构建目标（仅编译级验证，不烧录）
#
# 与 P4.3 在途的 firmware/（project cdt_p42_display）完全隔离：
#   - 本工程在 firmware/examples/board_io_skeleton/ 自带独立 build/；
#   - EXTRA_COMPONENT_DIRS 挂入 firmware/components/，但用 set(COMPONENTS ...)
#     只构建 battery+input（+main 及其 REQUIRES 依赖链）——不编译
#     display_st7305/transport/cdt_lvgl，不引入 P4.3 域与 LVGL 依赖
#     （同 transport_skeleton 的隔离意图，见其 CMakeLists.txt 注释）；
#   - shared/power 经 battery 组件相对路径引用编译，不复制源码（§10 惯例）。
cmake_minimum_required(VERSION 3.16)
list(APPEND EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
set(COMPONENTS main battery input)
project(cdt_board_io_skeleton)
