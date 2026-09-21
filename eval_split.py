"""Train on resting data, test on elevated heart rate.

The only test that distinguishes a model that learned pulse-peak SHAPE from one
that memorised this subject's resting rate. If accuracy survives a 40 BPM shift
in the label distribution, the features are doing real work.
"""
import json, sys
import numpy as np
sys.argv = [sys.argv[0]]
exec(open('train.py').read().split("def main()")[0])   # reuse load/candidates/features

split_t = float(open('/tmp/split_t').read().strip())
rows = load('paired.jsonl')
rest = [r for r in rows if r['t'] <= split_t]
exer = [r for r in rows if r['t'] >  split_t]
print(f'rest: {len(rest)} samples   elevated: {len(exer)} samples')
if len(exer) < 60:
    print('Need >= 60 elevated samples. Keep collecting after the exercise.')
    raise SystemExit

for name, sub in (('rest', rest), ('elevated', exer)):
    r = np.array([x['ref'] for x in sub])
    print(f'  {name:9s} reference HR {r.mean():6.1f} +/- {r.std():.1f}  ({r.min():.0f}-{r.max():.0f})')
shift = np.mean([x['ref'] for x in exer]) - np.mean([x['ref'] for x in rest])
print(f'  label shift: {shift:+.1f} BPM')

Xtr, ytr, gtr, cbtr, rtr = build(rest)
Xte, yte, gte, cbte, rte = build(exer)

from sklearn.ensemble import GradientBoostingClassifier
m = GradientBoostingClassifier(n_estimators=200, max_depth=3,
                               learning_rate=0.06, random_state=0).fit(Xtr, ytr)
p = m.predict_proba(Xte)[:, 1]

base, mod = [], []
for gi in np.unique(gte):
    sel = gte == gi
    ref = rte[gi]['ref']
    mod.append(abs(cbte[sel][np.argmax(p[sel])] - ref))
    base.append(abs(rte[gi]['raw'] - ref))
base, mod = np.array(base), np.array(mod)

print(f'\nTRAINED ON REST, TESTED ON ELEVATED  ({len(base)} windows)')
print(f'  DSP baseline : MAE {base.mean():6.2f}  within 5 BPM {100*(base<=5).mean():5.1f}%')
print(f'  learned pick : MAE {mod.mean():6.2f}  within 5 BPM {100*(mod<=5).mean():5.1f}%')
print(f'  improvement  : {base.mean()-mod.mean():+.2f} BPM')
ceil = np.mean([any(abs(cbte[gte==gi] - rte[gi]['ref']) <= 5) for gi in np.unique(gte)])
print(f'  ceiling (true peak present at all): {100*ceil:.1f}%')
