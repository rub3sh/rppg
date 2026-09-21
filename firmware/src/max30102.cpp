#include "max30102.h"

namespace {
// Register map (MAX30102 datasheet, rev 1).
const uint8_t REG_FIFO_WR_PTR  = 0x04;
const uint8_t REG_OVF_COUNTER  = 0x05;
const uint8_t REG_FIFO_RD_PTR  = 0x06;
const uint8_t REG_FIFO_DATA    = 0x07;
const uint8_t REG_FIFO_CONFIG  = 0x08;
const uint8_t REG_MODE_CONFIG  = 0x09;
const uint8_t REG_SPO2_CONFIG  = 0x0A;
const uint8_t REG_LED1_PA      = 0x0C;   // red
const uint8_t REG_LED2_PA      = 0x0D;   // infrared
const uint8_t REG_REV_ID       = 0xFE;
const uint8_t REG_PART_ID      = 0xFF;

const uint8_t MODE_RESET = 0x40;
const uint8_t MODE_SPO2  = 0x03;         // both LEDs, red then IR in the FIFO

// I2C burst ceiling. Each sample is 6 bytes (3 per channel) and the ESP32
// Wire buffer is 128 bytes, so 16 samples per transaction stays well clear.
const uint8_t kSamplesPerBurst = 16;
}  // namespace

bool MAX30102::begin(TwoWire &bus, uint8_t addr) {
  bus_ = &bus;
  addr_ = addr;

  bus_->beginTransmission(addr_);
  if (bus_->endTransmission() != 0) return false;

  if (!read8(REG_PART_ID, &partId_)) return false;
  if (partId_ != kExpectedPartId) return false;
  read8(REG_REV_ID, &rev_);

  // A reset leaves every register at its power-on value, so configuration
  // below is complete rather than differential.
  if (!write8(REG_MODE_CONFIG, MODE_RESET)) return false;
  const uint32_t deadline = millis() + 100;
  uint8_t mode = MODE_RESET;
  while (millis() < deadline) {
    if (read8(REG_MODE_CONFIG, &mode) && (mode & MODE_RESET) == 0) break;
    delay(1);
  }
  if (mode & MODE_RESET) return false;

  // FIFO: average 4 consecutive samples in hardware, roll over when full.
  // Averaging is free noise reduction — it is why we run the ADC at 400 Hz
  // for a 100 Hz output rate rather than sampling at 100 Hz directly.
  const uint8_t smpAve = 0b010;                 // 4x
  write8(REG_FIFO_CONFIG, (smpAve << 5) | (1 << 4) | 0x0F);

  // SpO2: ADC full scale 4096 nA, 400 Hz, 215 us pulse width (17-bit).
  // 411 us would buy another bit but is out of spec for two LEDs at 400 Hz.
  const uint8_t adcRange = 0b01, sampleRate = 0b011, pulseWidth = 0b10;
  write8(REG_SPO2_CONFIG, (adcRange << 5) | (sampleRate << 2) | pulseWidth);

  if (!setLedCurrent(0x2A, 0x2A)) return false;  // 8.4 mA each

  write8(REG_FIFO_WR_PTR, 0);
  write8(REG_OVF_COUNTER, 0);
  write8(REG_FIFO_RD_PTR, 0);
  overflow_ = false;

  return write8(REG_MODE_CONFIG, MODE_SPO2);
}

bool MAX30102::setLedCurrent(uint8_t red, uint8_t ir) {
  return write8(REG_LED1_PA, red) && write8(REG_LED2_PA, ir);
}

uint8_t MAX30102::read(PpgSample *out, uint8_t max) {
  if (bus_ == nullptr || max == 0) return 0;

  uint8_t wr = 0, rd = 0, ovf = 0;
  if (!read8(REG_FIFO_WR_PTR, &wr)) return 0;
  if (!read8(REG_OVF_COUNTER, &ovf)) return 0;
  if (!read8(REG_FIFO_RD_PTR, &rd)) return 0;

  uint8_t pending = (wr - rd) & 0x1F;
  if (ovf > 0) {
    // Write pointer has lapped the read pointer: the two are equal but the
    // FIFO is full, not empty, and `ovf` samples are already gone.
    overflow_ = true;
    pending = 32;
  }
  if (pending == 0) return 0;
  if (pending > max) pending = max;

  uint8_t got = 0;
  uint8_t buf[kSamplesPerBurst * 6];
  while (got < pending) {
    uint8_t n = pending - got;
    if (n > kSamplesPerBurst) n = kSamplesPerBurst;
    if (readBurst(REG_FIFO_DATA, buf, n * 6) != n * 6) break;
    for (uint8_t i = 0; i < n; i++) {
      const uint8_t *p = buf + i * 6;
      // Big-endian 18-bit, left-padded into 3 bytes; the top 6 bits are junk.
      out[got + i].red = (((uint32_t)p[0] << 16) | ((uint32_t)p[1] << 8) | p[2]) & 0x03FFFF;
      out[got + i].ir  = (((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 8) | p[5]) & 0x03FFFF;
    }
    got += n;
  }
  return got;
}

bool MAX30102::write8(uint8_t reg, uint8_t value) {
  bus_->beginTransmission(addr_);
  bus_->write(reg);
  bus_->write(value);
  return bus_->endTransmission() == 0;
}

bool MAX30102::read8(uint8_t reg, uint8_t *value) {
  return readBurst(reg, value, 1) == 1;
}

uint8_t MAX30102::readBurst(uint8_t reg, uint8_t *buf, uint8_t len) {
  bus_->beginTransmission(addr_);
  bus_->write(reg);
  if (bus_->endTransmission(false) != 0) return 0;   // repeated start
  uint8_t n = bus_->requestFrom(addr_, len);
  for (uint8_t i = 0; i < n && i < len; i++) buf[i] = bus_->read();
  return n;
}
