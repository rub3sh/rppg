import http.server, socketserver

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 10
    def do_GET(self):
        print(f'*** REACHED by {self.client_address[0]} {self.path} ***', flush=True)
        b = (b'<meta name=viewport content="width=device-width,initial-scale=1">'
             b'<h1 style="font:600 8vw sans-serif;text-align:center;padding:15vh 4vw">REACHABLE</h1>'
             b'<p style="font:4.5vw sans-serif;text-align:center">Your phone can reach the laptop.<br><br>'
             b'Now open<br><b>https://10.30.206.123:8443</b>')
        self.send_response(200); self.send_header('Content-Type','text/html')
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

class S(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

S(('0.0.0.0', 8080), H).serve_forever()
