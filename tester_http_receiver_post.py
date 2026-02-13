#!/usr/bin/env python3
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class Handler(BaseHTTPRequestHandler):
    server_version = "TestHTTPReceiverPOST/1.0"

    def _send_json(self, status: int, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        parsed = urlparse(self.path)

        if parsed.path != "/trigger":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        # Read body
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # Parse JSON (best effort)
        payload = None
        err = None
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception as e:
            err = repr(e)

        print("=" * 80)
        print(f"[{ts}] From {self.client_address[0]}:{self.client_address[1]}")
        print(f"Path: {parsed.path}")
        print("Headers (subset):")
        print(f"  Content-Type: {self.headers.get('Content-Type')}")
        print(f"  Content-Length: {content_length}")

        if err:
            print("Body (raw):")
            print(body.decode("utf-8", errors="replace"))
            print(f"JSON parse error: {err}")
            self._send_json(400, {"ok": False, "error": "invalid json", "detail": err})
            return

        print("JSON payload:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

        # Reply
        self._send_json(200, {"ok": True, "received": payload})

    def log_message(self, fmt, *args):
        return  # quiet


def main():
    host = "0.0.0.0"
    port = 8080
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"HTTP receiver listening on http://{host}:{port}/trigger (POST)")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
