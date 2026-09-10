# P4.4 电池组件（固件侧，A3；纯编译级验证——真机表计校准/分压核验未做）
#
# shared/power 引擎源码以相对路径引用编译（项目惯例 §10：不复制源码）：
#   cdt_power.c — P5.1 冻结的纯 Power FSM；同时也是本组件唯一校准公式真源
#   （cdt_power_effective_mv，"与 FSM 参数同源"由构造保证，不靠注释约定）。
idf_component_register(
    SRCS "cdt_battery.c"
         "cdt_battery_pure.c"
         "${CMAKE_CURRENT_LIST_DIR}/../../../shared/power/cdt_power.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_adc)

# cdt_power.h 公开（cdt_battery.h/cdt_battery_pure.h 引用 cdt_power_sample_t
# 与 cdt_power_params_t——FSM 同源形状）。其内部相对 include
# "../presenter/cdt_runtime.h"→"../state/codex_state.h" 按文件相对路径自行
# 解析，均为纯 C99 头（P1.4/P5.1 已过主机端门禁），无需额外 include 路径。
target_include_directories(${COMPONENT_LIB} PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/../../../shared/power")
