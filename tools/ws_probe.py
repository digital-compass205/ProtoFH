#!/usr/bin/env python3
"""ws_probe.py -- raw-socket WebSocket CLIENT for verifying jnxweb.

Python 3.6-safe (must pass tools/py36check.py; this is a dev/ops tool
that may also run on the RHEL 8 target). No external libraries --
hand-rolled RFC 6455 handshake + framing, reusing jnxweb.wsock's frame
codec (mirrored here for the CLIENT side: client frames MUST be
masked, unlike the server side jnxweb.wsock implements).

Usage:
    python3 tools/ws_probe.py HOST:PORT TICKER [--frames N] [--timeout SECS]

Connects to ws://HOST:PORT/ws, performs the handshake, sends
{"sub": "TICKER"}, prints up to N received text frames (default 5,
one per line, raw JSON), then exits 0. Exits nonzero on handshake
failure or timeout before N frames arrive.
"""
import argparse
import base64
import hashlib
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def make_client_key():
    return base64.b64encode(os.urandom(16)).decode("ascii")


def expected_accept(key):
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_client_frame(opcode, payload):
    """Masked client->server frame (RFC 6455 §5.3 -- client frames MUST
    be masked)."""
    out = bytearray()
    out.append(0x80 | (opcode & 0x0F))
    length = len(payload)
    mask_key = os.urandom(4)
    if length < 126:
        out.append(0x80 | length)
    elif length < 65536:
        out.append(0x80 | 126)
        out += struct.pack(">H", length)
    else:
        out.append(0x80 | 127)
        out += struct.pack(">Q", length)
    out += mask_key
    out += bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(out)


def decode_server_frame(buf):
    """Parse one UNmasked server->client frame. Returns (fin, opcode,
    payload, consumed) or None if `buf` is incomplete."""
    n = len(buf)
    if n < 2:
        return None
    b0, b1 = buf[0], buf[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        if n < offset + 2:
            return None
        length = struct.unpack_from(">H", buf, offset)[0]
        offset += 2
    elif length == 127:
        if n < offset + 8:
            return None
        length = struct.unpack_from(">Q", buf, offset)[0]
        offset += 8
    if n < offset + length:
        return None
    payload = bytes(buf[offset:offset + length])
    return fin, opcode, payload, offset + length


def recv_line_headers(sock, deadline):
    """Read bytes from `sock` until the header terminator, returning
    (header_bytes, leftover_bytes)."""
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        if time.monotonic() > deadline:
            raise TimeoutError("timed out waiting for handshake response")
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("connection closed during handshake")
        buf += chunk
    idx = buf.find(b"\r\n\r\n")
    return bytes(buf[:idx]), bytes(buf[idx + 4:])


def handshake(sock, host, port, path, timeout):
    key = make_client_key()
    request = (
        "GET {} HTTP/1.1\r\n"
        "Host: {}:{}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: {}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).format(path, host, port, key)
    sock.sendall(request.encode("ascii"))
    header_bytes, leftover = recv_line_headers(sock, time.monotonic() + timeout)
    status_line = header_bytes.split(b"\r\n", 1)[0].decode("ascii", "replace")
    if " 101 " not in status_line:
        raise ConnectionError("handshake failed: {!r}".format(status_line))
    accept = None
    for line in header_bytes.split(b"\r\n")[1:]:
        if line.lower().startswith(b"sec-websocket-accept:"):
            accept = line.split(b":", 1)[1].strip().decode("ascii")
    if accept != expected_accept(key):
        raise ConnectionError("Sec-WebSocket-Accept mismatch")
    return leftover


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="raw-socket WebSocket client to probe jnxweb")
    parser.add_argument("hostport", help="HOST:PORT of the jnxweb HTTP server")
    parser.add_argument("ticker", help="ticker to subscribe to")
    parser.add_argument("--frames", type=int, default=5,
                        help="number of frames to print before exiting "
                             "(default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="overall timeout in seconds (default: %(default)s)")
    parser.add_argument("--path", default="/ws", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    host, _, port_str = args.hostport.rpartition(":")
    if not host or not port_str:
        print("expected HOST:PORT, got {!r}".format(args.hostport),
             file=sys.stderr)
        return 2
    port = int(port_str)

    deadline = time.monotonic() + args.timeout
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(args.timeout)
    try:
        sock.connect((host, port))
        leftover = handshake(sock, host, port, args.path, args.timeout)
    except (OSError, ConnectionError, TimeoutError) as exc:
        print("handshake error: {}".format(exc), file=sys.stderr)
        sock.close()
        return 1

    sub_payload = '{{"sub": "{}"}}'.format(args.ticker).encode("utf-8")
    sock.sendall(encode_client_frame(OP_TEXT, sub_payload))

    buf = bytearray(leftover)
    printed = 0
    try:
        while printed < args.frames:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("timed out waiting for frames "
                     "({} of {} printed)".format(printed, args.frames),
                     file=sys.stderr)
                return 1
            sock.settimeout(remaining)
            result = decode_server_frame(buf)
            if result is None:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    print("connection closed by server "
                         "({} of {} printed)".format(printed, args.frames),
                         file=sys.stderr)
                    return 1
                buf += chunk
                continue
            fin, opcode, payload, consumed = result
            del buf[:consumed]
            if opcode == OP_TEXT:
                print(payload.decode("utf-8", "replace"))
                printed += 1
            elif opcode == OP_PING:
                sock.sendall(encode_client_frame(OP_PONG, payload))
            elif opcode == OP_CLOSE:
                print("server closed the connection", file=sys.stderr)
                return 1
    finally:
        try:
            sock.sendall(encode_client_frame(OP_CLOSE, b""))
        except OSError:
            pass
        sock.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
