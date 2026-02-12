#!/usr/bin/env python3
"""
Simple UDP listener for unicast or multicast.
Prints received packets and optionally parses candidate format (8 columns).
"""

import socket
import struct
import argparse
import numpy as np
import sys


def is_multicast(ip):
    first_octet = int(ip.split('.')[0])
    return 224 <= first_octet <= 239


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("hostport", help="host:port (e.g. 127.0.0.1:4900 or 224.1.1.1:4900)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    host, port = args.hostport.split(":")
    port = int(port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    sock.bind((host, port))

    if is_multicast(host):
        print(f"Joining multicast group {host}")
        mreq = struct.pack("4sl", socket.inet_aton(host), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    print(f"Listening on {host}:{port}")
    print("Waiting for packets...\n")

    while True:
        data, addr = sock.recvfrom(4096)

        print("=" * 60)
        print(f"Received {len(data)} bytes from {addr}")

        text = data.decode("utf-8", errors="ignore").strip()
        print("Raw data:")
        print(text)

        # Try parsing as candidate array
        try:
            npdata = np.fromstring(text, sep=" ")
            if len(npdata) % 8 == 0 and len(npdata) > 0:
                npdata.shape = (-1, 8)
                print("\nParsed candidate array:")
                print(npdata)
        except Exception as e:
            if args.verbose:
                print("Parse error:", e)


if __name__ == "__main__":
    main()
