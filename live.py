"""Live view of the streaming estimates from the phone."""
import json, time, sys, os, statistics as st

P = 'live.jsonl'
open(P, 'a').close()
f = open(P)
f.seek(0, os.SEEK_END)
hist = []
print('waiting for live data from the phone...  (Ctrl-C to stop)', flush=True)
while True:
    line = f.readline()
    if not line:
        time.sleep(0.3); continue
    try: d = json.loads(line)
    except Exception: continue
    hist.append(d); hist[:] = hist[-60:]
    conf = [x['bpm'] for x in hist if x['good']]
    med = st.median(conf) if conf else float('nan')
    bar = '#' * max(0, min(20, int((d['snr'] + 5) * 2)))
    rr   = f"{d['rr']:5.1f} br/m" if d.get('rr') else "   -- br/m"
    hrv  = f"RMSSD {d['rmssd']:5.1f}ms" if d.get('rmssd') else "RMSSD    --   "
    pi   = f"PI {d.get('pi',0):5.2f}%"
    print(f"{d['t']:8.1f}s  HR {d['bpm']:5.1f}  SNR {d['snr']:+5.1f} {bar:<16} "
          f"{rr}  {hrv}  {pi}  G={d['g']:3.0f} skin={d['skin']*100:3.0f}% "
          f"| stable HR {med:.1f}", flush=True)
