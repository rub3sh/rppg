/*
 * Minimal MAX30102 driver — register level, no vendor library.
 *
 * The part is a two-LED (660 nm red + 880 nm IR) reflectance pulse oximeter
 * front end with a 32-deep sample FIFO. We drive it in SpO2 mode, let its own
 * oscillator set the sample rate, and drain the FIFO from the main loop; the
 * INT pin is not needed because 32 samples at 100 Hz is 320 ms of slack.
 */
#pragma once
#include <Arduino.h>
#include <Wire.h>

struct PpgSample {
  uint32_t red;   // 18-bit ADC counts, LED1
  uint32_t ir;    // 18-bit ADC counts, LED2
};

class MAX30102 {
 public:
  // Resets the part, checks PART_ID, and applies the SpO2-mode configuration.
  // Returns false if nothing answers at `addr` or the ID is not a MAX30102.
  bool begin(TwoWire &bus, uint8_t addr = kDefaultAddress);

  // Red/IR drive current, 0..255 in 0.2 mA steps (0x2A = 8.4 mA).
  // Higher current buys signal on thick or cold fingers at the cost of heat.
  bool setLedCurrent(uint8_t red, uint8_t ir);

  // Pulls every unread sample out of the FIFO. Returns how many were written
  // to `out` (at most `max`); 0 means the FIFO was empty, which is normal.
  uint8_t read(PpgSample *out, uint8_t max);

  // True if the FIFO overflowed since the last read — i.e. the loop stalled
  // long enough to lose samples, which makes the waveform lie about timing.
  bool overflowed() const { return overflow_; }
  void clearOverflow() { overflow_ = false; }

  uint8_t partId() const { return partId_; }
  uint8_t revision() const { return rev_; }

  static const uint8_t kDefaultAddress = 0x57;
  static const uint8_t kExpectedPartId = 0x15;

 private:
  bool write8(uint8_t reg, uint8_t value);
  bool read8(uint8_t reg, uint8_t *value);
  uint8_t readBurst(uint8_t reg, uint8_t *buf, uint8_t len);

  TwoWire *bus_ = nullptr;
  uint8_t addr_ = kDefaultAddress;
  uint8_t partId_ = 0, rev_ = 0;
  bool overflow_ = false;
};
