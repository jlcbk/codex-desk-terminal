# 整机产品形态（整机集成 v1，A3+A4 合并；ESP-IDF v5.5.5，target esp32s3）
# 版本真源：docs/VERSIONS.md（IDF v5.5.5，tag ff1bac0 / commit b774170f）
# LVGL 9.3.0 真源：vendor/lvgl（锁 commit c033a98，经 components/cdt_lvgl 接入）
cmake_minimum_required(VERSION 3.16)

# P4.3 期间的临时组件限定（set(COMPONENTS main cdt_lvgl display_st7305)）已按
# 该行注释的约定删除：A4 骨架源码（wss_client.c/ble_peripheral.c）2026-09-11
# 已补齐入树，恢复全量构建（battery/input/power/transport 全部参与）。
# BLE（NimBLE）编译进固件但本集成不启动（同一时刻仅一个活动 transport，
# transport.md §1；本任务用 WSS）。

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(cdt_device)
