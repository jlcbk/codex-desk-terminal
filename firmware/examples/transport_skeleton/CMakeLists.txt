# P3.3/P3.4 固件传输骨架 — 独立构建目标（仅编译级验证，不烧录）
#
# 与 P4.3 正在进行的 firmware/（project cdt_p42_display）完全隔离：
#   - 本工程在 firmware/examples/transport_skeleton/ 自带独立 build/ 目录；
#   - 通过 EXTRA_COMPONENT_DIRS 挂入 firmware/components/（transport 组件）；
#   - 不引用 display_st7305，不触碰 firmware/main/（并避开 shared/display 与
#     shared/transport 的 cdt_frame.h 同名同 guard 冲突——本工程只编译后者）。
cmake_minimum_required(VERSION 3.16)
list(APPEND EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(cdt_transport_skeleton)
