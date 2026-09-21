/*
 * Minimal radio test — 20 lines, no sensor, no MQTT, no I2C.
 *
 * Purpose: separate "this firmware does something expensive" from "this board
 * cannot start its radio on this supply". If this sketch brownouts too, no
 * amount of firmware work will fix it; the 3V3 rail needs help.
 *
 *   pio run -e wifimin -t upload
 */
#include <Arduino.h>
#include <WiFi.h>
#include <esp_system.h>

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);
  delay(500);
  Serial.printf("\n# wifimin: boot, reset reason %d\n", (int)esp_reset_reason());
  Serial.flush();
  delay(200);

  Serial.println("# wifimin: calling WiFi.mode(WIFI_STA)");
  Serial.flush();
  delay(50);

  WiFi.mode(WIFI_STA);

  Serial.println("# wifimin: SURVIVED radio power-up");
  Serial.flush();
}

void loop() {
  static uint32_t last = 0;
  if (millis() - last >= 1000) {
    last = millis();
    Serial.printf("# wifimin: alive %lus, status %d\n",
                  (unsigned long)(millis() / 1000), (int)WiFi.status());
  }
}
