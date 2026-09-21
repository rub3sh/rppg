"""
rPPG - contactless heart-rate estimation from a webcam.
DSP mini project: spatial averaging -> resampling -> bandpass -> FFT peak.

  python rppg.py record --seconds 45     # capture, saves trace.npz
  python rppg.py analyse                 # process + produce report figures
"""
import argparse, time
import numpy as np
from scipy.signal import butter, filtfilt, detrend, freqz

FS   = 30.0          # Hz, uniform grid we resample onto
LO, HI = 0.8, 3.0    # Hz passband -> 48-180 BPM
ORDER  = 3


# ---------------------------------------------------------------- capture
def record(seconds=45, cam=0, out='trace.npz', gui=True):
    """Capture forehead+cheek mean RGB from the webcam.

    Three details matter for rPPG and are easy to get wrong:
      * auto white balance is disabled - AWB continuously re-normalises the
        colour channels, which is exactly the chromatic variation we measure;
      * the face box is frozen after an initial detection window, because a box
        that jumps every re-detection injects broadband steps into the signal;
      * the ROI covers forehead AND both cheeks - more skin pixels means the
        per-pixel sensor noise averages down as 1/sqrt(N) while the pulse,
        being coherent across pixels, does not.
    """
    import cv2
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    cap = cv2.VideoCapture(cam)
    # V4L2 control state persists on the device between programs, so put the
    # camera into a known mode every run rather than trusting what we inherit.
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)   # 3 = auto (leave AE on: we need the light)
    cap.set(cv2.CAP_PROP_AUTO_WB, 0)         # but AWB off: it cancels the pulse
    # 720p costs no frame rate on this sensor but gives ~4x the skin pixels,
    # and SNR from spatial averaging grows as sqrt(pixels).
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    for _ in range(30):
        cap.read()                           # let auto-exposure settle

    # --- lock onto the face: median of several detections, then freeze ---
    print('waiting for a face - LOOK AT THE CAMERA...', flush=True)
    found = []
    t0 = time.time()
    while len(found) < 12 and time.time() - t0 < 45:
        ok, f = cap.read()
        if not ok:
            continue
        faces = cascade.detectMultiScale(
            cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), 1.2, 5, minSize=(120, 120))
        if len(faces):
            found.append(max(faces, key=lambda b: b[2] * b[3]))
    if len(found) < 12:
        cap.release()
        raise SystemExit('no face detected in 45 s - face the camera squarely; the Haar '
                         'detector needs a roughly frontal view and fails if you look down')
    x, y, w, h = np.median(np.array(found), axis=0).astype(int)
    print(f'face locked at ({x},{y}) {w}x{h} - RECORDING, hold still', flush=True)

    def patches(frame):
        """forehead strip + left and right cheek patches"""
        return [frame[y+int(a*h):y+int(b*h), x+int(c*w):x+int(d*w)]
                for a, b, c, d in ((0.08, 0.30, 0.20, 0.80),    # forehead
                                   (0.50, 0.72, 0.06, 0.30),    # left cheek
                                   (0.50, 0.72, 0.70, 0.94))]   # right cheek

    ok, probe = cap.read()
    npix = sum(p.shape[0] * p.shape[1] for p in patches(probe))
    print(f'ROI is {npix} pixels -> sensor noise averages down ~{np.sqrt(npix):.0f}x')
    if npix < 15000:
        print(f'WARNING: small ROI. Your face is {w}x{h} px in a {probe.shape[1]}x'
              f'{probe.shape[0]} frame - SIT CLOSER so it fills the frame.')

    ts, rgb, t0 = [], [], time.time()
    while time.time() - t0 < seconds:
        ok, frame = cap.read()
        if not ok:
            break
        px = np.concatenate([p.reshape(-1, 3) for p in patches(frame) if p.size])
        b, g, r = px.mean(axis=0)                       # spatial average
        rgb.append((r, g, b))
        ts.append(time.time() - t0)
        if gui and len(ts) % 3 == 0:      # throttle: imshow costs frames
            for a, bb, c, d in ((0.08,0.30,0.20,0.80),(0.50,0.72,0.06,0.30),(0.50,0.72,0.70,0.94)):
                cv2.rectangle(frame, (x+int(c*w), y+int(a*h)),
                              (x+int(d*w), y+int(bb*h)), (0, 255, 0), 2)
            cv2.putText(frame, f'{seconds-(time.time()-t0):.0f}s  HOLD STILL',
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            try:
                cv2.imshow('rPPG', frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
            except cv2.error:
                gui = False

    cap.release()
    if gui:
        cv2.destroyAllWindows()
    ts, rgb = np.array(ts), np.array(rgb)
    np.savez(out, ts=ts, rgb=rgb)
    rate = len(ts) / ts[-1] if len(ts) > 1 else 0
    print(f'{len(ts)} frames, {ts[-1]:.1f}s, mean rate {rate:.2f} fps'
          f'  (jitter sd {np.diff(ts).std()*1000:.1f} ms) -> {out}')
    if rgb.mean() < 60:
        print(f'WARNING: dark capture (mean {rgb.mean():.0f}/255) - add light')
    if rate < 20:
        print(f'WARNING: {rate:.0f} fps - camera is using a long exposure; more light raises this')
    return ts, rgb


# ---------------------------------------------------------------- DSP
def resample(ts, rgb, fs=None):
    """Webcam frames arrive with jitter; put them on a uniform grid.

    The grid rate defaults to the *measured* mean frame rate (clamped to
    8-30 Hz) rather than a hard-coded 30: in dim light a webcam trades frame
    rate for exposure time and may deliver far fewer frames than it claims.
    Nyquist only needs fs > 2*HI = 6 Hz, so this stays valid.
    """
    if fs is None:
        fs = float(np.clip(len(ts) / (ts[-1] - ts[0]), 8.0, 30.0))
    tu = np.arange(ts[0], ts[-1], 1 / fs)
    r = np.stack([np.interp(tu, ts, rgb[:, k]) for k in range(3)], axis=1)
    return tu, r, fs


def green(rgb):
    return rgb[:, 1]


def chrom(rgb):
    """CHROM (de Haan & Jeanne 2013) - chrominance combination, motion robust."""
    m = rgb.mean(axis=0)
    m[np.abs(m) < 1e-9] = 1e-9
    Rn, Gn, Bn = (rgb / m).T
    X = 3 * Rn - 2 * Gn
    Y = 1.5 * Rn + Gn - 1.5 * Bn
    return X - (X.std() / Y.std()) * Y


def pos(rgb, fs=FS, win_s=1.6):
    """POS - Plane Orthogonal to Skin (Wang et al. 2017).

    Projects RGB onto a plane orthogonal to the skin-tone direction, so
    intensity changes from motion and illumination cancel while the
    blood-volume pulse survives. Applied per short window with overlap-add,
    which also removes slow drift for free.
    """
    n, L = len(rgb), max(8, int(win_s * fs))
    out = np.zeros(n)
    for i in range(0, n - L):
        w = rgb[i:i + L]
        m = w.mean(axis=0)
        m[m == 0] = 1e-9
        Cn = w / m                                   # temporal normalisation
        S1 = Cn[:, 1] - Cn[:, 2]                     # G - B
        S2 = Cn[:, 1] + Cn[:, 2] - 2 * Cn[:, 0]      # G + B - 2R
        h = S1 + (S1.std() / (S2.std() + 1e-12)) * S2
        out[i:i + L] += h - h.mean()                 # overlap-add
    return out


def bpm_harmonic(x, fs=FS, w2=0.5):
    """Peak pick that also credits the first harmonic.

    A heartbeat is not a pure tone: the pulse waveform has a strong second
    harmonic. Scoring each candidate f by P(f) + w2*P(2f) breaks ties between
    a true rate and a spurious peak that has no harmonic support.
    """
    f, P = spectrum(x, fs)
    m = (f >= LO) & (f <= HI)
    fm, Pm = f[m], P[m]
    score = Pm.copy()
    for i, fi in enumerate(fm):
        j = np.searchsorted(f, 2 * fi)
        if j < len(P):
            score[i] += w2 * P[j] / (P[m].max() + 1e-30) * Pm.max()
    return 60 * fm[np.argmax(score)]


def clean(x, fs=FS):
    x = detrend(x)                                        # remove DC + linear drift
    b, a = butter(ORDER, [LO, HI], btype='band', fs=fs)
    return filtfilt(b, a, x)                              # zero-phase


def spectrum(x, fs=FS, zp=8):
    """Hann-windowed, zero-padded periodogram."""
    n = len(x)
    X = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(n), n * zp)) ** 2
    f = np.fft.rfftfreq(n * zp, 1 / fs)
    return f, X


def bpm(x, fs=FS):
    f, X = spectrum(x, fs)
    m = (f >= LO) & (f <= HI)
    return 60 * f[m][np.argmax(X[m])]


def snr(x, fs=FS, bw=0.15):
    """Power near the spectral peak vs the rest of the passband, in dB.

    A genuine pulse concentrates power in a narrow peak (positive dB).
    A noise floor spreads it evenly, so the argmax is meaningless (negative dB).
    """
    f, X = spectrum(x, fs)
    m = (f >= LO) & (f <= HI)
    fm, Xm = f[m], X[m]
    sel = np.abs(fm - fm[np.argmax(Xm)]) < bw
    return 10 * np.log10(Xm[sel].sum() / Xm[~sel].sum())


def sliding(x, win_s=20, hop_s=1, fs=FS):
    W, H = int(win_s * fs), int(hop_s * fs)
    t = np.arange(0, len(x) - W, H) / fs + win_s / 2
    return t, np.array([bpm(x[i:i + W], fs) for i in range(0, len(x) - W, H)])


# ---------------------------------------------------------------- figures
def analyse(path='trace.npz'):
    import matplotlib.pyplot as plt
    d = np.load(path)
    dur = float(d['ts'][-1] - d['ts'][0]) if len(d['ts']) > 1 else 0.0
    if len(d['ts']) < 200 or dur < 20:
        print(f'TRACE TOO SHORT: {len(d["ts"])} samples over {dur:.1f}s.')
        print('Need >=20 s. The capture stops if the phone screen locks or the')
        print('browser tab goes to the background - keep the page in front.')
        return
    if not np.all(np.isfinite(d['rgb'])) or d['rgb'].mean() < 5 \
            or d['rgb'].std(axis=0).min() < 1e-9:
        print(f'DEGENERATE TRACE: mean RGB {d["rgb"].mean(axis=0).round(1)}, '
              f'std {d["rgb"].std(axis=0).round(3)}')
        print('The ROI recorded near-constant or black pixels - the skin mask')
        print('matched nothing. Reload the page for the fixed version.')
        return
    tu, rgb, fs = resample(d['ts'], d['rgb'])

    # Per-ROI selection: forehead, left cheek and right cheek are sampled
    # separately, so pick whichever carries the cleanest pulse rather than
    # averaging a good patch together with a bad one.
    if 'patches' in d.files:
        P = d['patches']
        names = ['forehead', 'left cheek', 'right cheek']
        best, cand = None, []
        for i, nm in enumerate(names):
            _, pr, _ = resample(d['ts'], P[:, 3*i:3*i+3], fs)
            for meth, sig in (('GREEN', green(pr)), ('CHROM', chrom(pr)), ('POS', pos(pr, fs))):
                x = clean(sig, fs)
                cand.append((snr(x, fs), f'{nm:11s} {meth:5s}', bpm(x, fs)))
        cand.sort(reverse=True)
        print('per-ROI ranking (best SNR first):')
        for sn, lab, bp in cand[:6]:
            print(f'   {lab}  {bp:5.1f} BPM   SNR {sn:+5.2f} dB')

        # Combine the regions: z-score each ROI's green signal and average.
        # The pulse is coherent across regions so it adds, while each region's
        # own noise is largely independent and partly cancels.
        zs = []
        for i in range(3):
            _, pr, _ = resample(d['ts'], P[:, 3*i:3*i+3], fs)
            x = clean(green(pr), fs)
            zs.append((x - x.mean()) / (x.std() + 1e-12))
        comb = np.mean(zs, axis=0)

        # The windowed MODE beats the mean here: the estimate distribution is
        # bimodal, and a mean between two peaks is a value the signal never took.
        wsec = 20 if len(comb) > 20 * fs else max(5, int(len(comb) / fs / 2))
        _, e = sliding(comb, wsec, hop_s=0.5, fs=fs)
        edges = np.arange(40, 121, 3)
        print(f'\nROI-COMBINED   {bpm(comb, fs):5.1f} BPM   SNR {snr(comb, fs):+5.2f} dB')
        if len(e):
            h, _ = np.histogram(e, bins=edges)
            mode = edges[np.argmax(h)] + 1.5
            print(f'window mode    {mode:5.1f} BPM   (median {np.median(e):.1f}, '
                  f'IQR {np.percentile(e,75)-np.percentile(e,25):.1f})')
        print()
    g_raw = green(rgb)
    g, c = clean(g_raw, fs), clean(chrom(rgb), fs)

    print(f'sampling grid  {fs:.1f} Hz  ({len(tu)/fs:.1f} s, Nyquist {fs/2:.1f} Hz)')
    print(f'brightness     R{rgb[:,0].mean():.0f} G{rgb[:,1].mean():.0f} '
          f'B{rgb[:,2].mean():.0f} /255')
    print(f'GREEN  {bpm(g, fs):6.1f} BPM   SNR {snr(g, fs):+5.2f} dB')
    print(f'CHROM  {bpm(c, fs):6.1f} BPM   SNR {snr(c, fs):+5.2f} dB')
    for w in (5, 10, 15, 20, 30):
        if len(g) > w * fs:
            _, e = sliding(g, w, fs=fs)
            print(f'  window {w:2d}s -> mean {e.mean():5.1f} BPM, sd {e.std():4.2f}'
                  f'   (resolution {60/w:.1f} BPM)')

    # verdict: is this a real lock or the argmax of a noise floor?
    ok = (snr(g, fs) > 3) and (abs(bpm(g, fs) - bpm(c, fs)) < 5)
    print('VERDICT: ' + ('pulse detected' if ok else
          'NO reliable pulse - need SNR > +3 dB and GREEN/CHROM agreeing within 5 BPM'))

    fig, ax = plt.subplots(2, 2, figsize=(12, 8))

    # 1. raw vs filtered
    ax[0,0].plot(tu, g_raw - g_raw.mean(), lw=.8, label='raw green (detrended)')
    ax[0,0].plot(tu, g * 6, lw=1.2, label='filtered x6')
    ax[0,0].set(xlabel='time (s)', ylabel='intensity', title='Spatial average: raw vs filtered')
    ax[0,0].legend(fontsize=8)

    # 2. per-channel spectra -> shows green wins
    for k, (name, col) in enumerate([('R','r'), ('G','g'), ('B','b')]):
        f, X = spectrum(clean(rgb[:, k], fs), fs)
        m = (f >= 0.5) & (f <= 3.5)
        ax[0,1].plot(60*f[m], X[m]/X[m].max(), col, lw=1, label=name)
    ax[0,1].axvspan(60*LO, 60*HI, color='k', alpha=.06)
    ax[0,1].axvline(bpm(g, fs), ls='--', c='k', lw=1, label=f'{bpm(g, fs):.1f} BPM')
    ax[0,1].set(xlabel='BPM', ylabel='normalised power', title='Spectrum per channel')
    ax[0,1].legend(fontsize=8)

    # 3. Butterworth response
    w, h = freqz(*butter(ORDER, [LO, HI], btype='band', fs=fs), fs=fs, worN=4096)
    ax[1,0].plot(w, 20*np.log10(abs(h) + 1e-12))
    ax[1,0].axvspan(LO, HI, color='g', alpha=.1)
    ax[1,0].set(xlim=(0, 5), ylim=(-80, 5), xlabel='Hz', ylabel='dB',
                title=f'Order-{ORDER} Butterworth {LO}-{HI} Hz')

    # 4. tracking, GREEN vs CHROM
    for sig, name in ((g, 'GREEN'), (c, 'CHROM')):
        if len(sig) > 20 * fs:
            t, e = sliding(sig, 20, fs=fs)
            ax[1,1].plot(t, e, marker='.', ms=3, label=name)
    ax[1,1].set(xlabel='time (s)', ylabel='BPM', title='20 s sliding-window estimate')
    ax[1,1].legend(fontsize=8)

    fig.tight_layout()
    fig.savefig('rppg_report.png', dpi=150)
    print('-> rppg_report.png')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['record', 'analyse'])
    p.add_argument('--seconds', type=int, default=45)
    p.add_argument('--cam', type=int, default=0)
    p.add_argument('--no-gui', action='store_true')
    a = p.parse_args()
    record(a.seconds, a.cam, gui=not a.no_gui) if a.mode == 'record' else analyse()
