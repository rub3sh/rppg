/*
 * Pulse-oximeter dashboard.
 *
 * Three views of the same stream, each with a different job:
 *   - stat tiles      the current numbers, read at a glance from across a room
 *   - pleth canvas    the raw pulse shape, which is how you tell a good reading
 *                     from a hand that moved
 *   - history charts  heart rate and SpO2 over time, one chart each
 *
 * Heart rate and SpO2 never share an axis. They are different quantities on
 * different scales, and overlaying them on twin axes invents correlations that
 * are artefacts of the scaling.
 */
'use strict';

const $ = (sel) => document.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const COLORS = {
  pulse: css('--pulse'),
  oxygen: css('--oxygen'),
  grid: css('--grid'),
  axis: css('--axis'),
  muted: css('--ink-muted'),
  ink2: css('--ink-2'),
  surface: css('--surface'),
};

// Ten seconds of pleth on screen, drawn a quarter-second behind the newest
// sample so the 4 Hz batches have always landed before the playhead wants them.
const WAVE_WINDOW_MS = 10_000;
const WAVE_LAG_MS = 350;

const state = {
  devices: [],
  deviceId: null,
  wave: [],            // {t, v} — v is the band-passed IR sample
  history: [],         // {t, bpm, spo2, pi, conf}
  bucket: 1000,
  minutes: 10,
  vitals: null,
  lastVitalsAt: 0,
  wsOpen: false,
};

// ------------------------------------------------------------------ websocket

let backoff = 500;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    backoff = 500;
    state.wsOpen = true;
    setChip('#wsState', '#wsStateText', 'live', 'connected');
  };

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handle(msg);
  };

  ws.onclose = () => {
    state.wsOpen = false;
    setChip('#wsState', '#wsStateText', 'offline', 'reconnecting');
    // Backing off matters when the server is down: a tight retry loop from a
    // parked browser tab is a surprising amount of traffic.
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10_000);
  };

  ws.onerror = () => ws.close();
}

function handle(msg) {
  if (msg.type === 'snapshot' || msg.type === 'devices') {
    setDevices(msg.devices || []);
    return;
  }
  if (msg.device !== state.deviceId) return;

  if (msg.type === 'vitals') {
    state.vitals = msg.vitals;
    state.lastVitalsAt = msg.ts;
    paintTiles();
    appendHistory(msg.ts, msg.vitals);
  } else if (msg.type === 'ppg') {
    pushWave(msg);
  }
}

// -------------------------------------------------------------------- devices

function setDevices(devices) {
  state.devices = devices;

  const sel = $('#device');
  const ids = devices.map((d) => d.id);
  if (ids.join() !== [...sel.options].map((o) => o.value).join()) {
    sel.innerHTML = devices.length
      ? devices.map((d) => `<option value="${d.id}">${d.id}</option>`).join('')
      : '<option value="">no devices</option>';
  }

  if (!state.deviceId || !ids.includes(state.deviceId)) {
    const live = devices.find((d) => d.live) || devices[0];
    if (live) selectDevice(live.id);
  }
  if (state.deviceId) sel.value = state.deviceId;

  const d = devices.find((x) => x.id === state.deviceId);
  if (!d) setChip('#devState', '#devStateText', 'offline', 'no device');
  else if (d.live) setChip('#devState', '#devStateText', 'live', `${d.id} online`);
  else if (d.online) setChip('#devState', '#devStateText', 'stale', `${d.id} stale`);
  else setChip('#devState', '#devStateText', 'offline', `${d.id} offline`);
}

function selectDevice(id) {
  state.deviceId = id;
  state.wave = [];
  state.history = [];
  state.vitals = null;
  paintTiles();
  loadHistory();
}

// Status is a dot plus words. The colour is reinforcement, never the message.
function setChip(chipSel, textSel, stateName, text) {
  $(chipSel).dataset.state = stateName;
  $(textSel).textContent = text;
}

// ---------------------------------------------------------------------- tiles

function fmt(v, digits, dash = '--') {
  return Number.isFinite(v) && v > 0 ? v.toFixed(digits) : dash;
}

function paintTiles() {
  const v = state.vitals;
  const stale = !v || !v.finger || Date.now() - state.lastVitalsAt > 5000;

  setTile('#tileBpm', fmt(v && v.bpm, 0), stale);
  setTile('#tileSpo2', fmt(v && v.spo2, 1), stale);
  setTile('#tilePi', fmt(v && v.pi, 2), stale);
  setTile('#tileConf', v && v.finger && Number.isFinite(v.conf) ? String(v.conf) : '--', stale);
  setTile('#tileR', fmt(v && v.r, 3), stale);

  $('#bpmNote').textContent = !v ? 'waiting for a device'
    : !v.finger ? 'no finger on the sensor'
    : v.pi < 0.1 ? 'weak pulse — press gently and hold still'
    : v.conf < 60 ? 'unsteady — intervals disagree'
    : 'steady';
}

function setTile(sel, text, stale) {
  const tile = $(sel);
  const value = tile.querySelector('.value');
  const unit = value.querySelector('.unit');
  value.firstChild.nodeValue = text;
  if (unit) value.appendChild(unit);
  tile.classList.toggle('stale', stale);
}

// ------------------------------------------------------------- pleth waveform

function pushWave(msg) {
  const fs = msg.fs || 100;
  const step = 1000 / fs;
  const ir = msg.ir || [];
  for (let i = 0; i < ir.length; i++) {
    state.wave.push({ t: msg.ts - (ir.length - 1 - i) * step, v: ir[i] });
  }
  const cutoff = Date.now() - WAVE_WINDOW_MS * 1.5;
  while (state.wave.length && state.wave[0].t < cutoff) state.wave.shift();
}

const waveCanvas = $('#wave');
const waveCtx = waveCanvas.getContext('2d');
let playhead = 0;
let waveScale = 1;

function sizeCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.round(rect.width * dpr));
  const h = Math.max(1, Math.round(canvas.height * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  return { w, h, dpr };
}

function drawWave() {
  const { w, h } = sizeCanvas(waveCanvas);
  const ctx = waveCtx;
  ctx.clearRect(0, 0, w, h);

  const mid = h / 2;
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(w, mid);
  ctx.stroke();

  const from = playhead - WAVE_WINDOW_MS;
  const pts = state.wave.filter((p) => p.t >= from && p.t <= playhead);

  const hasSignal = pts.length > 2 && state.vitals && state.vitals.finger;
  if (!hasSignal) {
    ctx.fillStyle = COLORS.muted;
    ctx.font = `${13 * (window.devicePixelRatio || 1)}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.fillText(state.vitals ? 'no finger on the sensor' : 'waiting for data', w / 2, mid - 10);
    return;
  }

  // Autoscale to the window's own amplitude, eased. A hard rescale each frame
  // makes a steady pulse look like it is breathing.
  let peak = 1;
  for (const p of pts) peak = Math.max(peak, Math.abs(p.v));
  waveScale += (peak * 1.25 - waveScale) * 0.08;

  ctx.strokeStyle = COLORS.pulse;
  ctx.lineWidth = 2 * (window.devicePixelRatio || 1);
  ctx.lineJoin = 'round';
  ctx.beginPath();
  for (let i = 0; i < pts.length; i++) {
    const x = ((pts[i].t - from) / WAVE_WINDOW_MS) * w;
    const y = mid - (pts[i].v / waveScale) * (h * 0.42);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

let lastFrame = performance.now();
function frame(now) {
  const dt = now - lastFrame;
  lastFrame = now;

  // The playhead runs on the browser's clock and is nudged toward the newest
  // sample, rather than jumping to it. That turns 4 batches a second into a
  // continuous slide instead of four visible steps.
  playhead += dt;
  const newest = state.wave.length ? state.wave[state.wave.length - 1].t : Date.now();
  const target = newest - WAVE_LAG_MS;
  if (Math.abs(playhead - target) > 1000) playhead = target;
  else playhead += (target - playhead) * 0.03;

  drawWave();
  requestAnimationFrame(frame);
}

// -------------------------------------------------------------- history charts

function makeChart(canvasSel, tipSel, { color, unit, digits, floor }) {
  const canvas = $(canvasSel);
  const ctx = canvas.getContext('2d');
  const tip = $(tipSel);
  let layout = null;
  let hoverX = null;

  canvas.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    hoverX = e.clientX - rect.left;
    draw();
  });
  canvas.addEventListener('mouseleave', () => {
    hoverX = null;
    tip.style.opacity = 0;
    draw();
  });

  function draw(rows, key) {
    if (rows) { draw.rows = rows; draw.key = key; }
    const data = (draw.rows || []).filter((r) => Number.isFinite(r[draw.key]) && r[draw.key] > 0);
    const { w, h, dpr } = sizeCanvas(canvas);
    ctx.clearRect(0, 0, w, h);

    const padL = 38 * dpr, padR = 12 * dpr, padT = 8 * dpr, padB = 20 * dpr;
    const plotW = w - padL - padR, plotH = h - padT - padB;

    const now = Date.now();
    const t0 = now - state.minutes * 60_000;

    let lo = Infinity, hi = -Infinity;
    for (const r of data) { lo = Math.min(lo, r[draw.key]); hi = Math.max(hi, r[draw.key]); }
    if (!data.length) { lo = floor[0]; hi = floor[1]; }
    // A floor on the span stops a flat trace from being magnified into noise.
    if (hi - lo < floor[2]) {
      const mid = (hi + lo) / 2;
      lo = mid - floor[2] / 2; hi = mid + floor[2] / 2;
    }
    const pad = (hi - lo) * 0.1;
    lo -= pad; hi += pad;

    const X = (t) => padL + ((t - t0) / (now - t0)) * plotW;
    const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * plotH;
    layout = { padL, padT, plotW, plotH, t0, now, lo, hi, X, Y, dpr, data };

    // Gridlines and ticks recede: they orient, they do not compete.
    ctx.font = `${10 * dpr}px system-ui, sans-serif`;
    ctx.fillStyle = COLORS.muted;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const v = lo + ((hi - lo) * i) / 3;
      const y = Y(v);
      ctx.strokeStyle = COLORS.grid;
      ctx.beginPath();
      ctx.moveTo(padL, y); ctx.lineTo(w - padR, y);
      ctx.stroke();
      ctx.fillText(v.toFixed(digits), padL - 6 * dpr, y);
    }

    ctx.strokeStyle = COLORS.axis;
    ctx.beginPath();
    ctx.moveTo(padL, padT + plotH); ctx.lineTo(w - padR, padT + plotH);
    ctx.stroke();

    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let i = 0; i <= 2; i++) {
      const t = t0 + ((now - t0) * i) / 2;
      ctx.fillText(timeLabel(t), X(t), padT + plotH + 6 * dpr);
    }

    if (!data.length) {
      ctx.fillStyle = COLORS.muted;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = `${12 * dpr}px system-ui, sans-serif`;
      ctx.fillText('no readings in this range', padL + plotW / 2, padT + plotH / 2);
      return;
    }

    // Break the line across gaps rather than interpolating: a straight line
    // through a finger-off period would be a claim we cannot support.
    const gap = state.bucket * 2.5;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2 * dpr;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    let pen = false;
    for (const r of data) {
      const x = X(r.t), y = Y(r[draw.key]);
      if (!pen) { ctx.moveTo(x, y); pen = true; }
      else ctx.lineTo(x, y);
      if (r.gapAfter > gap) pen = false;
    }
    ctx.stroke();

    // One direct label, on the latest point — not a number on every point.
    const last = data[data.length - 1];
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(X(last.t), Y(last[draw.key]), 3 * dpr, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = COLORS.ink2;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'bottom';
    ctx.font = `${11 * dpr}px system-ui, sans-serif`;
    ctx.fillText(`${last[draw.key].toFixed(digits)} ${unit}`, X(last.t) - 6 * dpr, Y(last[draw.key]) - 6 * dpr);

    if (hoverX !== null) drawCrosshair();
  }

  function drawCrosshair() {
    const { dpr, padL, padT, plotH, X, Y, data } = layout;
    const px = hoverX * dpr;
    let best = null, bestDist = Infinity;
    for (const r of data) {
      const d = Math.abs(X(r.t) - px);
      if (d < bestDist) { bestDist = d; best = r; }
    }
    if (!best || bestDist > 40 * dpr) { tip.style.opacity = 0; return; }

    ctx.strokeStyle = COLORS.axis;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(X(best.t), padT); ctx.lineTo(X(best.t), padT + plotH);
    ctx.stroke();

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(X(best.t), Y(best[draw.key]), 4 * dpr, 0, Math.PI * 2);
    ctx.fill();

    tip.innerHTML = `<span class="t">${timeLabel(best.t, true)}</span><b>${best[draw.key].toFixed(digits)}</b> ${unit}`;
    tip.style.opacity = 1;
    const rect = canvas.getBoundingClientRect();
    const card = canvas.closest('.card').getBoundingClientRect();
    const left = rect.left - card.left + X(best.t) / dpr;
    tip.style.left = `${Math.min(Math.max(left + 10, 4), card.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${rect.top - card.top + Y(best[draw.key]) / dpr - 40}px`;
  }

  return { draw, redraw: () => draw() };
}

function timeLabel(t, withSeconds) {
  const d = new Date(t);
  const p = (n) => String(n).padStart(2, '0');
  return withSeconds
    ? `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    : `${p(d.getHours())}:${p(d.getMinutes())}`;
}

const chartBpm = makeChart('#chartBpm', '#tipBpm', { color: COLORS.pulse, unit: 'bpm', digits: 0, floor: [50, 100, 12] });
const chartSpo2 = makeChart('#chartSpo2', '#tipSpo2', { color: COLORS.oxygen, unit: '%', digits: 1, floor: [92, 100, 4] });

// -------------------------------------------------------------------- history

async function loadHistory() {
  if (!state.deviceId) { paintHistory(); return; }
  try {
    const res = await fetch(`/api/history?device=${encodeURIComponent(state.deviceId)}&minutes=${state.minutes}`);
    const data = await res.json();
    state.bucket = data.bucket || 1000;
    state.history = markGaps(data.rows || []);
    $('#rangeNote').textContent = `${state.history.length} points, ${Math.round(state.bucket / 1000)}s buckets`;
  } catch {
    state.history = [];
    $('#rangeNote').textContent = 'history unavailable';
  }
  paintHistory();
}

function markGaps(rows) {
  for (let i = 0; i < rows.length; i++) {
    rows[i].gapAfter = i + 1 < rows.length ? rows[i + 1].t - rows[i].t : 0;
  }
  return rows;
}

// Live readings extend the newest bucket rather than appending a point per
// second, so the live tail keeps the same resolution as the loaded history.
function appendHistory(ts, v) {
  if (!v.finger || !(v.bpm > 0)) return;
  const bucket = Math.floor(ts / state.bucket) * state.bucket;
  const last = state.history[state.history.length - 1];
  if (last && last.t === bucket) {
    last.n = (last.n || 1) + 1;
    last.bpm += (v.bpm - last.bpm) / last.n;
    last.spo2 += ((v.spo2 || 0) - last.spo2) / last.n;
    last.pi += ((v.pi || 0) - last.pi) / last.n;
    last.conf += ((v.conf || 0) - last.conf) / last.n;
  } else {
    if (last) last.gapAfter = bucket - last.t;
    state.history.push({ t: bucket, bpm: v.bpm, spo2: v.spo2 || 0, pi: v.pi || 0, conf: v.conf || 0, n: 1, gapAfter: 0 });
  }
  const cutoff = Date.now() - state.minutes * 60_000;
  while (state.history.length && state.history[0].t < cutoff) state.history.shift();
  paintHistory();
}

function paintHistory() {
  chartBpm.draw(state.history, 'bpm');
  chartSpo2.draw(state.history, 'spo2');
  paintTable();
}

function paintTable() {
  const rows = state.history.slice(-40).reverse();
  $('#table tbody').innerHTML = rows.length
    ? rows.map((r) => `<tr><td>${timeLabel(r.t, true)}</td><td>${r.bpm.toFixed(0)}</td>` +
        `<td>${r.spo2 ? r.spo2.toFixed(1) : '--'}</td><td>${r.pi ? r.pi.toFixed(2) : '--'}</td>` +
        `<td>${r.conf ? r.conf.toFixed(0) : '--'}</td></tr>`).join('')
    : '<tr><td colspan="5">no readings yet</td></tr>';
}

// ----------------------------------------------------------------------- init

$('#device').addEventListener('change', (e) => selectDevice(e.target.value));

for (const btn of document.querySelectorAll('.ranges button')) {
  btn.addEventListener('click', () => {
    for (const b of document.querySelectorAll('.ranges button')) b.setAttribute('aria-pressed', 'false');
    btn.setAttribute('aria-pressed', 'true');
    state.minutes = Number(btn.dataset.min);
    loadHistory();
  });
}

window.addEventListener('resize', () => { chartBpm.redraw(); chartSpo2.redraw(); });

// Tiles go stale on their own if the device stops publishing, so a frozen
// number is never mistaken for a current one.
setInterval(paintTiles, 1000);

paintTiles();
paintHistory();
connect();
requestAnimationFrame(frame);
