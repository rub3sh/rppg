"""Learn to pick the right spectral peak from the camera signal.

Why this formulation: with a few minutes of paired data there is nowhere near
enough to regress BPM from a raw waveform without memorising it. But the DSP
already produces a spectrum containing the correct answer -- the observed
failure was picking the WRONG peak out of it. That is a ranking problem over a
handful of candidates, with a handful of features each, and it is learnable from
minutes rather than hours of data.

Label: the contact PPG sensor. Input: the camera's own power spectrum.
"""
import json, sys
import numpy as np

LO, HI = 0.8, 3.0                      # the spectrum's frequency span, Hz
PATH   = sys.argv[1] if len(sys.argv) > 1 else 'paired.jsonl'


def load(path):
    rows = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get('spec') and d.get('ref', 0) > 30:
            rows.append(d)
    return rows


def bpm_axis(n):
    return 60.0 * np.linspace(LO, HI, n)


def candidates(spec, k=6):
    """Local maxima of the spectrum, strongest first."""
    s = np.asarray(spec, float)
    bpm = bpm_axis(len(s))
    idx = [i for i in range(1, len(s) - 1) if s[i] >= s[i-1] and s[i] > s[i+1]]
    idx.sort(key=lambda i: -s[i])
    out = []
    for i in idx:
        if all(abs(bpm[i] - bpm[j]) > 4 for j in out):
            out.append(i)
        if len(out) == k:
            break
    return out


def features(spec, i, rank, meta):
    """Describe one candidate peak well enough to judge it."""
    s = np.asarray(spec, float)
    bpm = bpm_axis(len(s))
    tot = s.sum() + 1e-9
    f = bpm[i] / 60.0

    # harmonic support: a real pulse has energy at 2f, a noise peak usually not
    j2 = int(np.argmin(np.abs(bpm / 60.0 - 2 * f))) if 2 * f <= HI else -1
    h2 = s[j2] / (s[i] + 1e-9) if j2 >= 0 else 0.0
    # subharmonic: is this peak itself the second harmonic of something lower?
    jh = int(np.argmin(np.abs(bpm / 60.0 - f / 2))) if f / 2 >= LO else -1
    sh = s[jh] / (s[i] + 1e-9) if jh >= 0 else 0.0

    lo = max(0, i - 6); hi = min(len(s), i + 7)
    local = s[lo:hi].sum() / tot                      # peak sharpness

    # The candidate's absolute rate is deliberately EXCLUDED. With one subject
    # at a near-constant heart rate it is a giveaway: the model would learn
    # "pick the peak nearest 87" and score brilliantly while generalising to
    # nothing. Only shape and quality features are allowed.
    return [
        rank, s[i], s[i] / (s.max() + 1e-9), local, h2, sh,
        float(meta.get('snr', 0)), float(meta.get('spread', 0)),
        float(meta.get('g', 0)) / 255.0, float(meta.get('skin', 0)),
        float(meta.get('fps', 0)) / 30.0,
        float(meta.get('npix', 0)) / 1e5,
    ]

FEAT_NAMES = ['rank', 'power', 'rel_power', 'sharpness', 'harm2',
              'subharm', 'snr', 'spread', 'green', 'skin', 'fps', 'npix']


def build(rows):
    X, y, groups, cand_bpm = [], [], [], []
    for gi, d in enumerate(rows):
        cs = candidates(d['spec'])
        if not cs:
            continue
        for r, i in enumerate(cs):
            X.append(features(d['spec'], i, r, d))
            # a candidate is "correct" if it lands within 5 BPM of the sensor
            y.append(1 if abs(bpm_axis(len(d['spec']))[i] - d['ref']) <= 5 else 0)
            groups.append(gi)
            cand_bpm.append(bpm_axis(len(d['spec']))[i])
    return (np.array(X, float), np.array(y), np.array(groups),
            np.array(cand_bpm), rows)


def main():
    rows = load(PATH)
    print(f'{len(rows)} paired samples in {PATH}')
    if len(rows) < 60:
        print('NOT ENOUGH DATA. Need >= 60 paired samples (about 2 minutes with')
        print('a finger on the sensor and your face to the camera). Collect more,')
        print('then run this again.')
        return

    X, y, g, cb, rows = build(rows)
    print(f'{len(X)} candidate peaks, {y.sum()} of them correct '
          f'({100*y.mean():.0f}% positive)')

    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import GroupKFold

    # Group by sample: candidates from one window must never be split across
    # train and test, or the model sees the answer.
    gkf = GroupKFold(n_splits=min(5, len(np.unique(g))))
    base_err, model_err, n = [], [], 0
    for tr, te in gkf.split(X, y, g):
        m = GradientBoostingClassifier(n_estimators=200, max_depth=3,
                                       learning_rate=0.06, random_state=0)
        m.fit(X[tr], y[tr])
        p = m.predict_proba(X[te])[:, 1]
        for gi in np.unique(g[te]):
            sel = g[te] == gi
            ref = rows[gi]['ref']
            model_err.append(abs(cb[te][sel][np.argmax(p[sel])] - ref))
            # baseline: what the DSP actually reported for this window
            base_err.append(abs(rows[gi]['raw'] - ref))
            n += 1

    base_err, model_err = np.array(base_err), np.array(model_err)
    print(f'\ncross-validated on {n} windows (grouped, no leakage)')
    print(f'  DSP baseline : MAE {base_err.mean():6.2f}  RMSE {np.sqrt((base_err**2).mean()):6.2f}'
          f'  within 5 BPM {100*(base_err<=5).mean():5.1f}%')
    print(f'  learned pick : MAE {model_err.mean():6.2f}  RMSE {np.sqrt((model_err**2).mean()):6.2f}'
          f'  within 5 BPM {100*(model_err<=5).mean():5.1f}%')
    gain = base_err.mean() - model_err.mean()
    print(f'  improvement  : {gain:+.2f} BPM MAE')

    m = GradientBoostingClassifier(n_estimators=200, max_depth=3,
                                   learning_rate=0.06, random_state=0).fit(X, y)
    import pickle
    pickle.dump(m, open('peak_model.pkl', 'wb'))
    print('\nsaved peak_model.pkl')
    order = np.argsort(m.feature_importances_)[::-1]
    print('feature importance:')
    for i in order[:6]:
        print(f'   {FEAT_NAMES[i]:10s} {m.feature_importances_[i]:.3f}')


if __name__ == '__main__':
    main()
