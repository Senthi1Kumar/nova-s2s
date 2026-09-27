// Board wiring. Everything board-specific lives here; main.c is generic.
// Default: Waveshare ESP32-S3-Touch-LCD-1.85C V1 (MEMS I2S mic + PCM5101 DAC,
// no AEC). The V2 board (ES8311 + ES7210) needs codec init over I2C -- use
// esp_codec_dev, and ESP-SR AFE if you declare echo cancellation.
#pragma once
#include "driver/gpio.h"

#define BOARD_MIC_WS   GPIO_NUM_2
#define BOARD_MIC_SCK  GPIO_NUM_15
#define BOARD_MIC_SD   GPIO_NUM_39

#define BOARD_SPK_BCK  GPIO_NUM_48
#define BOARD_SPK_LRCK GPIO_NUM_38
#define BOARD_SPK_DIN  GPIO_NUM_47

#define BOARD_WAKE_BUTTON GPIO_NUM_0   // BOOT button stands in for a wake word

// MEMS mics deliver 24-bit audio left-justified in 32-bit slots.
#define BOARD_MIC_SHIFT 14
