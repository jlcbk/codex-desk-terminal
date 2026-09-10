# P4.3 — LVGL + shared/ui 完整页面（ESP-IDF v5.5.5，target esp32s3）
# 版本真源：docs/VERSIONS.md（IDF v5.5.5，tag ff1bac0 / commit b774170f）
# LVGL 9.3.0 真源：vendor/lvgl（锁 commit c033a98，经 components/cdt_lvgl 接入）
cmake_minimum_required(VERSION 3.16)

# P4.3 构建面（临时限定，A0 合并时复核）：本任务镜像只含 UI 链路三组件。
# components/transport（A4 的 P3.3/P3.4 固件侧骨架，2026-09-11 入树时仅
# CMakeLists.txt、wss_client.c/ble_peripheral.c 缺源）会使全工程 cmake 失败，
# 且不属于 P4.3 镜像范围。A4 补齐源码 / A0 统一合并后删除本行恢复全量构建。
set(COMPONENTS main cdt_lvgl display_st7305)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(cdt_p43_shared_ui)
