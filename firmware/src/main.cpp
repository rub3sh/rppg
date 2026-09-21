/*
 * Pulse oximeter — XIAO ESP32-C3 + MH-ET LIVE MAX30102.
 *
 *   sensor SDA -> D4 (GPIO6)      sensor VIN -> 3V3 (the MH-ET board has its
 *   sensor SCL -> D5 (GPIO7)      own regulator and level shifters, so 3.3 V
 *   sensor GND -> GND             is fine and 5 V is not required)
 *
 * Samples red+IR at 100 Hz, derives BPM and SpO2 on-device, and publishes:
 *
 *   pulseox/<id>/status   retained, {"state":"online"|"offline"} + the MQTT
 *                         last will, so the dashboard knows about a silent
 *                         death rather than showing a frozen number
 *   pulseox/<id>/vitals   1 Hz, the derived numbers
 *   pulseox/<id>/ppg      4 Hz, batches of 25 band-passed samples for the
 *                         scrolling waveform
 *
 * Timestamps are device millis(); the server stamps wall-clock time on
 * receipt, which keeps NTP out of the firmware.
 */
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <esp_system.h>

#include "config.h"
#include "max30102.h"
#include "ppg.h"

static const char *FW_VERSION = "1.0.0";

// The bringup env builds with -DBRINGUP_NO_WIFI: sensor and serial only, no
// radio. That is the first thing to try when the board reboots on its own —
// starting the radio is the current spike that browns out a marginal supply.
#ifdef BRINGUP_NO_WIFI
static const bool kNetworking = false;
#else
static const bool kNetworking = true;
#endif

MAX30102 sensor;
PulseAnalyzer analyzer(SAMPLE_RATE_HZ);
WiFiClient netClient;
PubSubClient mqtt(netClient);

static char deviceId[24];
static char topicStatus[64], topicVitals[64], topicPpg[64];
static char payload[900];

// One batch of waveform, filled at the sample rate and flushed at 4 Hz.
static int16_t waveIr[PPG_BATCH_SAMPLES];
static int16_t waveRed[PPG_BATCH_SAMPLES];
static uint8_t waveN = 0;
static uint32_t waveT0 = 0;

static bool sensorOk = false;
static uint32_t lastPollMs = 0, lastVitalsMs = 0, lastSerialMs = 0;
static uint32_t lastWifiTryMs = 0, lastMqttTryMs = 0;
static uint32_t fifoOverflows = 0;   // events, not samples: each one drops an unknown number

// ---------------------------------------------------------------- networking

static void publishStatus(bool online) {
  if (!mqtt.connected()) return;
  if (online) {
    snprintf(payload, sizeof(payload),
             "{\"state\":\"online\",\"ip\":\"%s\",\"rssi\":%d,\"fs\":%d,\"fw\":\"%s\"}",
             WiFi.localIP().toString().c_str(), (int)WiFi.RSSI(), SAMPLE_RATE_HZ, FW_VERSION);
  } else {
    snprintf(payload, sizeof(payload), "{\"state\":\"offline\"}");
  }
  mqtt.publish(topicStatus, payload, true);   // retained
}

static void mqttConnect() {
  const char *user = strlen(MQTT_USER) ? MQTT_USER : nullptr;
  const char *pass = strlen(MQTT_PASS) ? MQTT_PASS : nullptr;

  // The will is what makes an unplugged board visible: the broker publishes it
  // retained on the same topic the online message uses, so whichever arrives
  // last is what a newly connected dashboard sees.
  const bool ok = mqtt.connect(deviceId, user, pass,
                               topicStatus, 1, true, "{\"state\":\"offline\"}");
  Serial.printf("# mqtt %s (state %d)\n", ok ? "connected" : "connect failed", mqtt.state());
  if (ok) publishStatus(true);
}

static void netLoop() {
  if (!kNetworking) return;
  const uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    if (now - lastWifiTryMs > 10000) {
      lastWifiTryMs = now;
      Serial.printf("# wifi: connecting to %s\n", WIFI_SSID);
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
    return;
  }

  if (!mqtt.connected()) {
    if (now - lastMqttTryMs > 3000) {
      lastMqttTryMs = now;
      mqttConnect();
    }
    return;
  }
  mqtt.loop();
}

// ---------------------------------------------------------------- publishing

static void publishWave() {
  if (waveN == 0) return;
  if (mqtt.connected()) {
    int n = snprintf(payload, sizeof(payload),
                     "{\"t\":%lu,\"fs\":%d,\"n\":%u,\"ir\":[",
                     (unsigned long)waveT0, SAMPLE_RATE_HZ, waveN);
    for (uint8_t i = 0; i < waveN; i++)
      n += snprintf(payload + n, sizeof(payload) - n, i ? ",%d" : "%d", waveIr[i]);
    n += snprintf(payload + n, sizeof(payload) - n, "],\"red\":[");
    for (uint8_t i = 0; i < waveN; i++)
      n += snprintf(payload + n, sizeof(payload) - n, i ? ",%d" : "%d", waveRed[i]);
    snprintf(payload + n, sizeof(payload) - n, "]}");
    mqtt.publish(topicPpg, payload);
  }
  waveN = 0;
}

static void publishVitals(uint32_t now) {
  const Vitals &v = analyzer.vitals();
  if (!mqtt.connected()) return;
  snprintf(payload, sizeof(payload),
           "{\"t\":%lu,\"finger\":%s,\"bpm\":%.1f,\"spo2\":%.1f,\"pi\":%.2f,"
           "\"r\":%.3f,\"conf\":%u,\"dc_ir\":%.0f,\"dc_red\":%.0f,"
           "\"rssi\":%d,\"overflows\":%lu}",
           (unsigned long)now, v.finger ? "true" : "false",
           v.bpm, v.spo2, v.pi, v.ratio, v.conf, v.dcIr, v.dcRed,
           (int)WiFi.RSSI(), (unsigned long)fifoOverflows);
  mqtt.publish(topicVitals, payload);
}

// Is anything electrically on the bus at all?
//
// A powered I2C device brings its own pull-ups (4.7k on the MH-ET board), so a
// line driven low and released snaps back high in nanoseconds. A bare pin with
// no internal pull-up has nothing to pull it anywhere and stays low. This
// separates "sensor present but not answering" from "nothing is connected",
// which an address scan alone cannot do.
static void probeBusPins() {
  const uint8_t pins[2] = { PIN_SDA, PIN_SCL };
  const char *names[2] = { "SDA/D4", "SCL/D5" };
  for (uint8_t i = 0; i < 2; i++) {
    pinMode(pins[i], OUTPUT);
    digitalWrite(pins[i], LOW);
    delayMicroseconds(50);
    pinMode(pins[i], INPUT);            // INPUT, not INPUT_PULLUP: no internal help
    delayMicroseconds(5);
    const int fast = digitalRead(pins[i]);
    delayMicroseconds(1000);
    const int slow = digitalRead(pins[i]);
    Serial.printf("# %s: %s\n", names[i],
                  fast ? "pulled up (device powered on the bus)"
                       : slow ? "weak pull-up — long wires or no module pull-ups"
                              : "floating — nothing connected, or no power to the module");
  }
}

// Reports every address that acknowledges. A MAX30102 answers at 0x57; an
// empty bus means power or wiring, and an unexpected address means the module
// is not what the silkscreen says.
static bool scanFoundAny = false;

static void scanI2c(const char *label) {
  uint8_t found = 0;
  Serial.printf("# i2c scan @%s:", label);
  for (uint8_t addr = 0x08; addr < 0x78; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", addr);
      found++;
    }
  }
  Serial.println(found ? "" : " nothing responded");
  scanFoundAny = found > 0;
}

// --------------------------------------------------------------------- setup

void setup() {
  Serial.begin(115200);
  // Native USB CDC blocks when the host is not draining it; a zero timeout
  // makes writes drop instead of stalling the sample loop.
  Serial.setTxTimeoutMs(0);
  delay(300);

  // Panic output goes to UART0, which this board does not expose, so the
  // reset reason is the only forensic evidence available over USB.
  static const char *kResetReason[] = {
    "unknown", "power-on", "external", "software", "panic", "interrupt watchdog",
    "task watchdog", "other watchdog", "deep sleep", "brownout", "sdio",
  };
  const int reason = (int)esp_reset_reason();
  Serial.printf("# reset: %s (%d)\n",
                reason >= 0 && reason < (int)(sizeof(kResetReason) / sizeof(*kResetReason))
                    ? kResetReason[reason] : "?", reason);

  uint8_t mac[6];
  WiFi.macAddress(mac);
  snprintf(deviceId, sizeof(deviceId), "pulseox-%02x%02x%02x", mac[3], mac[4], mac[5]);
  snprintf(topicStatus, sizeof(topicStatus), "%s/%s/status", TOPIC_BASE, deviceId);
  snprintf(topicVitals, sizeof(topicVitals), "%s/%s/vitals", TOPIC_BASE, deviceId);
  snprintf(topicPpg,    sizeof(topicPpg),    "%s/%s/ppg",    TOPIC_BASE, deviceId);

  Serial.printf("\n# %s  fw %s\n# topics: %s | %s | %s\n",
                deviceId, FW_VERSION, topicStatus, topicVitals, topicPpg);

  probeBusPins();                       // before Wire.begin(), which adds pull-ups

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);
  Wire.setTimeOut(20);

  scanI2c("400kHz");
  if (!scanFoundAny) {
    // Long jumpers slow the bus edges; a device that cannot ack at 400 kHz
    // often still answers at 100 kHz.
    Wire.setClock(100000);
    scanI2c("100kHz");
  }

  sensorOk = sensor.begin(Wire);
  if (sensorOk) {
    Serial.printf("# max30102: part 0x%02X rev 0x%02X, %d Hz\n",
                  sensor.partId(), sensor.revision(), SAMPLE_RATE_HZ);
    sensor.setLedCurrent(LED_CURRENT_RED, LED_CURRENT_IR);
  } else {
    Serial.println("# max30102: NOT FOUND — check SDA=D4, SCL=D5, 3V3, GND");
  }

  if (kNetworking) {
    // Powering up the PHY is the biggest current step this firmware takes. If
    // this is the last line the board ever prints, the supply cannot carry it:
    // check the reset reason on the next boot, it will say brownout.
    Serial.println(F("# wifi: starting radio")); Serial.flush();
    WiFi.mode(WIFI_STA);
    // Modem sleep stays enabled (the Arduino default): it cuts average current
    // on a supply that has already proved marginal, and only costs latency on
    // inbound packets, which this firmware does not wait for.
    //
    // TX power is capped for the same reason. Starting the radio at full power
    // is what browns out a board on a thin USB cable; raise WIFI_TX_POWER in
    // config.h if range matters more than margin.
    WiFi.setTxPower(WIFI_TX_POWER);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    lastWifiTryMs = millis();

    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setKeepAlive(15);
    // A waveform batch is ~350 bytes; PubSubClient's 256-byte default would
    // silently drop every one of them.
    mqtt.setBufferSize(1024);
    Serial.printf("# wifi: radio up, joining %s\n", WIFI_SSID);
  } else {
    Serial.println(F("# networking disabled (bringup build)"));
  }
}

// ---------------------------------------------------------------------- loop

void loop() {
  netLoop();

  const uint32_t now = millis();
  if (sensorOk && now - lastPollMs >= 5) {
    lastPollMs = now;

    PpgSample buf[32];
    const uint8_t n = sensor.read(buf, 32);
    if (sensor.overflowed()) {
      sensor.clearOverflow();
      fifoOverflows++;
      Serial.println("# fifo overflow — samples lost");
    }

    // The FIFO arrives in bursts, so space the samples backwards from now at
    // the nominal period. That anchors beat timing to the ESP32 crystal
    // instead of to whenever the loop happened to drain the FIFO.
    const uint32_t stepMs = 1000 / SAMPLE_RATE_HZ;
    for (uint8_t i = 0; i < n; i++) {
      const uint32_t ts = now - (uint32_t)(n - 1 - i) * stepMs;
      const float pleth = analyzer.push(buf[i].red, buf[i].ir, ts);

      if (waveN == 0) waveT0 = ts;
      if (waveN < PPG_BATCH_SAMPLES) {
        waveIr[waveN] = (int16_t)constrain(pleth, -32000.0f, 32000.0f);
        // Red is carried for diagnostics: if it goes flat while IR is healthy,
        // the SpO2 number is meaningless and this is where you see it.
        waveRed[waveN] = (int16_t)constrain(analyzer.vitals().finger
                                            ? (float)(buf[i].red - analyzer.vitals().dcRed) * -1.0f
                                            : 0.0f, -32000.0f, 32000.0f);
        waveN++;
      }
      if (waveN >= PPG_BATCH_SAMPLES) publishWave();
    }
  }

  if (now - lastVitalsMs >= VITALS_INTERVAL_MS) {
    lastVitalsMs = now;
    publishVitals(now);
  }

  if (now - lastSerialMs >= 1000) {
    lastSerialMs = now;
    const Vitals &v = analyzer.vitals();
    Serial.printf("%s bpm=%5.1f spo2=%5.1f pi=%4.2f%% r=%4.2f conf=%3u dc_ir=%6.0f %s\n",
                  v.finger ? "FINGER " : "no-finger",
                  v.bpm, v.spo2, v.pi, v.ratio, v.conf, v.dcIr,
                  !kNetworking ? "no-radio"
                    : mqtt.connected() ? "mqtt"
                    : (WiFi.status() == WL_CONNECTED ? "wifi" : "offline"));
  }
}
