/*
 * Build-time configuration.
 *
 * Credentials live in include/secrets.h, which is not checked in. Copy
 * secrets.h.example over and fill it in; without it the firmware still builds
 * and runs, it just cannot join a network — useful for bench-testing the
 * sensor over USB serial alone.
 */
#pragma once

#if __has_include("secrets.h")
#include "secrets.h"
#endif

#ifndef WIFI_SSID
#define WIFI_SSID "set-me-in-secrets-h"
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef MQTT_HOST
#define MQTT_HOST "192.168.1.10"     // the machine running broker/docker-compose.yml
#endif
#ifndef MQTT_PORT
#define MQTT_PORT 1883
#endif
#ifndef MQTT_USER
#define MQTT_USER ""                 // empty = anonymous, matching the default broker
#endif
#ifndef MQTT_PASS
#define MQTT_PASS ""
#endif

// Topic root. Full topics are <base>/<device-id>/{status,vitals,ppg}, where the
// device id is derived from the MAC so two boards never collide.
#define TOPIC_BASE "pulseox"

// WiFi transmit power. The full 19.5 dBm draws a current spike at radio start
// that browns out a XIAO on a thin cable or an unpowered hub; 11 dBm is plenty
// for a house and leaves headroom. Raise it if the board is far from the AP
// and the supply is solid.
#ifndef WIFI_TX_POWER
#define WIFI_TX_POWER WIFI_POWER_11dBm
#endif

// I2C. D4/D5 on the XIAO ESP32-C3.
#define PIN_SDA 6
#define PIN_SCL 7

// Sample rate the sensor is configured for (400 Hz ADC / 4x averaging).
#define SAMPLE_RATE_HZ 100

// Publish cadence.
#define VITALS_INTERVAL_MS 1000
#define PPG_BATCH_SAMPLES  (SAMPLE_RATE_HZ / 4)   // 25 samples -> 4 batches/s

// LED drive current, 0.2 mA per step. Raise for cold or thick fingers, lower
// if the IR channel saturates (DC pinned near 262143).
#define LED_CURRENT_RED 0x2A
#define LED_CURRENT_IR  0x2A
