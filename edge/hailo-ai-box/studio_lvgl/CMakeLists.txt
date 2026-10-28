cmake_minimum_required(VERSION 3.21)
project(aibox_studio C)
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# Renderer: gpu (SDL X11/Xwayland + EGL + NanoVG, as the nova cockpit) or sw (LVGL software on SDL's native Wayland).
set(STUDIO_RENDER "gpu" CACHE STRING "Renderer: gpu or sw")
set_property(CACHE STUDIO_RENDER PROPERTY STRINGS gpu sw)
if(NOT STUDIO_RENDER MATCHES "^(gpu|sw)$")
  message(FATAL_ERROR "STUDIO_RENDER must be 'gpu' or 'sw', got '${STUDIO_RENDER}'")
endif()

find_package(PkgConfig REQUIRED)
find_package(Threads REQUIRED)
pkg_check_modules(SDL2 REQUIRED IMPORTED_TARGET sdl2)
pkg_check_modules(PORTAUDIO REQUIRED IMPORTED_TARGET portaudio-2.0)
if(STUDIO_RENDER STREQUAL "gpu")
  pkg_check_modules(EGL REQUIRED IMPORTED_TARGET egl)
  pkg_check_modules(GLES REQUIRED IMPORTED_TARGET glesv2)
endif()

# LVGL: the same pinned release and trimmed build as edge/nova-hailo/cb_lvgl. FETCHCONTENT_SOURCE_DIR_LVGL
# (-D at configure time) reuses an unpacked tree when the machine is offline.
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

if(STUDIO_RENDER STREQUAL "gpu")
  target_compile_definitions(lvgl PUBLIC CB_RENDER_GPU=1)
  target_link_libraries(lvgl PUBLIC PkgConfig::SDL2 PkgConfig::EGL PkgConfig::GLES m)
else()
  target_compile_definitions(lvgl PUBLIC CB_RENDER_GPU=0)
  target_link_libraries(lvgl PUBLIC PkgConfig::SDL2 m)
endif()

set(STUDIO_WARN -Wall -Wextra -Werror)
file(GLOB STUDIO_FONTS ${CMAKE_CURRENT_SOURCE_DIR}/fonts/cb_*.c)
add_executable(aibox_studio
  src/main.c src/app.c src/display.c src/theme.c src/net.c src/ws.c src/json.c
  src/ui_kit.c src/logo_brain_chip.c src/voice.c src/sparkline.c src/nova/avatar.c src/nova/avatar_model.c src/nova/nova_mood.c src/nova/nova_fsm.c src/cb_ambient.c src/cb_snapshot.c
  src/screens/voice.c src/screens/overview.c src/screens/device.c src/screens/inspector.c
  src/screens/vision.c src/screens/chat.c src/screens/face.c
  ${STUDIO_FONTS})
target_include_directories(aibox_studio PRIVATE src fonts)
target_link_libraries(aibox_studio PRIVATE lvgl Threads::Threads PkgConfig::PORTAUDIO m)
target_compile_options(aibox_studio PRIVATE ${STUDIO_WARN})
