#include "ppg.h"
#include <math.h>
#include <string.h>

namespace {
// A finger on the sensor drives the IR baseline far above the dark reading;
// with 8.4 mA of drive an uncovered sensor sits in the low thousands.
const float FINGER_ON_IR  = 40000.0f;
const float FINGER_OFF_IR = 25000.0f;   // hysteresis, so a loose finger doesn't chatter

// Below this perfusion index the "beats" are noise, not blood.
const float MIN_PI = 0.04f;

// Beat plausibility: 30-210 BPM, with a refractory gap that also rejects the
// dicrotic notch being counted as a second beat.
const uint16_t MIN_IBI_MS = 285;
const uint16_t MAX_IBI_MS = 2000;
const uint32_t BEAT_TIMEOUT_MS = 3000;

// Schmitt trigger levels as a fraction of the tracked pulse envelope.
const float THR_HI = 0.55f, THR_LO = 0.25f;

float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
}  // namespace

PulseAnalyzer::PulseAnalyzer(float sampleRateHz)
    : fs_(sampleRateHz), windowN_((uint16_t)sampleRateHz) {
  memset(ibi_, 0, sizeof(ibi_));
}

void PulseAnalyzer::reset() {
  acIr_ = acRed_ = 0;
  wN_ = 0; sumIr2_ = sumRed2_ = 0; minPleth_ = maxPleth_ = 0;
  env_ = 0; above_ = false;
  lastBeatMs_ = 0; ibiN_ = ibiI_ = 0;
  v_.bpm = v_.spo2 = v_.pi = v_.ratio = 0;
  v_.conf = 0;
}

float PulseAnalyzer::push(uint32_t red, uint32_t ir, uint32_t nowMs) {
  const float fIr = (float)ir, fRed = (float)red;

  if (!primed_) {                    // start the baselines on the first sample
    dcIr_ = fIr; dcRed_ = fRed; primed_ = true;
  }

  // DC baseline: a slow one-pole low pass, ~0.16 Hz at 100 Hz. Slow enough to
  // pass under the pulse, fast enough to follow a finger settling in place.
  const float aDc = 10.0f / fs_;
  dcIr_  += aDc * (fIr  - dcIr_);
  dcRed_ += aDc * (fRed - dcRed_);
  v_.dcIr = dcIr_; v_.dcRed = dcRed_;

  // Finger presence, with hysteresis. Losing the finger invalidates every
  // derived number immediately — a stale BPM on an empty sensor is the one
  // failure mode that actively misleads.
  const bool wasFinger = v_.finger;
  if (!v_.finger && dcIr_ > FINGER_ON_IR)  v_.finger = true;
  if (v_.finger  && dcIr_ < FINGER_OFF_IR) v_.finger = false;
  if (wasFinger != v_.finger) reset();
  if (!v_.finger) return 0.0f;

  // AC: the residual after the baseline, smoothed by a fast one-pole (~5 Hz)
  // to knock down ADC noise without touching the pulse shape. Inverted,
  // because more blood means more absorption means less returned light.
  const float aAc = 0.35f;
  acIr_  += aAc * (-(fIr  - dcIr_) - acIr_);
  acRed_ += aAc * (-(fRed - dcRed_) - acRed_);
  const float pleth = acIr_;

  // --- one-second window for the ratio of ratios ---
  if (wN_ == 0) { minPleth_ = maxPleth_ = pleth; }
  sumIr2_  += acIr_ * acIr_;
  sumRed2_ += acRed_ * acRed_;
  if (pleth < minPleth_) minPleth_ = pleth;
  if (pleth > maxPleth_) maxPleth_ = pleth;
  if (++wN_ >= windowN_) closeWindow();

  // --- beat detection on the pleth ---
  // Envelope follows peaks instantly and decays ~10%/s, so the threshold
  // tracks a weakening signal instead of latching onto one good beat.
  if (pleth > env_) env_ = pleth;
  else env_ *= (1.0f - 1.0f / (fs_ * 10.0f));

  const bool strongEnough = v_.pi >= MIN_PI || v_.pi == 0.0f;
  if (above_) {
    if (pleth < THR_LO * env_) above_ = false;
  } else if (strongEnough && env_ > 0 && pleth > THR_HI * env_) {
    above_ = true;
    onBeat(nowMs);
  }

  if (v_.bpm > 0 && lastBeatMs_ != 0 && nowMs - lastBeatMs_ > BEAT_TIMEOUT_MS) {
    v_.bpm = 0; v_.conf = 0; ibiN_ = ibiI_ = 0;
  }

  return pleth;
}

void PulseAnalyzer::onBeat(uint32_t nowMs) {
  if (lastBeatMs_ == 0) { lastBeatMs_ = nowMs; return; }
  const uint32_t ibi = nowMs - lastBeatMs_;
  if (ibi < MIN_IBI_MS) return;                 // refractory: not a new beat
  lastBeatMs_ = nowMs;
  if (ibi > MAX_IBI_MS) { ibiN_ = ibiI_ = 0; return; }   // gap: start over

  ibi_[ibiI_] = (uint16_t)ibi;
  ibiI_ = (ibiI_ + 1) % 8;
  if (ibiN_ < 8) ibiN_++;

  const uint16_t med = medianIbi();
  if (med == 0) return;
  v_.bpm = 60000.0f / (float)med;

  // Confidence is simply how many of the recent intervals agree with the
  // median within 20% — motion artefact shows up here before it shows up in
  // the BPM, which the median hides.
  uint8_t agree = 0;
  for (uint8_t i = 0; i < ibiN_; i++) {
    if (fabsf((float)ibi_[i] - (float)med) <= 0.20f * (float)med) agree++;
  }
  v_.conf = (uint8_t)((100 * agree) / ibiN_);
}

void PulseAnalyzer::closeWindow() {
  const float acIrRms  = sqrtf(sumIr2_  / (float)wN_);
  const float acRedRms = sqrtf(sumRed2_ / (float)wN_);

  v_.pi = dcIr_ > 0 ? (maxPleth_ - minPleth_) / dcIr_ * 100.0f : 0.0f;

  if (dcIr_ > 0 && dcRed_ > 0 && acIrRms > 0) {
    const float r = (acRedRms / dcRed_) / (acIrRms / dcIr_);
    v_.ratio = r;
    // Uncalibrated empirical curve (Maxim's linear approximation). Good for
    // watching trends; not a medical measurement, and it needs a real
    // calibration against a certified oximeter before the number means much.
    const float spo2 = clampf(110.0f - 25.0f * r, 70.0f, 100.0f);
    // Four-second smoothing: SpO2 physiologically cannot step around, so
    // anything fast in this number is artefact.
    v_.spo2 = (v_.spo2 == 0) ? spo2 : v_.spo2 + 0.25f * (spo2 - v_.spo2);
  }

  wN_ = 0; sumIr2_ = sumRed2_ = 0;
}

uint16_t PulseAnalyzer::medianIbi() const {
  if (ibiN_ == 0) return 0;
  uint16_t v[8];
  memcpy(v, ibi_, sizeof(uint16_t) * ibiN_);
  for (uint8_t i = 1; i < ibiN_; i++) {          // insertion sort, n <= 8
    uint16_t k = v[i]; int8_t j = (int8_t)i - 1;
    while (j >= 0 && v[j] > k) { v[j + 1] = v[j]; j--; }
    v[j + 1] = k;
  }
  return v[ibiN_ / 2];
}
