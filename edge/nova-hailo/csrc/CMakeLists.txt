cmake_minimum_required(VERSION 3.15)
project(hailo_llm_cpp LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(HailoRT 5 REQUIRED)
find_package(pybind11 REQUIRED)

if(NOT HailoRT_VERSION)
  find_program(PKG_CONFIG_EXECUTABLE pkg-config)
  if(PKG_CONFIG_EXECUTABLE)
    execute_process(
      COMMAND ${PKG_CONFIG_EXECUTABLE} --modversion hailort
      OUTPUT_VARIABLE HailoRT_VERSION
      OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET
    )
  endif()
endif()

message(STATUS "HailoRT package version: ${HailoRT_VERSION}")

pybind11_add_module(hailo_llm_cpp hailo_llm_cpp.cpp)
target_link_libraries(hailo_llm_cpp PRIVATE HailoRT::libhailort)

# Headers also define HAILORT_*_VERSION; these CMake flags match the installed
# package so a 5.3 Pi builds the 5.2+ generate(prompts, tools) overload.
if(HailoRT_VERSION VERSION_GREATER_EQUAL "5.2")
  target_compile_definitions(hailo_llm_cpp PRIVATE HAILO_GENAI_LLM_HAS_TOOLS=1)
endif()
if(HailoRT_VERSION VERSION_GREATER_EQUAL "5.3")
  target_compile_definitions(hailo_llm_cpp PRIVATE HAILO_GENAI_LLM_HAS_LONG_READ_TIMEOUT=1)
endif()
