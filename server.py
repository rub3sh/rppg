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
import http.server, json, socket, socketserver, ssl, sys
import numpy as np

PORT = 8443
CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
CTX.load_cert_chain('cert.pem', 'key.pem')


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        # wrap here, inside the worker thread, so a stalled handshake
        # only ties up this one connection
        self.request.settimeout(20)
        self.request = CTX.wrap_socket(self.request, server_side=True)
        super().setup()

    def _send(self, body, ctype='text/html; charset=utf-8', code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            print(f'  page served to {self.client_address[0]}', flush=True)
            self._send(open('index.html', 'rb').read())
        else:
            self._send(b'not found', 'text/plain', 404)

    def do_POST(self):
        if self.path != '/upload':
            return self._send(b'not found', 'text/plain', 404)
        d = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        ts, rgb = np.asarray(d['ts'], float), np.asarray(d['rgb'], float)
        np.savez('trace.npz', ts=ts, rgb=rgb)
        np.savez('trace_phone.npz', ts=ts, rgb=rgb)
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
        except (ssl.SSLError, socket.timeout, ConnectionResetError, OSError):
            self.close_connection = True


class Srv(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        pass          # idle pre-connect sockets that never handshake are normal


ip = sys.argv[1] if len(sys.argv) > 1 else '192.168.29.188'
print(f'open on your phone (same Wi-Fi):  https://{ip}:{PORT}')
print('self-signed certificate: tap Advanced -> Proceed', flush=True)
Srv(('0.0.0.0', PORT), H).serve_forever()
