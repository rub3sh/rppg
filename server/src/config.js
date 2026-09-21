import { fileURLToPath } from 'node:url';

const num = (v, fallback) => (v === undefined ? fallback : Number(v));

export const config = {
  // Broker. The compose stack in broker/ listens on 1883 with no auth.
  mqttUrl: process.env.MQTT_URL ?? 'mqtt://127.0.0.1:1883',
  mqttUser: process.env.MQTT_USER || undefined,
  mqttPass: process.env.MQTT_PASS || undefined,
  topicBase: process.env.TOPIC_BASE ?? 'pulseox',

  port: num(process.env.PORT, 8080),
  host: process.env.HOST ?? '0.0.0.0',

  dbPath: process.env.DB_PATH ?? fileURLToPath(new URL('../data/pulseox.db', import.meta.url)),
  webRoot: fileURLToPath(new URL('../../web/', import.meta.url)),

  // History is cheap (one row per second per device) but not free: a week is
  // ~600k rows and a few tens of MB.
  retentionDays: num(process.env.RETENTION_DAYS, 7),

  // A device that has not published within this window is treated as stale
  // even if its retained status still says "online" — belt to the will's braces.
  staleAfterMs: num(process.env.STALE_AFTER_MS, 10_000),
};
