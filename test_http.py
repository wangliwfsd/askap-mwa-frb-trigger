#!/usr/bin/env python3
"""
HTTP monitor: prints every incoming GET request and its parameters.
Runs until Ctrl+C.

Usage:
    python3 test_http.py [port]   (default port: 8765)
"""

import http.server
import sys
import threading
import time
import urllib.parse

HTTP_HOST = "0.0.0.0"
HTTP_PORT = 8080


class LogHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        print(f"\n[HTTP] {self.client_address[0]} -> {parsed.path}")
        for k, v in params.items():
            print(f"  {k} = {v[0]}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else HTTP_PORT
    srv = http.server.HTTPServer((HTTP_HOST, port), LogHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[monitor] Listening on {HTTP_HOST}:{port}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[monitor] Stopped.")
        srv.shutdown()
