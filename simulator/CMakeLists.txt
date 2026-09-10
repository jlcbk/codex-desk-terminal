# codex-display-sim 模拟器工程（P1.3 / A2）
#
# 独立 CMake 工程：vendor/lvgl（锁 commit c033a98）+ third_party/sdl2-install（SDL2 release-2.30.12）。
# 先运行 scripts/vendor_lvgl.sh 和 scripts/build_sdl2.sh，或直接运行 scripts/build_simulator.sh。
cmake_minimum_required(VERSION 3.16)
project(codex-display-sim LANGUAGES C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)

get_filename_component(SIM_PROJ_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
set(LVGL_DIR "${SIM_PROJ_ROOT}/vendor/lvgl")
set(SDL2_PREFIX "${SIM_PROJ_ROOT}/third_party/sdl2-install")

if(NOT EXISTS "${LVGL_DIR}/lv_version.h")
    message(FATAL_ERROR "vendor/lvgl 不存在或无效，请先运行: scripts/vendor_lvgl.sh")
endif()
if(NOT EXISTS "${SDL2_PREFIX}/lib/cmake/SDL2/SDL2Config.cmake")
    message(FATAL_ERROR "SDL2 未安装，请先运行: scripts/build_sdl2.sh")
endif()

# --- LVGL 集成（vendor/lvgl/env_support/cmake/os_desktop.cmake） ---
# LV_BUILD_CONF_PATH：显式指定本工程 lv_conf.h（内部会定义 LV_CONF_PATH="<绝对路径>"）
set(LV_BUILD_CONF_PATH "${CMAKE_CURRENT_SOURCE_DIR}/lv_conf.h" CACHE PATH
    "codex-display-sim lv_conf.h" FORCE)
# 缩小构建面：不构建 demos/examples/ThorVG（C++ 库），LVGL 本体只编译所需部分
set(CONFIG_LV_BUILD_DEMOS OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_USE_THORVG_INTERNAL OFF CACHE BOOL "" FORCE)

add_subdirectory("${LVGL_DIR}" "${CMAKE_BINARY_DIR}/lvgl")

# --- SDL2（安装树内建 CMake config；静态库 libSDL2.a，无运行期 dylib 路径问题） ---
list(APPEND CMAKE_PREFIX_PATH "${SDL2_PREFIX}")
find_package(SDL2 CONFIG REQUIRED)

# LVGL 的 SDL 驱动源码（lv_sdl_window.c 等）需要能找到 <SDL2/SDL.h>
target_include_directories(lvgl PRIVATE "${SDL2_PREFIX}/include")

add_executable(codex-display-sim
    main.c
    # shared/ 源码相对路径引用（不复制）：state 解析、公共单色帧、presenter、共享 UI、
    # 导航纯逻辑（P2.2）、Power FSM（P2.3 证据帧经真实 FSM 驱动低压页）
    "${SIM_PROJ_ROOT}/shared/state/cdt_json.c"
    "${SIM_PROJ_ROOT}/shared/state/cdt_parser.c"
    "${SIM_PROJ_ROOT}/shared/display/cdt_frame.c"
    "${SIM_PROJ_ROOT}/shared/presenter/cdt_presenter.c"
    "${SIM_PROJ_ROOT}/shared/power/cdt_power.c"
    "${SIM_PROJ_ROOT}/shared/ui/cdt_ui.c"
    "${SIM_PROJ_ROOT}/shared/ui/cdt_font_noto_sc.c"
    "${SIM_PROJ_ROOT}/shared/ui/cdt_ui_now.c"
    "${SIM_PROJ_ROOT}/shared/ui/cdt_ui_internal.c"
    "${SIM_PROJ_ROOT}/shared/ui/cdt_ui_agents.c"
    "${SIM_PROJ_ROOT}/shared/ui/cdt_ui_plan.c"
    "${SIM_PROJ_ROOT}/shared/ui/cdt_ui_usage.c"
    "${SIM_PROJ_ROOT}/shared/ui/cdt_ui_lowbat.c"
    "${SIM_PROJ_ROOT}/shared/ui/cdt_nav.c"
)
target_include_directories(codex-display-sim PRIVATE
    "${SIM_PROJ_ROOT}/shared/state"
    "${SIM_PROJ_ROOT}/shared/display"
    "${SIM_PROJ_ROOT}/shared/presenter"
    "${SIM_PROJ_ROOT}/shared/power"
    "${SIM_PROJ_ROOT}/shared/ui"
)
target_link_libraries(codex-display-sim PRIVATE lvgl SDL2::SDL2-static)
target_compile_options(codex-display-sim PRIVATE -Wall -Wextra)
