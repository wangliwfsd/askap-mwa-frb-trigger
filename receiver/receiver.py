#!/usr/bin/env python3
"""
HTTP receiver for udp_to_triggerbuffer.py debug events.

Receives GET requests from --debug-url and logs all candidate fields
to a JSONL file and stdout.

Usage:
    python3 receiver.py [--port PORT] [--log LOG_FILE]

Default port : 8080
Default log  : data/candidates.jsonl
"""

import argparse
import csv
import http.server
import json
import os
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone


# Fields sent by send_debug_event() in udp_to_triggerbuffer.py
EXPECTED_FIELDS = [
    "sn", "tfile", "time_from_file",
    "ibc", "idt", "dm",
    "ibeam", "cand_mjd",
    "sender_ip", "sender_port",
]

CSV_HEADER = ["received_utc"] + EXPECTED_FIELDS

# Fields that indicate a real debug/trigger event
TRIGGER_FIELDS = {"sn", "dm", "ibeam", "cand_mjd", "sender_ip"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class CandidateHandler(http.server.BaseHTTPRequestHandler):

    # injected by main before server starts
    log_file: str = "data/candidates.jsonl"
    csv_file: str = "data/candidates.csv"
    access_log_file: str = "log/access.log"
    verbose: bool = True
    _csv_initialized: bool = False
    _lock: threading.Lock = threading.Lock()

    def do_GET(self):
        received = utc_now_iso()
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        # Always log the raw URL
        self._write_access_log(received, self.path)

        # Only parse and record if this looks like a real debug/trigger event
        if TRIGGER_FIELDS & set(params.keys()):
            record = {"received_utc": received}
            record.update({f: params.get(f, "") for f in EXPECTED_FIELDS})
            self._write_jsonl(record)
            self._write_csv(record)
            if self.verbose:
                self._print_record(record)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def _write_access_log(self, received: str, url: str) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.access_log_file)), exist_ok=True)
            with open(self.access_log_file, "a", encoding="utf-8") as f:
                f.write(f"{received} {self.client_address[0]} {url}\n")

    def _write_jsonl(self, record: dict) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.log_file)), exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def _write_csv(self, record: dict) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.csv_file)), exist_ok=True)
            write_header = not os.path.exists(self.csv_file) or os.path.getsize(self.csv_file) == 0
            with open(self.csv_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
                if write_header:
                    writer.writeheader()
                writer.writerow(record)

    def _print_record(self, record: dict) -> None:
        print(f"\n[{record['received_utc']}] candidate from {record.get('sender_ip')}:{record.get('sender_port')}")
        for field in ["sn", "dm", "ibeam", "cand_mjd", "tfile", "time_from_file", "ibc", "idt"]:
            print(f"  {field:>16s} = {record.get(field, '')}")

    def log_message(self, *_):
        pass  # suppress default access log

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            print(f"[scanner] {self.client_address[0]} reset connection (ignored)")

    def handle_error(self, request, client_address):
        import traceback, sys
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionResetError):
            print(f"[scanner] {client_address[0]} reset connection (ignored)")
        else:
            traceback.print_exc()


def main():
    ap = argparse.ArgumentParser(description="HTTP receiver for FRB candidate debug events")
    ap.add_argument("--port", type=int, default=8080, help="HTTP listen port (default: 8080)")
    ap.add_argument("--host", default="0.0.0.0", help="HTTP bind address (default: 0.0.0.0)")
    ap.add_argument("--log", default="data/candidates.jsonl",
                    help="JSONL log file path (default: data/candidates.jsonl)")
    ap.add_argument("--csv", default="data/candidates.csv",
                    help="CSV log file path (default: data/candidates.csv)")
    ap.add_argument("-q", "--quiet", action="store_true", help="Suppress per-event console output")
    args = ap.parse_args()

    # Resolve paths relative to this script's directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_file = args.log if os.path.isabs(args.log) else os.path.join(base_dir, args.log)
    csv_file = args.csv if os.path.isabs(args.csv) else os.path.join(base_dir, args.csv)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    os.makedirs(os.path.dirname(csv_file), exist_ok=True)

    # Resolve access log path
    access_log_file = os.path.join(base_dir, "log/access.log")
    os.makedirs(os.path.dirname(access_log_file), exist_ok=True)

    # Inject config into handler class
    CandidateHandler.log_file = log_file
    CandidateHandler.csv_file = csv_file
    CandidateHandler.access_log_file = access_log_file
    CandidateHandler.verbose = not args.quiet
    CandidateHandler._lock = threading.Lock()

    server = http.server.ThreadingHTTPServer((args.host, args.port), CandidateHandler)

    print(f"[receiver] Listening on {args.host}:{args.port}")
    print(f"[receiver] JSONL log  : {log_file}")
    print(f"[receiver] CSV  log   : {csv_file}")
    print(f"[receiver] Access log : {access_log_file}")
    print("[receiver] Ctrl+C to stop")

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[receiver] Shutting down...")
        server.shutdown()
        print("[receiver] Done.")


if __name__ == "__main__":
    main()
