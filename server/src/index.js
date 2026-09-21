/*
 * Pulse-oximeter backend.
 *
 *   MQTT  ->  SQLite (vitals history)
 *         ->  WebSocket fan-out (vitals + waveform, live)
 *   HTTP  ->  the dashboard in web/ and a small history API
 *
 * Two dependencies, one process, no build step.
 */
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, sep } from 'node:path';
import mqtt from 'mqtt';
import { WebSocketServer } from 'ws';

import { config } from './config.js';
import { openDb } from './db.js';

const db = openDb(config.dbPath);

/** Live device state, keyed by device id. Rebuilt from retained MQTT messages. */
const devices = new Map();

function touch(id) {
  let d = devices.get(id);
  if (!d) {
    d = { id, online: false, lastSeen: 0, ip: null, rssi: null, fw: null, vitals: null };
    devices.set(id, d);
  }
  return d;
}

/** A device is only "live" if it says online AND has published recently. */
function deviceList() {
  const now = Date.now();
  return [...devices.values()].map((d) => ({
    ...d,
    live: d.online && now - d.lastSeen < config.staleAfterMs,
  }));
}

// ------------------------------------------------------------------ WebSocket

const wss = new WebSocketServer({ noServer: true });

function broadcast(msg) {
  const text = JSON.stringify(msg);
  for (const ws of wss.clients) {
    if (ws.readyState === ws.OPEN) ws.send(text);
  }
}

wss.on('connection', (ws) => {
  ws.isAlive = true;
  ws.on('pong', () => { ws.isAlive = true; });
  ws.send(JSON.stringify({ type: 'snapshot', devices: deviceList(), now: Date.now() }));
});

// Browsers on a sleeping laptop leave half-open sockets behind; ping them out.
const heartbeat = setInterval(() => {
  for (const ws of wss.clients) {
    if (!ws.isAlive) { ws.terminate(); continue; }
    ws.isAlive = false;
    ws.ping();
  }
}, 15_000);

// Device liveness is time-based, so the dashboard needs a nudge when a device
// goes quiet without a will being delivered (broker restart, for instance).
let lastLive = '';
setInterval(() => {
  const live = deviceList();
  const key = live.map((d) => `${d.id}:${d.live}`).join('|');
  if (key !== lastLive) {
    lastLive = key;
    broadcast({ type: 'devices', devices: live });
  }
}, 2000);

// ----------------------------------------------------------------------- MQTT

const client = mqtt.connect(config.mqttUrl, {
  username: config.mqttUser,
  password: config.mqttPass,
  clientId: `pulseox-server-${process.pid}`,
  reconnectPeriod: 2000,
});

client.on('connect', () => {
  const topics = [`${config.topicBase}/+/status`, `${config.topicBase}/+/vitals`, `${config.topicBase}/+/ppg`];
  client.subscribe(topics, (err) => {
    if (err) console.error('subscribe failed:', err.message);
    else console.log(`mqtt connected to ${config.mqttUrl}, subscribed to ${config.topicBase}/+/{status,vitals,ppg}`);
  });
});
client.on('error', (err) => console.error('mqtt:', err.message));
client.on('offline', () => console.warn('mqtt offline, retrying'));

client.on('message', (topic, buf) => {
  const parts = topic.split('/');
  const [base, id, kind] = parts;
  if (base !== config.topicBase || parts.length !== 3) return;

  let msg;
  try {
    msg = JSON.parse(buf.toString());
  } catch {
    console.warn(`bad JSON on ${topic}`);
    return;
  }

  const now = Date.now();
  const d = touch(id);

  if (kind === 'status') {
    d.online = msg.state === 'online';
    if (d.online) {
      d.ip = msg.ip ?? null;
      d.rssi = msg.rssi ?? null;
      d.fw = msg.fw ?? null;
      d.lastSeen = now;
    }
    broadcast({ type: 'devices', devices: deviceList() });
    console.log(`device ${id} ${msg.state}${msg.ip ? ` (${msg.ip})` : ''}`);
    return;
  }

  d.lastSeen = now;

  if (kind === 'vitals') {
    d.vitals = { ...msg, serverTs: now };
    db.insert(id, now, msg);
    broadcast({ type: 'vitals', device: id, ts: now, vitals: msg });
  } else if (kind === 'ppg') {
    // Waveform is pass-through only: live or not at all.
    broadcast({ type: 'ppg', device: id, ts: now, fs: msg.fs, ir: msg.ir, red: msg.red });
  }
});

// ----------------------------------------------------------------------- HTTP

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

function sendJson(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(text) });
  res.end(text);
}

async function serveStatic(res, urlPath) {
  const rel = normalize(urlPath === '/' ? '/index.html' : urlPath).replace(/^(\.\.[/\\])+/, '');
  const file = join(config.webRoot, rel);
  // normalize() plus this check is what stops /../../etc/passwd.
  if (!file.startsWith(config.webRoot.endsWith(sep) ? config.webRoot : config.webRoot + sep)) {
    res.writeHead(403).end('forbidden');
    return;
  }
  try {
    const body = await readFile(file);
    res.writeHead(200, {
      'content-type': MIME[extname(file)] ?? 'application/octet-stream',
      'cache-control': 'no-cache',
    });
    res.end(body);
  } catch {
    res.writeHead(404, { 'content-type': 'text/plain' }).end('not found');
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`);

  if (url.pathname === '/api/devices') {
    return sendJson(res, 200, { devices: deviceList(), stored: db.devices() });
  }

  if (url.pathname === '/api/history') {
    const device = url.searchParams.get('device');
    if (!device) return sendJson(res, 400, { error: 'device is required' });
    const minutes = Math.min(Math.max(Number(url.searchParams.get('minutes')) || 10, 1), 60 * 24 * 7);
    const { bucket, rows } = db.history(device, Date.now() - minutes * 60_000);
    return sendJson(res, 200, { device, minutes, bucket, rows });
  }

  if (url.pathname === '/api/health') {
    return sendJson(res, 200, {
      ok: true,
      mqtt: client.connected,
      clients: wss.clients.size,
      devices: deviceList().length,
      uptimeSec: Math.round(process.uptime()),
    });
  }

  if (req.method !== 'GET') {
    return res.writeHead(405, { allow: 'GET' }).end('method not allowed');
  }
  return serveStatic(res, url.pathname);
});

server.on('upgrade', (req, socket, head) => {
  const { pathname } = new URL(req.url, 'http://localhost');
  if (pathname !== '/ws') return socket.destroy();
  wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req));
});

server.listen(config.port, config.host, () => {
  console.log(`dashboard on http://localhost:${config.port}  (db ${config.dbPath})`);
});

// ------------------------------------------------------------------ retention

const pruneTimer = setInterval(() => {
  const cutoff = Date.now() - config.retentionDays * 86_400_000;
  const removed = db.prune(cutoff);
  if (removed) console.log(`pruned ${removed} rows older than ${config.retentionDays}d`);
}, 3_600_000);

function shutdown() {
  console.log('\nshutting down');
  clearInterval(heartbeat);
  clearInterval(pruneTimer);
  client.end(true);
  for (const ws of wss.clients) ws.close();
  server.close();
  db.close();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
