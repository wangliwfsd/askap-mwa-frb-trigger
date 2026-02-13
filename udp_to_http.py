#!/usr/bin/env python3
"""
UDP -> Queue -> HTTP trigger

Design:
- UDP receiver thread: receive, parse, enqueue (fast, never do HTTP here)
- HTTP worker thread(s): dequeue, build URL, send HTTP request(s)

One UDP message => one URL trigger.
"""

import argparse
import socket
import struct
import threading
import queue
import time
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlencode

import numpy as np
import requests

from astropy.time import Time
import os

@dataclass(frozen=True)
class UdpTask:
    """A parsed UDP message turned into a HTTP trigger task."""
    addr: Tuple[str, int]
    raw_text: str
    fields: Tuple[float, int, float, int, int, float, int, float]  # 8 columns


def is_multicast(ip: str) -> bool:
    first_octet = int(ip.split(".")[0])
    return 224 <= first_octet <= 239


def parse_hostport(hostport: str) -> Tuple[str, int]:
    host, port_s = hostport.split(":")
    return host, int(port_s)


def parse_candidate_8cols(text: str) -> Optional[Tuple[float, int, float, int, int, float, int, float]]:
    """
    Parse a single UDP line formatted like:
    "%0.2f %lu %0.4f %d %d %0.2f %d %0.9f\n"

    Returns a typed tuple of 8 values if valid, else None.
    """
    # Fast parsing: numpy fromstring handles spaces and newlines well
    arr = np.fromstring(text.strip(), sep=" ")
    if arr.size != 8:
        return None

    # Map types: float, ulong, float, int, int, float, int, float
    # Note: %lu can exceed 32-bit; Python int is unbounded.
    try:
        f1 = float(arr[0])
        u2 = int(arr[1])
        f3 = float(arr[2])
        i4 = int(arr[3])
        i5 = int(arr[4])
        f6 = float(arr[5])
        i7 = int(arr[6])
        f8 = float(arr[7])
    except Exception:
        return None

    return (f1, u2, f3, i4, i5, f6, i7, f8)


class UDPReceiver(threading.Thread):
    """
    Receive UDP packets and enqueue parsed tasks.
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

        # bind
        sock.bind((self.host, self.port))

        # multicast join if needed
        if self.join_multicast and is_multicast(self.host):
            print(f"[udp] Joining multicast group {self.host}")
            mreq = struct.pack("4sl", socket.inet_aton(self.host), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        # Timeout so we can check stop_event periodically (avoid hanging on recvfrom)
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

                # Enqueue: either drop when full, or block briefly
                try:
                    if self.drop_when_full:
                        self.out_queue.put_nowait(task)
                    else:
                        self.out_queue.put(task, timeout=0.5)
                except queue.Full:
                    if self.verbose:
                        print("[udp] Queue full, dropping packet.")
                    continue

        finally:
            self.close_socket()
            print("[udp] Receiver stopped.")


class HTTPTriggerWorker(threading.Thread):
    """
    Dequeue tasks and trigger HTTP requests.
    """
    def __init__(
        self,
        name: str,
        in_queue: "queue.Queue[UdpTask]",
        stop_event: threading.Event,
        *,
        base_url: str,
        timeout: Tuple[float, float] = (1.0, 3.0),  # (connect, read)
        max_retries: int = 2,
        retry_backoff_sec: float = 0.5,
        verbose: bool = False,
    ):
        super().__init__(daemon=True, name=name)
        self.in_queue = in_queue
        self.stop_event = stop_event
        self.base_url = base_url.rstrip("?")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec
        self.verbose = verbose
        self.session = requests.Session()

    def build_url(self, task: UdpTask) -> str:
        """
        One UDP => one URL
        Map 8 fields into query params. Adjust names as you like.
        """
        f1, u2, f3, i4, i5, f6, i7, f8 = task.fields

        params = {
            "f1": f"{f1:.2f}",
            "u2": str(u2),
            "f3": f"{f3:.4f}",
            "i4": str(i4),
            "i5": str(i5),
            "f6": f"{f6:.2f}",
            "i7": str(i7),
            "f8": f"{f8:.9f}",
            # Optional metadata
            "src_ip": task.addr[0],
            "src_port": str(task.addr[1]),
        }
        return f"{self.base_url}?{urlencode(params)}"

    def trigger(self, url: str) -> requests.Response:
        # You can change GET -> POST if needed
        return self.session.get(url, timeout=self.timeout)

    def run(self) -> None:
        print(f"[http] Worker {self.name} started.")
        try:
            while True:
                # Exit condition: stop requested AND queue drained
                if self.stop_event.is_set() and self.in_queue.empty():
                    break

                try:
                    task = self.in_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                url = self.build_url(task)

                ok = False
                last_err: Optional[Exception] = None

                for attempt in range(self.max_retries + 1):
                    try:
                        resp = self.trigger(url)
                        if self.verbose:
                            print(f"[http] {resp.status_code} {url}")
                        if 200 <= resp.status_code < 300:
                            ok = True
                            break
                        last_err = RuntimeError(f"HTTP {resp.status_code}")
                        break  # 非 2xx：按你的需求决定要不要 retry；这里先不重试
                    except Exception as e:
                        last_err = e

                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff_sec * (2 ** attempt))

                if not ok and self.verbose:
                    print(f"[http] Failed: {url} err={last_err!r}")

                self.in_queue.task_done()

        finally:
            self.session.close()
            print(f"[http] Worker {self.name} stopped.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hostport", help="host:port (e.g. 127.0.0.1:4900 or 224.1.1.1:4900)")
    ap.add_argument("--base-url", required=True, help="Base URL to trigger, e.g. http://127.0.0.1:8080/trigger")
    ap.add_argument("--workers", type=int, default=2, help="Number of HTTP worker threads")
    ap.add_argument("--queue-size", type=int, default=1000, help="Max queued UDP tasks")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-drop", action="store_true", help="Block briefly instead of dropping when queue is full")
    args = ap.parse_args()

    host, port = parse_hostport(args.hostport)

    q: "queue.Queue[UdpTask]" = queue.Queue(maxsize=args.queue_size)
    stop_event = threading.Event()

    receiver = UDPReceiver(
        host, port, q, stop_event,
        verbose=args.verbose,
        drop_when_full=not args.no_drop,
    )

    workers = [
        HTTPTriggerWorker(
            name=f"w{i+1}",
            in_queue=q,
            stop_event=stop_event,
            base_url=args.base_url,
            verbose=args.verbose,
        )
        for i in range(args.workers)
    ]

    receiver.start()
    for w in workers:
        w.start()

    try:
        # Main thread just waits; Ctrl+C triggers shutdown
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[main] Ctrl+C received, shutting down...")
        stop_event.set()

        # Wait for receiver to exit (it checks stop_event every timeout tick)
        receiver.join(timeout=3.0)

        # Wait for queued tasks to be processed (optional)
        q.join()

        # Wait for workers to stop
        for w in workers:
            w.join(timeout=3.0)

        print("[main] Done.")


if __name__ == "__main__":
    main()
