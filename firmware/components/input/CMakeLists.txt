# P4.5 按键组件（固件侧，A3；纯编译级验证——真机按压实测未做）
#
# 去抖/长短按纯状态机（cdt_key_pure.{h,c}）零平台头，主机端单测
# tests/firmware/test_key_pure.c 由 scripts/build_firmware_io_tests.sh 编译运行。
idf_component_register(
    SRCS "cdt_key.c"
         "cdt_key_pure.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_gpio esp_timer)
