/* Shared rPPG DSP - used by the mobile capture page. */
const DSP_VERSION = 'v3-doubleMA';   // reported to the monitor so the
                                      // running build is never in doubt
const LO = 0.8, HI = 3.0;          // Hz  -> 48-180 BPM
const WIN = 15, MINWIN = 5, HOP = 0.35;   // s  full window / first output / cadence
const NF = []; for (let f = LO; f <= HI; f += 0.004) NF.push(f);

// YCbCr chroma box: keys on hue/saturation, so it separates skin from wood,
// beige walls and hair, which a plain R>G>B ordering test does not.
function isSkin(r, g, b) {
  const cr = 128 + (0.5*r - 0.418688*g - 0.081312*b);
  const cb = 128 + (-0.168736*r - 0.331264*g + 0.5*b);
  return r > 32 && r - g > 6 && cr > 132 && cr < 184 && cb > 72 && cb < 132;
}

// Subtracting a single centred moving average is a comb filter, not a clean
// highpass: 1 - sinc(pi*f*tau) ripples by ~2.9 dB across the pulse band and, at
// tau = 1.5 s, peaks at 0.95 Hz -- exactly 57 BPM. That manufactured a peak
// there and biased every estimate toward it by 2.2 dB.
//
// Applying the average TWICE gives a triangular window, so the response becomes
// 1 - sinc^2, which is non-negative and monotone. Ripple drops to 0.14 dB across
// 48-180 BPM while drift below 0.1 Hz is still cut by 12 dB.
function movavg(t, x, tau) {
  const n = x.length, o = new Float64Array(n);
  let a = 0, b = 0, sum = 0;
  for (let i = 0; i < n; i++) {
    while (b < n && t[b] < t[i] + tau/2) { sum += x[b]; b++; }
    while (t[a] < t[i] - tau/2) { sum -= x[a]; a++; }
    o[i] = sum / Math.max(1, b - a);
  }
  return o;
}

function highpass(t, x, tau) {
  const m1 = movavg(t, x, tau);
  const m2 = movavg(t, m1, tau);            // triangular window => 1 - sinc^2
  const n = x.length, o = new Float64Array(n);
  for (let i = 0; i < n; i++) o[i] = x[i] - m2[i];
  return o;
}

function detrend(t, x) {
  const n = x.length;
  let st=0, sx=0, stt=0, stx=0;
  for (let i = 0; i < n; i++) { st+=t[i]; sx+=x[i]; stt+=t[i]*t[i]; stx+=t[i]*x[i]; }
  const den = n*stt - st*st, m = den ? (n*stx - st*sx)/den : 0, c = (sx - m*st)/n;
  const o = new Float64Array(n);
  let mu = 0;
  for (let i = 0; i < n; i++) { o[i] = x[i] - (m*t[i] + c); mu += o[i]; }
  mu /= n;
  let sd = 0;
  for (let i = 0; i < n; i++) { o[i] -= mu; sd += o[i]*o[i]; }
  sd = Math.sqrt(sd/n) || 1;
  for (let i = 0; i < n; i++) o[i] /= sd;      // z-score so ROIs combine fairly
  return o;
}

// Non-uniform DFT on the real timestamps: frames arrive jittered, so summing
// x_n * exp(-j2*pi*f*t_n) avoids resampling, and frequency resolution is set by
// the grid rather than the record length.
function spectrum(t, x, grid) {
  const G = grid || NF, n = x.length, T = t[n-1] - t[0];
  const w = new Float64Array(n);
  for (let i = 0; i < n; i++) w[i] = 0.5 - 0.5*Math.cos(2*Math.PI*(t[i]-t[0])/T);
  const P = new Float64Array(G.length);
  for (let k = 0; k < G.length; k++) {
    const om = 2*Math.PI*G[k];
    let re = 0, im = 0;
    for (let i = 0; i < n; i++) {
      const a = om*t[i], v = w[i]*x[i];
      re += v*Math.cos(a); im -= v*Math.sin(a);
    }
    P[k] = re*re + im*im;
  }
  return P;
}

function bandSNR(P, grid, pk, bw) {
  let inb = 0, out = 0;
  for (let k = 0; k < P.length; k++)
    (Math.abs(grid[k] - grid[pk]) < bw ? inb += P[k] : out += P[k]);
  return 10*Math.log10(inb / (out || 1e-12));
}

function findPeaks(t, x) {
  let sd = 0; for (const v of x) sd += v*v;
  sd = Math.sqrt(sd/x.length) || 1;
  const th = 0.35*sd, minGap = 1/HI, idx = [];
  for (let i = 1; i < x.length-1; i++) {
    if (x[i] > x[i-1] && x[i] >= x[i+1] && x[i] > th) {
      const last = idx[idx.length-1];
      if (last !== undefined && t[i] - t[last] < minGap) {
        if (x[i] > x[last]) idx[idx.length-1] = i;
      } else idx.push(i);
    }
  }
  return idx;
}

class RPPG {
  constructor() {
    this.t = []; this.g = [[],[],[]];
    this.prior = 0; this.hist = []; this.disp = 0; this.dissent = 0;
  }

  push(tsec, per) {
    this.t.push(tsec);
    for (let i = 0; i < 3; i++) this.g[i].push(per[i][1]);   // green per ROI
    while (this.t.length && tsec - this.t[0] > 30) {
      this.t.shift(); for (let i = 0; i < 3; i++) this.g[i].shift();
    }
  }

  estimate() {
    const n = this.t.length;
    if (n < 24) return null;
    const tE = this.t[n-1];
    const span = tE - this.t[0];
    // Adaptive window: start estimating from MINWIN and grow to WIN as data
    // arrives. Waiting for a full 15 s before the first output was most of the
    // convergence delay; a short window is coarse (resolution = 1/T) but the
    // prior and the median filter clean it up while the window fills.
    const win = Math.min(WIN, Math.max(MINWIN, span));
    let i0 = 0; while (i0 < n && tE - this.t[i0] > win) i0++;
    const t = this.t.slice(i0), m = t.length;
    if (m < 24 || t[m-1] - t[0] < MINWIN*0.85) return null;

    const comb = new Float64Array(m);
    for (let r = 0; r < 3; r++) {
      const d = detrend(t, highpass(t, this.g[r].slice(i0), 3.0));
      for (let i = 0; i < m; i++) comb[i] += d[i]/3;
    }
    const P = spectrum(t, comb);

    // Heart rate cannot jump 80 BPM in half a second. A Gaussian prior around
    // the last confident estimate stops a momentary SNR dip throwing the
    // tracker onto an unrelated noise peak.
    let gmax = 0;
    for (let k = 1; k < P.length; k++) if (P[k] > P[gmax]) gmax = k;
    let pk = gmax;
    if (this.prior > 0) {
      const f0 = this.prior/60, sg = 0.20;
      let best = -1;
      for (let k = 0; k < P.length; k++) {
        // A weaker prior floor (0.5, was 0.25) still rejects a wild jump but
        // cannot entrench a wrong lock the way the old weighting did.
        const wgt = Math.exp(-0.5*((NF[k]-f0)/sg)**2);
        const v = P[k]*(0.5 + 0.5*wgt);
        if (v > best) { best = v; pk = k; }
      }
      // Escape hatch: if the unbiased maximum is decisively stronger for
      // several consecutive windows, the prior is wrong - jump to it.
      if (pk !== gmax && P[gmax] > 1.6*P[pk]) {
        if (++this.dissent >= 4) { pk = gmax; this.prior = 0; this.dissent = 0; }
      } else this.dissent = 0;
    }
    const snr = bandSNR(P, NF, pk, 0.15);
    const bpm = 60*NF[pk];
    if (snr > 1) this.prior = this.prior > 0 ? 0.85*this.prior + 0.15*bpm : bpm;

    this.hist.push(bpm); if (this.hist.length > 10) this.hist.shift();
    const srt = [...this.hist].sort((a,b)=>a-b);
    const med = srt[srt.length >> 1];
    const spread = srt[srt.length-1] - srt[0];

    // Displayed value: median first (rejects the odd noise peak outright),
    // then an exponential average so the number eases instead of stepping.
    // The rate adapts to confidence, so a strong signal still tracks quickly.
    const a = this.disp === 0 ? 1 : (snr > 2 ? 0.35 : snr > 0 ? 0.20 : 0.07);
    this.disp = (1-a)*this.disp + a*med;

    const good = snr > 0 && spread < 12 && this.hist.length >= 3;
    const settle = Math.min(1, span / WIN);        // 0..1, how full the window is

    return { bpm, med: this.disp, raw: bpm, medRaw: med, snr, spread, good,
             settle, win, sig: comb, t, P,
             peaks: snr > 1 ? findPeaks(t, comb) : [] };
  }
}
