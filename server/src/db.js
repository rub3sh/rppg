import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

/**
 * Vitals history. One row per device per second; the waveform is deliberately
 * not stored — it is 100 values a second per device and nothing reads it back.
 */
export function openDb(path) {
  mkdirSync(dirname(path), { recursive: true });
  const db = new DatabaseSync(path);

  // WAL keeps the 1 Hz writes from blocking dashboard reads.
  db.exec(`
    PRAGMA journal_mode = WAL;
    PRAGMA synchronous = NORMAL;
    CREATE TABLE IF NOT EXISTS vitals (
      id      INTEGER PRIMARY KEY,
      device  TEXT    NOT NULL,
      ts      INTEGER NOT NULL,   -- server receive time, unix ms
      bpm     REAL,
      spo2    REAL,
      pi      REAL,
      ratio   REAL,
      conf    INTEGER,
      finger  INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS vitals_device_ts ON vitals (device, ts);
  `);

  const insertStmt = db.prepare(`
    INSERT INTO vitals (device, ts, bpm, spo2, pi, ratio, conf, finger)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  `);
  const devicesStmt = db.prepare(`
    SELECT device, MAX(ts) AS lastTs, COUNT(*) AS rows FROM vitals GROUP BY device
  `);
  const pruneStmt = db.prepare(`DELETE FROM vitals WHERE ts < ?`);

  return {
    insert(device, ts, v) {
      insertStmt.run(
        device, ts,
        Number.isFinite(v.bpm) ? v.bpm : null,
        Number.isFinite(v.spo2) ? v.spo2 : null,
        Number.isFinite(v.pi) ? v.pi : null,
        Number.isFinite(v.r) ? v.r : null,
        Number.isFinite(v.conf) ? v.conf : null,
        v.finger ? 1 : 0,
      );
    },

    /**
     * Averaged into buckets so a 24-hour range costs the same as a 10-minute
     * one. Empty buckets are simply absent, which is what lets the chart draw
     * a gap instead of interpolating across a missing finger.
     */
    history(device, sinceMs, maxPoints = 900) {
      const span = Math.max(1, Date.now() - sinceMs);
      const bucket = Math.max(1000, Math.ceil(span / maxPoints / 1000) * 1000);
      const rows = db.prepare(`
        SELECT (ts / ?) * ? AS t,
               AVG(bpm)  AS bpm,
               AVG(spo2) AS spo2,
               AVG(pi)   AS pi,
               AVG(conf) AS conf
        FROM vitals
        WHERE device = ? AND ts >= ? AND finger = 1 AND bpm > 0
        GROUP BY t ORDER BY t
      `).all(bucket, bucket, device, sinceMs);
      return { bucket, rows };
    },

    devices() {
      return devicesStmt.all();
    },

    prune(olderThanMs) {
      return pruneStmt.run(olderThanMs).changes;
    },

    close() { db.close(); },
  };
}
