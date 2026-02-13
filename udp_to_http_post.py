#!/usr/bin/env python3
"""
UDP -> Queue -> HTTP POST trigger (JSON)

One UDP message => one HTTP POST request.
"""

import argparse
import socket
import struct
import threading
import queue
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

import numpy as np
import requests


@dataclass(frozen=True)
class UdpTask:
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
    Parse:
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


class UDPReceiver(threading.Thread):
    """Receive UDP packets, parse, enqueue. Never do HTTP here."""
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


class HTTPPostWorker(threading.Thread):
    """Dequeue tasks and POST JSON to a HTTP endpoint."""
    def __init__(
        self,
        name: str,
        in_queue: "queue.Queue[UdpTask]",
        stop_event: threading.Event,
        *,
        endpoint_url: str,
        timeout: Tuple[float, float] = (1.0, 3.0),  # (connect, read)
        max_retries: int = 2,
        retry_backoff_sec: float = 0.5,
        verbose: bool = False,
        headers: Optional[Dict[str, str]] = None,
    ):
        super().__init__(daemon=True, name=name)
        self.in_queue = in_queue
        self.stop_event = stop_event
        self.endpoint_url = endpoint_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec
        self.verbose = verbose
        self.session = requests.Session()
        self.headers = headers or {}

    def build_json(self, task: UdpTask) -> Dict[str, Any]:
        sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd = task.fields
        return {
            "sn": sn,
            "tfile": tfile,
            "time_from_file": time_from_file,
            "ibc": ibc,
            "idt": idt,
            "dm": dm,
            "ibeam": ibeam,
            "cand_mjd": cand_mjd,
            "src_ip": task.addr[0],
            "src_port": task.addr[1],
            "raw": task.raw_text,  # 调试方便，不想要可删
        }

    def post_once(self, payload: Dict[str, Any]) -> requests.Response:
        # requests 会自动设置 Content-Type: application/json
        return self.session.post(
            self.endpoint_url,
            json=payload,
            headers=self.headers,
            timeout=self.timeout,
        )

    def run(self) -> None:
        print(f"[http] Worker {self.name} started.")
        try:
            while True:
                if self.stop_event.is_set() and self.in_queue.empty():
                    break

                try:
                    task = self.in_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                payload = self.build_json(task)

                ok = False
                last_err: Optional[Exception] = None
                last_status: Optional[int] = None

                for attempt in range(self.max_retries + 1):
                    try:
                        resp = self.post_once(payload)
                        last_status = resp.status_code
                        if 200 <= resp.status_code < 300:
                            ok = True
                            if self.verbose:
                                print(f"[http] OK {resp.status_code} POST {self.endpoint_url} Payload {payload}")
                            break
                        else:
                            # 你可以选择：非 2xx 也重试。这里先重试（常见于 503）
                            last_err = RuntimeError(f"HTTP {resp.status_code}")
                    except Exception as e:
                        last_err = e

                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff_sec * (2 ** attempt))

                if not ok and self.verbose:
                    print(f"[http] FAILED status={last_status} err={last_err!r} payload={payload}")

                self.in_queue.task_done()

        finally:
            self.session.close()
            print(f"[http] Worker {self.name} stopped.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hostport", help="host:port (e.g. 0.0.0.0:4900 or 224.1.1.1:4900)")
    ap.add_argument("--endpoint", required=True, help="HTTP POST endpoint, e.g. http://127.0.0.1:8080/trigger")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--queue-size", type=int, default=1000)
    ap.add_argument("--timeout-connect", type=float, default=1.0)
    ap.add_argument("--timeout-read", type=float, default=3.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-drop", action="store_true", help="Block briefly instead of dropping when queue is full")
    ap.add_argument("--auth-token", default="", help="Optional bearer token for Authorization header")
    args = ap.parse_args()

    host, port = parse_hostport(args.hostport)

    q: "queue.Queue[UdpTask]" = queue.Queue(maxsize=args.queue_size)
    stop_event = threading.Event()

    receiver = UDPReceiver(
        host, port, q, stop_event,
        verbose=args.verbose,
        drop_when_full=not args.no_drop,
    )

    headers = {}
    if args.auth_token:
        headers["Authorization"] = f"Bearer {args.auth_token}"

    workers = [
        HTTPPostWorker(
            name=f"w{i+1}",
            in_queue=q,
            stop_event=stop_event,
            endpoint_url=args.endpoint,
            timeout=(args.timeout_connect, args.timeout_read),
            max_retries=args.retries,
            verbose=args.verbose,
            headers=headers,
        )
        for i in range(args.workers)
    ]

    receiver.start()
    for w in workers:
        w.start()

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
