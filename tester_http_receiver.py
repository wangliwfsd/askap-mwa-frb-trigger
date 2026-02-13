#!/usr/bin/env python3
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


class Handler(BaseHTTPRequestHandler):
    server_version = "TestHTTPReceiver/1.0"

    def do_GET(self):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)

        # parse_qs gives lists; convert to single-value if len==1 for nicer output
        nice_params = {k: (v[0] if len(v) == 1 else v) for k, v in params.items()}

        print("=" * 80)
        print(f"[{ts}] From {self.client_address[0]}:{self.client_address[1]}")
        print(f"Path: {parsed.path}")
        print(f"Raw query: {parsed.query}")
        print("Parsed params:")
        print(json.dumps(nice_params, ensure_ascii=False, indent=2))

        # Only accept /trigger (you can relax this if you want)
        if parsed.path != "/trigger":
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "not found"}).encode("utf-8"))
            return

        # Response JSON
        resp = {
            "ok": True,
            "path": parsed.path,
            "params": nice_params,
            "from": f"{self.client_address[0]}:{self.client_address[1]}",
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        # Disable default noisy logging; comment this out if you want it.
        return


def main():
    host = "0.0.0.0"
    port = 8080
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"HTTP receiver listening on http://{host}:{port}/trigger")
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
