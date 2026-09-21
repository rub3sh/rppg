/*
 * Pulse/SpO2 analysis for a two-wavelength PPG stream.
 *
 * Everything here runs per sample at a fixed rate, in float, with no buffers
 * longer than one second — it has to keep up with the FIFO while WiFi and MQTT
 * share the same core.
 */
#pragma once
#include <Arduino.h>

struct Vitals {
  float bpm    = 0;      // 0 when no recent beat
  float spo2   = 0;      // 0 until a full window has been measured
  float pi     = 0;      // perfusion index, AC/DC of IR, in percent
  float ratio  = 0;      // R, the ratio of ratios SpO2 is derived from
  float dcIr   = 0;      // raw IR baseline, the finger-present evidence
  float dcRed  = 0;
  bool  finger = false;
  uint8_t conf = 0;      // 0..100, beat-interval agreement
};

class PulseAnalyzer {
 public:
  explicit PulseAnalyzer(float sampleRateHz = 100.0f);

  // Feed one red/IR pair. Returns the pleth value for display: the band-passed
  // IR channel, sign-flipped so a systolic upstroke reads as a rising edge.
  float push(uint32_t red, uint32_t ir, uint32_t nowMs);

  const Vitals &vitals() const { return v_; }

 private:
  void reset();
  void closeWindow();
  void onBeat(uint32_t nowMs);
  uint16_t medianIbi() const;

  const float fs_;
  const uint16_t windowN_;    // samples per SpO2 window (one second)

  // Per-channel DC baseline and band-passed AC.
  float dcIr_ = 0, dcRed_ = 0, acIr_ = 0, acRed_ = 0;
  bool primed_ = false;

  // One-second accumulators for the ratio of ratios.
  uint16_t wN_ = 0;
  float sumIr2_ = 0, sumRed2_ = 0, minPleth_ = 0, maxPleth_ = 0;

  // Beat detection.
  float env_ = 0;
  bool above_ = false;
  uint32_t lastBeatMs_ = 0;
  uint16_t ibi_[8];
  uint8_t ibiN_ = 0, ibiI_ = 0;

  Vitals v_;
};
