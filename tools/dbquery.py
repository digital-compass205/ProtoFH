#!/usr/bin/env python3
"""dbquery.py — query client for jnxdb's TCP line protocol (F4).

Python 3.6-safe (target box tool; must pass tools/py36check.py).

Usage:
    python3 tools/dbquery.py [--host H] [--port P] CMD [ARG...]

Examples:
    python3 tools/dbquery.py PING
    python3 tools/dbquery.py STATS
    python3 tools/dbquery.py GET 8306
    python3 tools/dbquery.py TABLE state

Sends the command words as one line, prints the response body (without
the terminating "." line). Exits 1 if the server answered with an ERR
line, 2 on connection problems.
"""
import argparse
import socket
import sys


def query(host, port, line, timeout=5.0):
    """Send one command line, return the response body lines (no '.')."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(line.encode("ascii") + b"\n")
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            # response ends with a lone "." line
            joined = b"".join(chunks)
            if joined.endswith(b"\n.\n") or joined == b".\n":
                break
    text = b"".join(chunks).decode("ascii", "replace")
    lines = text.splitlines()
    if lines and lines[-1] == ".":
        lines = lines[:-1]
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="query client for the jnxdb TCP line protocol"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=26401)
    parser.add_argument("words", nargs="+", metavar="CMD",
                        help="command and arguments, e.g. GET 8306")
    args = parser.parse_args(argv)

    line = " ".join(args.words)
    try:
        lines = query(args.host, args.port, line)
    except (OSError, socket.timeout) as exc:
        sys.stderr.write("dbquery: cannot query {}:{}: {}\n".format(
            args.host, args.port, exc))
        return 2

    for out_line in lines:
        print(out_line)
    if lines and lines[0].startswith("ERR"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
