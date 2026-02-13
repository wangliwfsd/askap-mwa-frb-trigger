#!/usr/bin/env python3
"""
UDP listener (unicast or multicast) wrapped as a class.

Features:
- Bind to host:port
- Join multicast group automatically if host is multicast
- Receive loop with printable output
- Optional parsing of "candidate format" (8 columns) into numpy array
"""

import socket
import struct
import argparse
import numpy as np


class UDPListener:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        verbose: bool = False,
        reuse_addr: bool = True,
        recv_bufsize: int = 4096,
        join_multicast: bool = True,
    ):
        self.host = host
        self.port = int(port)
        self.verbose = verbose
        self.reuse_addr = reuse_addr
        self.recv_bufsize = recv_bufsize
        self.join_multicast = join_multicast

        self.sock: socket.socket | None = None

    @staticmethod
    def is_multicast(ip: str) -> bool:
        first_octet = int(ip.split(".")[0])
        return 224 <= first_octet <= 239

    def open(self) -> None:
        """Create, configure, bind, and (optionally) join multicast."""
        if self.sock is not None:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        if self.reuse_addr:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Bind
        sock.bind((self.host, self.port))

        # Join multicast group if needed
        if self.join_multicast and self.is_multicast(self.host):
            print(f"Joining multicast group {self.host}")
            mreq = struct.pack("4sl", socket.inet_aton(self.host), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        self.sock = sock

    def close(self) -> None:
        """Close socket."""
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def recv_once(self):
        """Receive one packet. Returns (data_bytes, addr_tuple)."""
        if self.sock is None:
            raise RuntimeError("Socket not opened. Call open() first.")
        return self.sock.recvfrom(self.recv_bufsize)

    @staticmethod
    def decode_text(data: bytes) -> str:
        return data.decode("utf-8", errors="ignore").strip()

    @staticmethod
    def try_parse_candidates(text: str):
        """
        Try parsing as float array; if total length is multiple of 8,
        reshape to (-1, 8) and return ndarray. Otherwise return None.
        """
        npdata = np.fromstring(text, sep=" ")
        if len(npdata) > 0 and (len(npdata) % 8 == 0):
            npdata.shape = (-1, 8)
            return npdata
        return None

    def handle_packet(self, data: bytes, addr) -> None:
        """Default packet handler: print raw and parsed candidate array if available."""
        print("=" * 60)
        print(f"Received {len(data)} bytes from {addr}")

        text = self.decode_text(data)
        print("Raw data:")
        print(text)

        try:
            parsed = self.try_parse_candidates(text)
            if parsed is not None:
                print("\nParsed candidate array:")
                print(parsed)
        except Exception as e:
            if self.verbose:
                print("Parse error:", e)

    def serve_forever(self) -> None:
        """Blocking receive loop."""
        self.open()
        print(f"Listening on {self.host}:{self.port}")
        print("Waiting for packets...\n")

        try:
            while True:
                data, addr = self.recv_once()
                self.handle_packet(data, addr)
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        finally:
            self.close()
            print("Socket closed")


def parse_hostport(hostport: str):
    host, port_s = hostport.split(":")
    return host, int(port_s)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("hostport", help="host:port (e.g. 127.0.0.1:4900 or 224.1.1.1:4900)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    host, port = parse_hostport(args.hostport)
    listener = UDPListener(host, port, verbose=args.verbose)
    listener.serve_forever()


if __name__ == "__main__":
    main()
