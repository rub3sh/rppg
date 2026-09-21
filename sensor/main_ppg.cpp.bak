/*
 * Contact PPG reference — XIAO ESP32-C3 + analog pulse sensor + 0.91" SSD1306
 *
 * Purpose: produce a trustworthy heart-rate reference to validate the camera
 * (rPPG) measurement against. Samples at a FIXED rate, filters, detects beats,
 * shows BPM on the OLED, and streams CSV over USB serial for logging.
 *
 * Wiring (see README): sensor signal -> D1/GPIO3 (ADC1), OLED on D4/D5 (I2C).
 */
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ---------- pins ----------
static const int PIN_PULSE = 3;    // D1 = GPIO3, ADC1_CH3 (NOT a strapping pin)
static const int PIN_SDA   = 6;    // D4
static const int PIN_SCL   = 7;    // D5

// ---------- sampling ----------
static const uint32_t FS_HZ    = 100;              // fixed sample rate
static const uint32_t PERIOD_US = 1000000UL / FS_HZ;

// ---------- display ----------
static const int SCR_W = 128, SCR_H = 32;
Adafruit_SSD1306 display(SCR_W, SCR_H, &Wire, -1);
bool haveDisplay = false;

// ---------- filter state ----------
// Band-pass by difference of two exponential averages: the slow one is the DC
// baseline (perfusion + ambient), the fast one smooths sensor noise. What's
// left is the pulsatile component, roughly 0.7-3.5 Hz at this sample rate.
float slowEma = 0, fastEma = 0;
const float A_SLOW = 0.006f;      // ~ 0.1 Hz corner at 100 Hz
const float A_FAST = 0.28f;       // ~ 5 Hz corner

// adaptive threshold
float envPos = 0, envNeg = 0;
const float A_ENV = 0.002f;

// ---------- beat detection ----------
uint32_t lastBeatMs = 0;
bool     above      = false;
uint16_t ibi[8];  uint8_t ibiN = 0, ibiI = 0;
float    bpm = 0;
uint32_t lastBeatSeen = 0;

// ---------- waveform for the OLED ----------
int8_t wave[SCR_W];  uint8_t waveI = 0;

static uint16_t medianIBI() {
  if (ibiN == 0) return 0;
  uint16_t v[8]; memcpy(v, ibi, sizeof(uint16_t) * ibiN);
  for (uint8_t i = 1; i < ibiN; i++) {         // insertion sort, n <= 8
    uint16_t k = v[i]; int8_t j = i - 1;
    while (j >= 0 && v[j] > k) { v[j+1] = v[j]; j--; }
    v[j+1] = k;
  }
  return v[ibiN / 2];
}

void setup() {
  Serial.begin(115200);
  // Native USB CDC blocks when the host isn't draining the buffer; a zero
  // timeout makes writes drop instead of stalling the sampling loop.
  Serial.setTxTimeoutMs(0);
  delay(300);

  analogReadResolution(12);                     // 0..4095
  analogSetPinAttenuation(PIN_PULSE, ADC_11db); // full 0..3.3 V range

  // The display is optional. Every step is announced first, so a hang is
  // attributable instead of silent - a floating I2C bus can block for a long
  // time, and that must never take the sampler down with it.
  Serial.println(F("# i2c: begin"));
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);
  Wire.setTimeOut(20);

  uint8_t addr = 0;
  for (uint8_t a : {0x3C, 0x3D}) {          // only the two SSD1306 addresses
    Serial.print(F("# i2c: probe 0x")); Serial.println(a, HEX);
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { addr = a; break; }
  }
  Serial.print(F("# i2c: found ")); Serial.println(addr ? addr : 0, HEX);

  if (addr) {
    Serial.println(F("# display: init"));
    haveDisplay = display.begin(SSD1306_SWITCHCAPVCC, addr);
  }
  Serial.print(F("# display: ")); Serial.println(haveDisplay ? F("ok") : F("absent"));
  if (haveDisplay) {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println(F("PPG reference"));
    display.println(F("place finger..."));
    display.display();
  }

  // prime the baseline so the first seconds aren't a huge transient
  uint32_t acc = 0;
  for (int i = 0; i < 100; i++) { acc += analogRead(PIN_PULSE); delay(2); }
  slowEma = fastEma = acc / 100.0f;

  Serial.println(F("# contact PPG reference, XIAO ESP32-C3"));
  Serial.println(F("# ms,raw,filtered,bpm,beat"));
}

void loop() {
  static uint32_t nextUs = micros();
  if ((int32_t)(micros() - nextUs) < 0) return;   // hold the sample rate
  nextUs += PERIOD_US;

  const uint32_t nowMs = millis();
  const int raw = analogRead(PIN_PULSE);

  slowEma += A_SLOW * (raw - slowEma);
  fastEma += A_FAST * (raw - fastEma);
  const float sig = fastEma - slowEma;           // band-passed pulse

  // track the signal envelope so the threshold follows perfusion changes
  if (sig > envPos) envPos = sig; else envPos += A_ENV * (sig - envPos);
  if (sig < envNeg) envNeg = sig; else envNeg += A_ENV * (sig - envNeg);
  const float amp = envPos - envNeg;
  const float thr = envNeg + 0.62f * amp;        // fires on the systolic upstroke

  uint8_t beat = 0;
  if (amp > 60) {                                // enough perfusion to trust a beat
    if (!above && sig > thr) {
      above = true;
      const uint32_t dt = nowMs - lastBeatMs;
      // refractory 300 ms = 200 BPM ceiling; 2000 ms = 30 BPM floor
      if (dt > 300 && dt < 2000) {
        // Reject intervals inconsistent with the established rhythm: a missed
        // beat doubles the interval and a double-trigger halves it, and either
        // one poisons the median for the next eight beats.
        const uint16_t m0 = medianIBI();
        const bool plausible = (m0 == 0) || (dt > m0 * 0.62f && dt < m0 * 1.6f);
        if (plausible) {
          ibi[ibiI] = dt; ibiI = (ibiI + 1) % 8;
          if (ibiN < 8) ibiN++;
          const uint16_t m = medianIBI();
          if (m && ibiN >= 3) bpm = 60000.0f / m;   // wait for 3 before reporting
          beat = 1;
          lastBeatSeen = nowMs;
        }
      }
      lastBeatMs = nowMs;
    } else if (above && sig < envNeg + 0.42f * amp) {
      above = false;                             // hysteresis
    }
  } else {
    if (nowMs - lastBeatSeen > 3000) { bpm = 0; ibiN = ibiI = 0; }
  }

  // CSV out. Sampling stays at 100 Hz; logging is decimated to 25 Hz, which is
  // still >8x the pulse band and a quarter of the USB traffic. A beat is never
  // dropped - it forces a line out.
  static uint8_t dec = 0;
  if (beat || (++dec >= 4)) {
    if (!beat) dec = 0; else dec = 0;
    Serial.print(nowMs);   Serial.print(',');
    Serial.print(raw);     Serial.print(',');
    Serial.print(sig, 2);  Serial.print(',');
    Serial.print(bpm, 1);  Serial.print(',');
    Serial.println(beat);
  }

  // loop-health beacon, so a stall is visible instead of silent
  static uint32_t loops = 0, lastBeacon = 0;
  loops++;
  if (nowMs - lastBeacon >= 2000) {
    Serial.print(F("# loop "));  Serial.print(loops / 2);
    Serial.print(F(" Hz, amp ")); Serial.print(amp, 0);
    Serial.print(F(", thr "));    Serial.println(thr, 0);
    loops = 0; lastBeacon = nowMs;
  }

  // ---------- display, refreshed ~12 Hz ----------
  static uint32_t lastDraw = 0;
  wave[waveI] = (int8_t)constrain(sig * 24.0f / (amp > 25 ? amp : 25), -15, 15);
  waveI = (waveI + 1) % SCR_W;

  if (haveDisplay && nowMs - lastDraw > 80) {
    lastDraw = nowMs;
    display.clearDisplay();
    display.setTextSize(2);
    display.setCursor(0, 2);
    if (bpm > 0) display.print((int)roundf(bpm)); else display.print(F("--"));
    display.setTextSize(1);
    display.setCursor(0, 22);
    display.print(F("BPM"));
    if (nowMs - lastBeatSeen < 120) { display.fillCircle(40, 26, 3, SSD1306_WHITE); }

    for (int x = 0; x < SCR_W - 56; x++) {       // waveform strip on the right
      const int i = (waveI + x) % SCR_W;
      display.drawPixel(56 + x, 16 - wave[i], SSD1306_WHITE);
    }
    display.display();
  }
}
