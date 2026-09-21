"""Stream the contact-PPG reference from the ESP32 into the monitor.

The camera measurement and the contact sensor run at the same time, so the
dashboard can show both and compute error against a real reference instead of
against the system's own agreement with itself.
"""
import json, statistics as st, sys, time, urllib.request
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyACM0'
URL  = sys.argv[2] if len(sys.argv) > 2 else 'http://127.0.0.1:8081'

def open_port():
    while True:
        try:
            p = serial.Serial(PORT, 115200, timeout=1)
            time.sleep(0.4); p.reset_input_buffer()
            print(f'connected {PORT} -> {URL}/ref', flush=True)
            return p
        except Exception as e:
            print(f'waiting for {PORT}: {e}', flush=True)
            time.sleep(2)

s = open_port()
beats, last_post, n = [], 0.0, 0
while True:
    # USB CDC drops a read now and then; a transient must not end the session
    try:
        ln = s.readline().decode(errors='replace').strip()
    except Exception as e:
        print(f'serial error: {e} - reconnecting', flush=True)
        try: s.close()
        except Exception: pass
        time.sleep(1)
        s = open_port()
        continue
    if not ln or ln.startswith('#'):
        continue
    p = ln.split(',')
    if len(p) != 5:
        continue
    try:
        ms, bpm, beat = int(p[0]), float(p[3]), int(p[4])
    except ValueError:
        continue
    if beat:
        # a reconnect restarts millis(); a backwards jump means stale history
        if beats and ms < beats[-1]:
            beats.clear()
        beats.append(ms)
        del beats[:-25]

    now = time.time()
    if now - last_post < 1.0:
        continue
    last_post = now

    # Beat intervals over the recent window are far more robust than the
    # firmware's instantaneous value. After a USB reconnect the beat history is
    # short and the firmware value can be nonsense, so publish NOTHING rather
    # than a bad label - these values are training targets.
    ibi = [b - a for a, b in zip(beats, beats[1:]) if 300 < b - a < 2000]
    if len(ibi) < 4:
        print(f'holding: only {len(ibi)+1} beats in window', flush=True)
        continue
    ref = 60000 / st.median(ibi)
    if not (40 <= ref <= 180):
        print(f'rejecting implausible reference {ref:.0f} BPM', flush=True)
        continue
    # a wildly dispersed window means missed beats, not a real rhythm
    if st.pstdev(ibi) > 0.25 * st.median(ibi):
        print(f'rejecting unstable window (IBI sd {st.pstdev(ibi):.0f} ms)', flush=True)
        continue
    n += 1
    body = json.dumps({'bpm': round(ref, 1), 'ts': now, 'n': n,
                       'beats': len(ibi) + 1,
                       'sd': round(st.pstdev(ibi), 0) if len(ibi) > 2 else 0}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(URL + '/ref', body,
                                   {'Content-Type': 'application/json'}), timeout=3)
    except Exception:
        pass
    print(f'ref {ref:5.1f} BPM   ({len(ibi)+1} beats in window)', flush=True)
