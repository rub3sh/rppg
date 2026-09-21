# Contact pulse oximetry — MAX30102 → MQTT → dashboard

A XIAO ESP32-C3 reads a MH-ET LIVE MAX30102 at 100 Hz, derives heart rate and
SpO2 on-device, and publishes over MQTT. A small Node service stores the
history and fans the live stream out to a browser dashboard.

```
XIAO ESP32C3 ──I2C──> MAX30102        D4/GPIO6 SDA, D5/GPIO7 SCL, 3V3, GND
      │
      │ WiFi + MQTT (JSON)
      ▼
Mosquitto (broker/, Docker)
      ▼
Node server (server/) ── SQLite history ── WebSocket ──> browser (web/)
```

> Not a medical device. SpO2 comes from an uncalibrated ratio-of-ratios curve
> and is good for trends, not for clinical decisions.

## Quick start

```bash
cd broker && docker compose up -d      # skip if you already run a broker on 1883
cd ../server && npm install && npm start
xdg-open http://localhost:8080
```

With no hardware, feed it a fake device:

```bash
cd server && node tools/simulate.js --id sim-01 --drop 40
```

`--drop 40` lifts the finger for 8 s every 40 s, which is the case worth
watching: the charts must break the line rather than interpolate across it,
and the tiles must go stale rather than freeze on the last good number.

## Firmware

```bash
cd firmware
cp include/secrets.h.example include/secrets.h     # WiFi + broker address
pio run -t upload
pio device monitor
```

Three build environments, each with a job:

| env | what it does | when to use it |
|---|---|---|
| `seeed_xiao_esp32c3` | the real thing: sensor + WiFi + MQTT | normal operation |
| `bringup` | sensor + serial only, radio never started | the board reboots, or you want to test the sensor with no network |
| `wifimin` | 20 lines: start the radio, print, nothing else | decide whether a brownout is this firmware's fault or the board's supply |

The firmware reports its own diagnosis at boot, which is most of the debugging:

```
# reset: brownout (9)                 <- why it last restarted
# SDA/D4: pulled up (device powered on the bus)
# i2c scan @400kHz: 0x57              <- retries at 100 kHz if nothing answers
# max30102: part 0x15 rev 0x03, 100 Hz
# wifi: starting radio                <- last line before a supply gives out
```

## Topics

Base is `pulseox/<device-id>`, where the id is derived from the MAC
(`pulseox-b54114`). Timestamps are device milliseconds; the server stamps
wall-clock time on receipt, which keeps NTP out of the firmware.

| topic | rate | payload |
|---|---|---|
| `…/status` | on change, retained | `{"state":"online","ip","rssi","fs","fw"}` |
| `…/vitals` | 1 Hz | `{"t","finger","bpm","spo2","pi","r","conf","dc_ir","dc_red","rssi","overflows"}` |
| `…/ppg` | 4 Hz | `{"t","fs":100,"n":25,"ir":[…],"red":[…]}` |

`status` is also the MQTT last will (`{"state":"offline"}`, retained), so an
unplugged board shows as offline instead of leaving a frozen number on screen.

## How the numbers are derived

Per sample, in float, at 100 Hz:

- **DC** — a one-pole low pass at ~0.16 Hz per channel: slow enough to pass
  under the pulse, fast enough to follow a finger settling into place.
- **AC** — the residual, smoothed at ~5 Hz and sign-flipped, because more blood
  means more absorption means *less* returned light.
- **Beats** — a Schmitt trigger on a decaying peak envelope, with a 285 ms
  refractory gap that also rejects the dicrotic notch. BPM is the median of the
  last 8 intervals; `conf` is how many of them agree with that median within
  20%, which catches motion artefact before the median does.
- **SpO2** — `R = (ACred/DCred) / (ACir/DCir)` over a one-second window, then
  `110 − 25R`, clamped and smoothed over ~4 s. The curve is empirical and
  uncalibrated; calibrate against a certified oximeter before trusting it.
- **Finger** — IR DC above 40 000 counts, with hysteresis at 25 000. Losing the
  finger resets every derived value immediately, because a stale BPM on an
  empty sensor is the one failure mode that actively misleads.

## Server

Two dependencies (`mqtt`, `ws`); SQLite via the built-in `node:sqlite`.

| endpoint | returns |
|---|---|
| `GET /api/health` | broker connection, WebSocket client count, uptime |
| `GET /api/devices` | live device state plus what is in the database |
| `GET /api/history?device=&minutes=` | bucket-averaged history |
| `WS /ws` | `snapshot`, `devices`, `vitals`, `ppg` |

History is averaged into buckets sized to the requested range, so 24 hours
costs the same as 10 minutes. Empty buckets are absent rather than zero, which
is what lets the dashboard draw a gap instead of inventing a reading. The
waveform is never stored — it is 100 values a second and nothing reads it back.

Configuration is by environment variable; see `server/.env.example`.

## Dashboard

Heart rate and SpO2 get **one chart each**. They are different quantities on
different scales, and putting them on twin axes manufactures correlations that
are artefacts of the scaling. The pleth canvas runs its playhead on the
browser's clock and eases it toward the newest sample, turning four batches a
second into a continuous slide rather than four visible steps.

There is a table view under the charts: the same data, read without colour.
