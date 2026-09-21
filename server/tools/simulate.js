/*
 * Fake pulse oximeter.
 *
 * Publishes the same three topics the firmware does, at the same rates, with
 * the same last-will behaviour — so the server and dashboard can be developed
 * and tested with no hardware attached.
 *
 *   node tools/simulate.js [--id sim-01] [--bpm 72] [--spo2 97] [--drop 30]
 *
 * --drop N lifts the "finger" off the sensor for 8 seconds every N seconds,
 * which is the case worth testing: the charts must show a gap, not a straight
 * line, and the tiles must go stale rather than freeze on the last number.
 */
import mqtt from 'mqtt';

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};

const ID = arg('id', 'sim-01');
const BASE = arg('base', 'pulseox');
const URL = arg('url', 'mqtt://127.0.0.1:1883');
const BPM0 = Number(arg('bpm', 72));
const SPO20 = Number(arg('spo2', 97));
const DROP = Number(arg('drop', 0));

const FS = 100;
const BATCH = 25;

const topic = (kind) => `${BASE}/${ID}/${kind}`;

const client = mqtt.connect(URL, {
  clientId: `sim-${ID}-${process.pid}`,
  will: { topic: topic('status'), payload: JSON.stringify({ state: 'offline' }), qos: 1, retain: true },
});

client.on('connect', () => {
  console.log(`simulating ${ID} -> ${URL}`);
  client.publish(topic('status'), JSON.stringify({
    state: 'online', ip: '10.0.0.99', rssi: -52, fs: FS, fw: 'sim',
  }), { retain: true });
  start();
});
client.on('error', (e) => { console.error('mqtt:', e.message); process.exit(1); });

/** One cardiac cycle: systolic upstroke, then a smaller dicrotic bump. */
function pulseShape(phase) {
  const g = (mu, sigma) => Math.exp(-((phase - mu) ** 2) / (2 * sigma * sigma));
  return g(0.16, 0.055) + 0.32 * g(0.44, 0.085) - 0.12;
}

let phase = 0;
let t0 = Date.now();
let sampleIndex = 0;
let irBuf = [], redBuf = [];

function start() {
  const started = Date.now();

  setInterval(() => {
    const now = Date.now();
    // Generate every sample that should have happened since the last tick, so
    // the stream stays at 100 Hz even if the timer drifts.
    const due = Math.floor(((now - started) / 1000) * FS) - sampleIndex;
    for (let i = 0; i < due; i++) {
      sampleIndex++;
      const bpm = currentBpm(now);
      phase += bpm / 60 / FS;
      if (phase >= 1) phase -= 1;

      if (fingerOn(now)) {
        const amp = 900 * (1 + 0.08 * Math.sin((now - t0) / 4000));   // slow respiratory sway
        const v = pulseShape(phase) * amp;
        irBuf.push(Math.round(v + (Math.random() - 0.5) * 30));
        redBuf.push(Math.round(v * 0.62 + (Math.random() - 0.5) * 30));
      } else {
        irBuf.push(Math.round((Math.random() - 0.5) * 12));
        redBuf.push(Math.round((Math.random() - 0.5) * 12));
      }

      if (irBuf.length >= BATCH) {
        client.publish(topic('ppg'), JSON.stringify({
          t: now, fs: FS, n: BATCH, ir: irBuf.splice(0, BATCH), red: redBuf.splice(0, BATCH),
        }));
      }
    }
  }, 50);

  setInterval(() => {
    const now = Date.now();
    const on = fingerOn(now);
    const bpm = currentBpm(now);
    const spo2 = SPO20 + Math.sin((now - t0) / 23_000) * 0.8;
    client.publish(topic('vitals'), JSON.stringify({
      t: now - started,
      finger: on,
      bpm: on ? Number(bpm.toFixed(1)) : 0,
      spo2: on ? Number(spo2.toFixed(1)) : 0,
      pi: on ? Number((1.8 + Math.sin((now - t0) / 9000) * 0.5).toFixed(2)) : 0,
      r: on ? Number(((110 - spo2) / 25).toFixed(3)) : 0,
      conf: on ? 85 + Math.round(Math.random() * 12) : 0,
      dc_ir: on ? 95_000 + Math.round(Math.random() * 2000) : 1180,
      dc_red: on ? 78_000 + Math.round(Math.random() * 2000) : 1100,
      rssi: -52,
      overflows: 0,
    }));
  }, 1000);
}

function currentBpm(now) {
  // Heart rate wanders; a perfectly constant one would hide bugs in the chart's
  // autoscaling that a real trace would expose immediately.
  return BPM0 + Math.sin((now - t0) / 11_000) * 4 + Math.sin((now - t0) / 2700) * 1.2;
}

function fingerOn(now) {
  if (!DROP) return true;
  return ((now - t0) / 1000) % DROP > 8;
}

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    client.publish(topic('status'), JSON.stringify({ state: 'offline' }), { retain: true }, () => {
      client.end(true, () => process.exit(0));
    });
  });
}
