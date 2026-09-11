# P5.3 电源执行器骨架 — 独立构建目标（仅编译级验证，不烧录、不上板）
#
# 隔离策略（同 transport_skeleton / board_io_skeleton）：
#   - 本工程在 firmware/examples/power_skeleton/ 自带独立 build/ 与 sdkconfig；
#   - EXTRA_COMPONENT_DIRS 只挂 components/power 一个组件目录——不拉入
#     transport/display_st7305/battery 等其他组件，避免跨任务在途状态影响
#     本验证（board_io_skeleton 用 set(COMPONENTS ...) 裁剪，本工程用目录级
#     隔离达成同一目的且互不影响）；
#   - shared/power 经 power 组件相对路径引用编译，不复制源码（§10 惯例）。
cmake_minimum_required(VERSION 3.16)
list(APPEND EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../components/power")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(cdt_power_skeleton)
