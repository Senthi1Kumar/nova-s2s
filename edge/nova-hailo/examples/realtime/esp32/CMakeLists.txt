cmake_minimum_required(VERSION 3.16)
# Wi-Fi join via the IDF example helper (SSID/password in menuconfig).
set(EXTRA_COMPONENT_DIRS $ENV{IDF_PATH}/examples/common_components/protocol_examples_common)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(nova_realtime_device)
