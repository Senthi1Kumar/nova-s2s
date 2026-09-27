cmake_minimum_required(VERSION 3.24)
project(nova_face C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

include(FetchContent)
find_package(SDL2 REQUIRED)
find_package(Threads REQUIRED)

# LVGL from its built-in defaults plus the few options this app needs,
# instead of a copied 1,500-line lv_conf.h.
set(LV_CONF_SKIP ON CACHE BOOL "" FORCE)
# LVGL's own demos/examples default to ON and this app needs neither -- skip
# them so the build only compiles the library and this app.
set(CONFIG_LV_BUILD_DEMOS OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
FetchContent_Declare(lvgl
  GIT_REPOSITORY https://github.com/lvgl/lvgl.git
  GIT_TAG v9.6.0
  GIT_SHALLOW TRUE)
FetchContent_MakeAvailable(lvgl)
target_compile_definitions(lvgl PUBLIC
  LV_USE_SDL=1
  LV_COLOR_FORMAT_DEFAULT=LV_COLOR_FORMAT_XRGB8888
  LV_USE_STDLIB_MALLOC=LV_STDLIB_CLIB
  LV_USE_STDLIB_STRING=LV_STDLIB_CLIB
  LV_USE_STDLIB_SPRINTF=LV_STDLIB_CLIB
  LV_FONT_MONTSERRAT_14=1
  LV_FONT_MONTSERRAT_20=1)
target_link_libraries(lvgl PUBLIC SDL2::SDL2)

# cJSON: its one source file, not its CMake project (tests, shared libs).
FetchContent_Declare(cjson
  GIT_REPOSITORY https://github.com/DaveGamble/cJSON.git
  GIT_TAG v1.7.19
  GIT_SHALLOW TRUE
  SOURCE_SUBDIR _not_a_cmake_project)
FetchContent_MakeAvailable(cjson)
add_library(cjson_min STATIC ${cjson_SOURCE_DIR}/cJSON.c)
target_include_directories(cjson_min PUBLIC ${cjson_SOURCE_DIR})

add_executable(nova_face src/main.c src/nova_face.c)
target_link_libraries(nova_face PRIVATE lvgl cjson_min Threads::Threads m)
target_compile_options(nova_face PRIVATE "$<$<C_COMPILER_ID:GNU,Clang,AppleClang>:-Wall;-Wextra>")
