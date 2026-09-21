"""Local HTTPS server: the phone captures rPPG traces, this saves them as trace.npz.

Camera access needs a secure context, so this serves over TLS with a self-signed
certificate. The phone warns once; accept it and continue.

Two details that matter, both learned the hard way:
  * the server must be threaded - browsers open speculative pre-connect sockets
    and a single-threaded server blocks on the first idle one, aborting
    every real request queued behind it;
  * the TLS handshake must happen in the worker thread, not on the listening
    socket, or an idle socket stalls accept() for the same reason.
"""
import http.server, json, os, socket, socketserver, ssl, sys, threading, time
import numpy as np

PORT = 8081
TLS_PORT = 8443
LIVE = []
STATE = {'now': None, 'trend': [], 'frame': None, 'wave': None, 'spec': None}
# WebRTC signalling mailbox. Media travels peer-to-peer; only these small
# SDP/ICE messages pass through the server.
SIG = {'offer': None, 'answer': None, 'ice': {'mobile': [], 'pc': []}, 'epoch': 0}
# Latest camera frame as raw JPEG bytes. Base64 inside JSON inflates a frame by
# 33% and forces a full parse on both ends; raw bytes on a dedicated endpoint
# let the fallback run at video-ish rates.
FRAME = {'data': None, 'n': 0}
# ROI geometry, posted at frame rate and tiny, so the monitor can draw the
# boxes and landmarks over WebRTC video as well as over JPEG frames.
GEO = {}
# Contact-PPG reference from the ESP32, for live accuracy comparison.
REF = {'bpm': 0, 'ts': 0, 'n': 0}
DATASET_PATH = 'paired.jsonl'


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        self.request.settimeout(20)
        super().setup()

    def _send(self, body, ctype='text/html; charset=utf-8', code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        # the page changes every few minutes during debugging; never let a
        # phone browser hand back a stale copy
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]        # allow ?v=N cache-busters
        routes = {'/': 'index.html', '/index.html': 'index.html',
                  '/mobile': 'mobile.html', '/pc': 'pc.html',
                  '/dsp.js': 'dsp.js'}
        if path == '/latest':
            return self._send(json.dumps(STATE).encode(), 'application/json')

        if path == '/geo':
            return self._send(json.dumps(GEO).encode(), 'application/json')

        if path == '/ref':
            return self._send(json.dumps(REF).encode(), 'application/json')

        if path == '/frame':
            if not FRAME['data']:
                return self._send(b'', 'image/jpeg', 204)
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(FRAME['data'])))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-N', str(FRAME['n']))
            self.end_headers()
            self.wfile.write(FRAME['data'])
            return

        if path == '/sig':
            q = dict(p.split('=', 1) for p in self.path.partition('?')[2].split('&') if '=' in p)
            role = q.get('role', 'pc')
            since = int(q.get('since', 0))
            other = 'mobile' if role == 'pc' else 'pc'
            out = {'epoch': SIG['epoch'],
                   'offer': SIG['offer'] if role == 'pc' else None,
                   'answer': SIG['answer'] if role == 'mobile' else None,
                   'ice': SIG['ice'][other][since:],
                   'n': len(SIG['ice'][other])}
            return self._send(json.dumps(out).encode(), 'application/json')
        f = routes.get(path)
        if not f or not os.path.exists(f):
            return self._send(b'not found', 'text/plain', 404)
        ctype = 'application/javascript' if f.endswith('.js') else 'text/html; charset=utf-8'
        self._send(open(f, 'rb').read(), ctype)

    def do_POST(self):
        p = self.path.split('?')[0]

        if p == '/live':
            # one JSON line per live estimate; append-only so it can be tailed
            d = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            frame = d.pop('frame', None)          # keep the JPEG out of the log
            with open('live.jsonl', 'a') as f:
                f.write(json.dumps(d) + '\n')
            LIVE.append(d)
            del LIVE[:-600]
            STATE['now'] = d
            STATE['trend'] = [[x['t'], x['bpm'], x['snr']] for x in LIVE[-240:]]
            if frame:
                STATE['frame'] = frame
            STATE['wave'] = d.get('wave')
            STATE['spec'] = d.get('spec')

            # Pair each camera estimate with the contact reference measured at
            # the same instant. This is the supervised dataset: the sensor is
            # the label, the camera spectrum is the input.
            if REF['bpm'] > 0 and (time.time() - REF['ts']) < 3:
                rec = {'t': d.get('t'), 'ref': REF['bpm'], 'ref_sd': REF.get('sd', 0),
                       'bpm': d.get('bpm'), 'raw': d.get('raw'), 'snr': d.get('snr'),
                       'spread': d.get('spread'), 'good': d.get('good'),
                       'g': d.get('g'), 'skin': d.get('skin'), 'fps': d.get('fps'),
                       'npix': d.get('npix'), 'spec': d.get('spec')}
                with open(DATASET_PATH, 'a') as fh:
                    fh.write(json.dumps(rec) + '\n')
            if len(LIVE) % 4 == 0:
                good = [x['bpm'] for x in LIVE[-20:] if x.get('snr', -99) > 0]
                med = sorted(good)[len(good)//2] if good else float('nan')
                print(f"live {d['bpm']:6.1f} BPM  SNR {d['snr']:+5.1f} dB  "
                      f"G={d['g']:3.0f}  skin={d['skin']*100:3.0f}%  "
                      f"fps={d['fps']:4.1f}  | median(conf) {med:.1f}", flush=True)
            return self._send(b'ok', 'text/plain')

        if p == '/ref':
            d = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            REF.update(d)
            STATE['ref'] = dict(REF)
            return self._send(b'ok', 'text/plain')

        if p == '/geo':
            d = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if d.get('mesh') is None:
                d.pop('mesh', None)          # keep the last mesh we were sent
            GEO.update(d)
            return self._send(b'ok', 'text/plain')

        if p == '/frame':
            n = int(self.headers['Content-Length'])
            FRAME['data'] = self.rfile.read(n)
            FRAME['n'] += 1
            return self._send(b'ok', 'text/plain')

        if p == '/sig':
            d = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            k = d.get('kind')
            if k == 'offer':
                SIG['offer'] = d['sdp']; SIG['answer'] = None
                SIG['ice'] = {'mobile': [], 'pc': []}; SIG['epoch'] += 1
                print('  webrtc: offer from phone', flush=True)
            elif k == 'answer':
                SIG['answer'] = d['sdp']
                print('  webrtc: answer from pc', flush=True)
            elif k == 'ice':
                SIG['ice'][d['role']].append(d['cand'])
            elif k == 'reset':
                SIG['offer'] = SIG['answer'] = None
                SIG['ice'] = {'mobile': [], 'pc': []}
            return self._send(b'ok', 'text/plain')

        if p != '/upload':
            return self._send(b'not found', 'text/plain', 404)
        d = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        ts, rgb = np.asarray(d['ts'], float), np.asarray(d['rgb'], float)
        extra = {}
        if d.get('patches'):
            extra['patches'] = np.asarray(d['patches'], float)   # (n, 3 patches x RGB)
        np.savez('trace.npz', ts=ts, rgb=rgb, **extra)
        np.savez('trace_phone.npz', ts=ts, rgb=rgb, **extra)
        rate = len(ts) / (ts[-1] - ts[0])
        msg = (f'saved {len(ts)} samples, {ts[-1]-ts[0]:.1f}s, {rate:.1f} fps, '
               f'jitter sd {np.diff(ts).std()*1000:.1f} ms, '
               f'RGB {rgb[:,0].mean():.0f}/{rgb[:,1].mean():.0f}/{rgb[:,2].mean():.0f}')
        print('\n*** ' + msg + '\n    -> run:  .venv/bin/python rppg.py analyse\n', flush=True)
        self._send((msg + '\nnow run  rppg.py analyse  on the laptop').encode(), 'text/plain')

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (socket.timeout, ConnectionResetError, OSError):
            self.close_connection = True


class Srv(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        pass          # idle pre-connect sockets that never handshake are normal


class HTLS(H):
    """Same handler, TLS wrapped in the worker thread.

    The camera needs a secure context, so the LAN path has to be HTTPS. The
    handshake must happen per-connection, not on the listening socket, or one
    idle browser pre-connect stalls accept() for everyone.
    """
    def setup(self):
        self.request.settimeout(20)
        self.request = CTX.wrap_socket(self.request, server_side=True)
        http.server.BaseHTTPRequestHandler.setup(self)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80)); return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


IP = lan_ip()
print(f'HTTP  :{PORT}   (behind the Cloudflare tunnel)', flush=True)

try:
    CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    CTX.load_cert_chain('cert.pem', 'key.pem')
    tls = Srv(('0.0.0.0', TLS_PORT), HTLS)
    threading.Thread(target=tls.serve_forever, daemon=True).start()
    print(f'HTTPS :{TLS_PORT}  ->  https://{IP}:{TLS_PORT}   '
          f'(same Wi-Fi: ~300x faster, and WebRTC connects P2P)', flush=True)
except Exception as e:
    print(f'HTTPS unavailable: {e}', flush=True)

Srv(('0.0.0.0', PORT), H).serve_forever()
