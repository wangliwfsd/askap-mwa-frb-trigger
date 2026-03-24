#!/usr/bin/env python3
"""
UDP candidate stream -> TriggerBuffer GET trigger

- UDP receiver thread: recv, parse (8 columns), enqueue
- HTTP worker thread(s): dequeue, build TriggerBuffer URL, requests.get()

secure_key is read from environment variable: TRIGGER_SECURE_KEY
export TRIGGER_SECURE_KEY="IAmASecret"

past dump seconds is configurable via --past-seconds
"""

import argparse
import os
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlencode

import numpy as np
import requests


# -----------------------------
# Optional: astropy for time conversion (recommended)
# -----------------------------
_HAS_ASTROPY = False
try:
    from astropy.time import Time  # type: ignore

    _HAS_ASTROPY = True
except Exception:
    _HAS_ASTROPY = False


# -----------------------------
# Data model
# -----------------------------
@dataclass(frozen=True)
class UdpTask:
    addr: Tuple[str, int]
    raw_text: str
    # (sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd)
    fields: Tuple[float, int, float, int, int, float, int, float]


# -----------------------------
# Helpers
# -----------------------------
def is_multicast(ip: str) -> bool:
    first_octet = int(ip.split(".")[0])
    return 224 <= first_octet <= 239


def parse_hostport(hostport: str) -> Tuple[str, int]:
    host, port_s = hostport.split(":")
    return host, int(port_s)


def parse_candidate_8cols(text: str) -> Optional[Tuple[float, int, float, int, int, float, int, float]]:
    """
    Parse one line formatted like:
      "%0.2f %lu %0.4f %d %d %0.2f %d %0.9f\n"
    -> (sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd)
    """
    arr = np.fromstring(text.strip(), sep=" ")
    if arr.size != 8:
        return None
    try:
        sn = float(arr[0])
        tfile = int(arr[1])
        time_from_file = float(arr[2])
        ibc = int(arr[3])
        idt = int(arr[4])
        dm = float(arr[5])
        ibeam = int(arr[6])
        cand_mjd = float(arr[7])
    except Exception:
        return None
    return (sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd)


# Manual fallback if astropy not installed:
# MJD(UTC) -> Unix seconds (UTC): unix = (mjd - 40587) * 86400
# Unix -> GPS seconds: gps = unix - 315964800 + (GPS-UTC)
# GPS-UTC is 18s for recent years, but can change if leap seconds change.
GPS_UTC_LEAP_SECONDS_FALLBACK = 18
UNIX_GPS_EPOCH = 315964800
MJD_UNIX_EPOCH = 40587.0


def mjd_to_gps_seconds(mjd: float) -> int:
    """
    Convert MJD (assumed UTC) to GPS seconds.
    Prefer astropy (handles leap seconds properly), fallback to constant offset.
    """
    if _HAS_ASTROPY:
        # astropy Time.gps returns GPS seconds (float)
        return int(round(Time(mjd, format="mjd", scale="utc").gps))

    unix_utc = (mjd - MJD_UNIX_EPOCH) * 86400.0
    gps = unix_utc - UNIX_GPS_EPOCH + GPS_UTC_LEAP_SECONDS_FALLBACK
    return int(round(gps))


def get_secure_key_from_env(env_name: str = "TRIGGER_SECURE_KEY") -> str:
    key = os.environ.get(env_name, "")
    if not key:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return key


# -----------------------------
# UDP receiver thread
# -----------------------------
class UDPReceiver(threading.Thread):
    """
    Receive UDP packets, parse, enqueue.
    Never do HTTP here.
    """
    def __init__(
        self,
        host: str,
        port: int,
        out_queue: "queue.Queue[UdpTask]",
        stop_event: threading.Event,
        *,
        recv_bufsize: int = 4096,
        reuse_addr: bool = True,
        join_multicast: bool = True,
        verbose: bool = False,
        drop_when_full: bool = True,
    ):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.out_queue = out_queue
        self.stop_event = stop_event
        self.recv_bufsize = recv_bufsize
        self.reuse_addr = reuse_addr
        self.join_multicast = join_multicast
        self.verbose = verbose
        self.drop_when_full = drop_when_full
        self.sock: Optional[socket.socket] = None

    def open_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.reuse_addr:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        sock.bind((self.host, self.port))

        if self.join_multicast and is_multicast(self.host):
            print(f"[udp] Joining multicast group {self.host}")
            mreq = struct.pack("4sl", socket.inet_aton(self.host), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        # timeout so we can check stop_event and exit cleanly
        sock.settimeout(1.0)

        self.sock = sock
        print(f"[udp] Listening on {self.host}:{self.port}")

    def close_socket(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def run(self) -> None:
        self.open_socket()
        try:
            while not self.stop_event.is_set():
                try:
                    data, addr = self.sock.recvfrom(self.recv_bufsize)  # type: ignore[union-attr]
                except socket.timeout:
                    continue
                except OSError:
                    break

                text = data.decode("utf-8", errors="ignore").strip()
                fields = parse_candidate_8cols(text)
                if fields is None:
                    if self.verbose:
                        print(f"[udp] Unparsed from {addr}: {text!r}")
                    continue

                task = UdpTask(addr=addr, raw_text=text, fields=fields)

                try:
                    if self.drop_when_full:
                        self.out_queue.put_nowait(task)
                    else:
                        self.out_queue.put(task, timeout=0.5)
                except queue.Full:
                    if self.verbose:
                        print("[udp] Queue full, dropping packet.")
        finally:
            self.close_socket()
            print("[udp] Receiver stopped.")


# -----------------------------
# HTTP worker thread(s): TriggerBuffer GET
# -----------------------------
class TriggerBufferWorker(threading.Thread):
    """
    Dequeue tasks and trigger TriggerBuffer via HTTP GET.
    """
    def __init__(
        self,
        name: str,
        in_queue: "queue.Queue[UdpTask]",
        stop_event: threading.Event,
        *,
        endpoint: str,
        project_id: str,
        secure_key_env: str,
        past_seconds: int,
        obstime: int,
        pretty: bool,
        pretend: bool,
        # behavior knobs:
        use_start_time_zero: bool,
        min_trigger_interval_sec: float,
        timeout: Tuple[float, float] = (2.0, 10.0),  # connect, read
        max_retries: int = 2,
        retry_backoff_sec: float = 0.5,
        verbose: bool = False,
        debug_url: Optional[str] = None,
    ):
        super().__init__(daemon=True, name=name)
        self.in_queue = in_queue
        self.stop_event = stop_event
        self.endpoint = endpoint.rstrip("?")
        self.project_id = project_id
        self.secure_key_env = secure_key_env
        self.past_seconds = int(past_seconds)
        self.obstime = int(obstime)
        self.pretty = bool(pretty)
        self.pretend = bool(pretend)
        self.use_start_time_zero = bool(use_start_time_zero)
        self.min_trigger_interval_sec = float(min_trigger_interval_sec)

        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec
        self.verbose = verbose
        self.debug_url = debug_url.rstrip("?") if debug_url else None

        self.session = requests.Session()

        # simple rate-limit (avoid spamming triggerbuffer if many candidates arrive)
        self._lock = threading.Lock()
        self._last_trigger_monotonic: float = 0.0

    def build_triggerbuffer_url(self, task: UdpTask) -> str:
        secure_key = get_secure_key_from_env(self.secure_key_env)

        sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd = task.fields

        if self.use_start_time_zero:
            start_time = 0
        else:
            cand_gps = mjd_to_gps_seconds(cand_mjd)
            start_time = max(0, cand_gps - self.past_seconds)

        params = {
            "project_id": self.project_id,
            "secure_key": secure_key,
            "pretty": "true" if self.pretty else "false",
            "pretend": "true" if self.pretend else "false",
            "start_time": int(start_time),
            "obstime": int(self.obstime),
        }

        # Optional: include debug info (won't hurt most services, but if you prefer keep it clean, remove)
        # params.update({
        #     "sn": f"{sn:.2f}",
        #     "dm": f"{dm:.2f}",
        #     "ibeam": str(ibeam),
        # })

        return f"{self.endpoint}?{urlencode(params)}"

    def send_debug_event(self, task: UdpTask) -> None:
        """Send all parsed fields + metadata to the debug URL (fire-and-forget)."""
        if not self.debug_url:
            return
        sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd = task.fields
        params = {
            "sn": f"{sn:.2f}",
            "tfile": str(tfile),
            "time_from_file": f"{time_from_file:.4f}",
            "ibc": str(ibc),
            "idt": str(idt),
            "dm": f"{dm:.2f}",
            "ibeam": str(ibeam),
            "cand_mjd": f"{cand_mjd:.9f}",
            "sender_ip": task.addr[0],
            "sender_port": str(task.addr[1]),
        }
        debug_full_url = f"{self.debug_url}?{urlencode(params)}"
        try:
            resp = self.session.get(debug_full_url, timeout=self.timeout)
            if self.verbose:
                print(f"[debug] {resp.status_code} GET {debug_full_url}")
        except Exception as e:
            if self.verbose:
                print(f"[debug] Failed to send debug event: {e!r}")

    def _allowed_to_trigger_now(self) -> bool:
        if self.min_trigger_interval_sec <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            if now - self._last_trigger_monotonic >= self.min_trigger_interval_sec:
                self._last_trigger_monotonic = now
                return True
            return False

    def run(self) -> None:
        if not _HAS_ASTROPY:
            # Print once per worker (could be multiple lines if many workers)
            if self.verbose:
                print("[time] astropy not available; using fallback MJD->GPS conversion (fixed GPS-UTC offset). "
                      "Recommend: pip install astropy")

        print(f"[http] Worker {self.name} started.")
        try:
            while True:
                if self.stop_event.is_set() and self.in_queue.empty():
                    break

                try:
                    task = self.in_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # send all fields to debug URL if configured
                self.send_debug_event(task)

                # basic rate limit to avoid hammering trigger service
                if not self._allowed_to_trigger_now():
                    if self.verbose:
                        print(f"[http] Skipped (rate-limit): from {task.addr} raw={task.raw_text!r}")
                    self.in_queue.task_done()
                    continue

                url = self.build_triggerbuffer_url(task)

                ok = False
                last_status: Optional[int] = None
                last_err: Optional[Exception] = None

                for attempt in range(self.max_retries + 1):
                    try:
                        resp = self.session.get(url, timeout=self.timeout)
                        last_status = resp.status_code
                        if self.verbose:
                            print(f"[http] {resp.status_code} GET {url}")
                        if 200 <= resp.status_code < 300:
                            ok = True
                            break
                        else:
                            last_err = RuntimeError(f"HTTP {resp.status_code}")
                    except Exception as e:
                        last_err = e

                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff_sec * (2 ** attempt))

                if not ok and self.verbose:
                    print(f"[http] FAILED status={last_status} err={last_err!r}")

                self.in_queue.task_done()

        finally:
            self.session.close()
            print(f"[http] Worker {self.name} stopped.")


# -----------------------------
# Main
# -----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hostport", help="UDP bind host:port (e.g. 0.0.0.0:4900 or 224.1.1.1:4900)")

    ap.add_argument("--endpoint", default="http://mro.mwa128t.org/trigger/triggerbuffer",
                    help="TriggerBuffer endpoint base URL")

    ap.add_argument("--project-id", default="C001", help="MWA project_id (default: C001)")
    ap.add_argument("--secure-key-env", default="TRIGGER_SECURE_KEY",
                    help="Environment variable name holding secure_key")

    ap.add_argument("--past-seconds", type=int, default=120,
                    help="Seconds before candidate time to start buffer dump (used when --use-start-zero is false)")
    ap.add_argument("--obstime", type=int, default=600,
                    help="Seconds to keep capturing into the future (ignored if end_time used; we don't set end_time here)")

    ap.add_argument("--pretty", action="store_true", help="pretty=true")
    ap.add_argument("--pretend", action="store_true", help="pretend=true (dry-run behavior on the trigger service)")

    ap.add_argument("--use-start-zero", action="store_true",
                    help="If set, use start_time=0 (recommended for continued capturing triggers). "
                         "If not set, start_time=candidate_gps - past_seconds")

    ap.add_argument("--min-trigger-interval", type=float, default=2.0,
                    help="Minimum seconds between triggers to avoid spamming (0 disables). Default 2s")

    ap.add_argument("--workers", type=int, default=1, help="Number of HTTP worker threads")
    ap.add_argument("--queue-size", type=int, default=1000, help="Max queued UDP tasks")

    ap.add_argument("--timeout-connect", type=float, default=2.0)
    ap.add_argument("--timeout-read", type=float, default=10.0)
    ap.add_argument("--retries", type=int, default=2)

    ap.add_argument("--no-drop", action="store_true",
                    help="If set, block briefly when queue is full instead of dropping")

    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--debug-url", default=None,
                    help="If set, send ALL parsed UDP fields to this URL for every received candidate (debug mode)")
    args = ap.parse_args()

    # Validate secure key exists early (fail fast)
    _ = get_secure_key_from_env(args.secure_key_env)

    host, port = parse_hostport(args.hostport)

    q: "queue.Queue[UdpTask]" = queue.Queue(maxsize=args.queue_size)
    stop_event = threading.Event()

    receiver = UDPReceiver(
        host, port, q, stop_event,
        verbose=args.verbose,
        drop_when_full=not args.no_drop,
    )

    workers = [
        TriggerBufferWorker(
            name=f"w{i+1}",
            in_queue=q,
            stop_event=stop_event,
            endpoint=args.endpoint,
            project_id=args.project_id,
            secure_key_env=args.secure_key_env,
            past_seconds=args.past_seconds,
            obstime=args.obstime,
            pretty=args.pretty,
            pretend=args.pretend,
            use_start_time_zero=args.use_start_zero,
            min_trigger_interval_sec=args.min_trigger_interval,
            timeout=(args.timeout_connect, args.timeout_read),
            max_retries=args.retries,
            verbose=args.verbose,
            debug_url=args.debug_url,
        )
        for i in range(args.workers)
    ]

    receiver.start()
    for w in workers:
        w.start()

    print("[main] Running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[main] Ctrl+C received, shutting down...")
        stop_event.set()

        receiver.join(timeout=3.0)
        q.join()
        for w in workers:
            w.join(timeout=3.0)

        print("[main] Done.")


if __name__ == "__main__":
    main()
