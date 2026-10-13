cmake_minimum_required(VERSION 3.21)
project(cb_lvgl C)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

set(CB_RENDER "sw" CACHE STRING "Renderer: sw (LVGL software, default) or gpu (SDL X11 + EGL + NanoVG)")
set_property(CACHE CB_RENDER PROPERTY STRINGS gpu sw)
if(NOT CB_RENDER MATCHES "^(gpu|sw)$")
  message(FATAL_ERROR "CB_RENDER must be 'gpu' or 'sw', got '${CB_RENDER}'")
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(SDL2 REQUIRED IMPORTED_TARGET sdl2)
if(CB_RENDER STREQUAL "gpu")
  pkg_check_modules(EGL REQUIRED IMPORTED_TARGET egl)
  pkg_check_modules(GLES REQUIRED IMPORTED_TARGET glesv2)
endif()

# LVGL: pinned release, our lv_conf.h, nothing fetched or built that we do not use.
set(LV_BUILD_CONF_PATH ${CMAKE_CURRENT_SOURCE_DIR}/lv_conf.h CACHE PATH "" FORCE)
set(CONFIG_LV_BUILD_DEMOS OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_USE_THORVG OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_USE_THORVG_INTERNAL OFF CACHE BOOL "" FORCE)
set(LV_FETCH_DEPENDENCIES OFF CACHE BOOL "" FORCE)
set(LV_BUILD_INSTALL OFF CACHE BOOL "" FORCE)
include(FetchContent)
FetchContent_Declare(lvgl
  URL https://github.com/lvgl/lvgl/archive/refs/tags/v9.6.0.tar.gz
  URL_HASH SHA256=b20ee3acc1bba13c62d854f9ebd62e4c51e0b443b1e0225892e86442defa84df
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(lvgl)

if(CB_RENDER STREQUAL "gpu")
  target_compile_definitions(lvgl PUBLIC CB_RENDER_GPU=1)
  target_link_libraries(lvgl PUBLIC PkgConfig::SDL2 PkgConfig::EGL PkgConfig::GLES m)
else()
  target_compile_definitions(lvgl PUBLIC CB_RENDER_GPU=0)
  target_link_libraries(lvgl PUBLIC PkgConfig::SDL2 m)
endif()

set(CB_WARN -Wall -Wextra -Werror)
file(GLOB CB_FONTS ${CMAKE_CURRENT_SOURCE_DIR}/fonts/cb_*.c)
add_library(cb_core STATIC
  src/cb_theme.c src/cb_snapshot.c src/cb_model.c src/cb_sim.c src/cb_demo.c src/cb_demo_script.c
  src/cb_app.c src/cb_ambient.c
  src/nova/nova_fsm.c src/nova/orb.c src/nova/nova_bar.c src/nova/avatar_model.c src/nova/avatar.c src/nova/nova_panel.c
  src/screens/gate.c src/screens/dash.c
  src/widgets/sparkline.c src/widgets/ui_kit.c src/widgets/drive.c src/widgets/comfort.c src/widgets/connected.c
  src/map/mapbox_url.c src/map/map_gesture.c src/map/map_view.c src/screens/cockpit.c
  src/live/json.c src/live/ws.c src/live/live.c src/live/session_ctl.c src/live/nav_state.c src/map/map_puck.c src/map/route_geojson.c src/widgets/settings_sheet.c
  ${CB_FONTS})
target_include_directories(cb_core PUBLIC src fonts)
target_link_libraries(cb_core PUBLIC lvgl)
target_compile_options(cb_core PRIVATE ${CB_WARN})

# Map: MapLibre Native C API from the prebuilt package unpacked under .deps/maplibre/<preset>/. Optional: without
# it the app builds and the map page shows a placeholder. Its headers are C23, so only cb_map compiles against them.
if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64")
  set(CB_MLN_PRESET linux-gnu-arm64-egl)
else()
  set(CB_MLN_PRESET linux-gnu-x64-egl)
endif()
set(CB_MAPLIBRE_PREFIX "${CMAKE_CURRENT_SOURCE_DIR}/.deps/maplibre/${CB_MLN_PRESET}/maplibre-native-c-${CB_MLN_PRESET}"
    CACHE PATH "MapLibre Native C package root (include/, lib/)")
set(CB_HAVE_MAP OFF)
if(EXISTS "${CB_MAPLIBRE_PREFIX}/include/maplibre_native_c.h")
  set(CB_HAVE_MAP ON)
  find_package(Threads REQUIRED)
  if(NOT TARGET PkgConfig::EGL)
    pkg_check_modules(EGL REQUIRED IMPORTED_TARGET egl)
  endif()
  add_library(cb_map STATIC src/map/map_engine.c src/map/mapbox_url.c)
  set_target_properties(cb_map PROPERTIES C_STANDARD 23 C_EXTENSIONS ON)
  target_include_directories(cb_map PUBLIC src PRIVATE ${CB_MAPLIBRE_PREFIX}/include)
  target_link_libraries(cb_map PUBLIC
    ${CB_MAPLIBRE_PREFIX}/lib/libmaplibre-native-c.so ${CB_MAPLIBRE_PREFIX}/lib/libmln_ffi_platform.a
    dl PkgConfig::EGL Threads::Threads m)
  target_compile_options(cb_map PRIVATE ${CB_WARN})
  target_sources(cb_core PRIVATE src/map/map_worker.c)
  target_link_libraries(cb_core PUBLIC cb_map)
  target_compile_definitions(cb_core PUBLIC CB_HAVE_MAP=1)
  add_executable(map_probe tools/map_probe.c)
  target_link_libraries(map_probe PRIVATE cb_map)
  target_compile_options(map_probe PRIVATE ${CB_WARN})
  message(STATUS "Map: MapLibre Native at ${CB_MAPLIBRE_PREFIX}")
else()
  message(STATUS "Map: MapLibre Native package not found at ${CB_MAPLIBRE_PREFIX}; map page disabled")
endif()

add_executable(cb_lvgl src/main.c src/cb_display.c)
target_link_libraries(cb_lvgl PRIVATE cb_core)
target_compile_options(cb_lvgl PRIVATE ${CB_WARN})

enable_testing()
foreach(t nova_fsm followup model demo ambient mapbox_url map_gesture json ws live avatar nav_state map_puck route_geojson)
  add_executable(test_${t} tests/test_${t}.c)
  target_link_libraries(test_${t} PRIVATE cb_core)
  target_compile_options(test_${t} PRIVATE ${CB_WARN})
  add_test(NAME ${t} COMMAND test_${t})
endforeach()

# Headless end-to-end smoke test: runs the real app on an in-memory display, so it needs the sw renderer.
if(CB_RENDER STREQUAL "sw")
  add_executable(test_smoke tests/test_smoke.c)
  target_link_libraries(test_smoke PRIVATE cb_core)
  target_compile_options(test_smoke PRIVATE ${CB_WARN})
  add_test(NAME smoke COMMAND test_smoke)
  add_executable(test_smoke_v2 tests/test_smoke_v2.c)
  target_link_libraries(test_smoke_v2 PRIVATE cb_core)
  target_compile_options(test_smoke_v2 PRIVATE ${CB_WARN})
  add_test(NAME smoke_v2 COMMAND test_smoke_v2)
endif()
