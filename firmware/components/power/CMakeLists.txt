# P5.3 电源执行器组件（固件侧，A3；纯编译级验证——睡眠电流/唤醒行为零实测）
#
# shared/power 引擎源码以相对路径引用编译（项目惯例 §10：不复制源码）：
#   cdt_power.c — P5.1 冻结纯 Power FSM；本组件执行其动作位掩码
#   （INTERFACES §5："纯逻辑由 IDF adapter 执行关无线/睡眠等动作"）。
#
# 分层：cdt_power_exec_pure.c 零 esp 头（主机单测
# tests/firmware/test_power_exec_pure.c 经 scripts/build_power_tests.sh 编译）；
# cdt_power_exec.c 为 esp 头唯一聚集点（esp_sleep/gpio/RTC_DATA_ATTR）。
idf_component_register(
    SRCS "cdt_power_exec.c"
         "cdt_power_exec_pure.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../../shared/power/cdt_power.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_gpio esp_hw_support)

# cdt_power.h 公开（cdt_power_exec_pure.h 引用 cdt_power_action_t）；其内部
# 相对 include（../presenter/cdt_runtime.h → ../state/codex_state.h）按文件
# 相对路径自行解析，均为纯 C99 头（P1.4/P5.1 已过主机端门禁）。
target_include_directories(${COMPONENT_LIB} PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/../../../shared/power")
