cmake_minimum_required(VERSION 3.15)
project(hailo_llm_cpp LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(HailoRT 5 REQUIRED)
find_package(pybind11 REQUIRED)

pybind11_add_module(hailo_llm_cpp hailo_llm_cpp.cpp)
target_link_libraries(hailo_llm_cpp PRIVATE HailoRT::libhailort)
