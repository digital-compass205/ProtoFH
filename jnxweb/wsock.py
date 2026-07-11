"""Minimal RFC 6455 WebSocket SERVER -- handshake, framing, per-client hub.

No external libraries exist for this on the target (stdlib-only Python
3.6, no pip) so this is a hand-rolled, deliberately narrow
implementation: text frames only, no compression extensions, no
fragmentation support (a fragmented message is treated as a protocol
error and the connection is closed -- browsers never fragment small
JSON control messages like ``{"sub": "..."}``, and jnxweb never sends
messages large enough to need fragmenting).

Direction matters for masking (RFC 6455 §5.3): frames FROM the browser
TO the server MUST be masked; frames the server sends back MUST NOT
be. ``encode_frame``/``encode_text_frame`` never mask (server role);
``decode_frame`` requires the client bit to be set and rejects
unmasked client frames as a protocol error.
"""
import base64
import hashlib
import json
import logging
import struct
import time

log = logging.getLogger("jnxweb.wsock")

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: per-client outgoing buffer cap; a client that can't keep up gets
#: dropped (WARN logged) rather than let the loop block or grow memory
#: unboundedly (JNX_PLAN2.md F7 boundary conditions).
MAX_SEND_BUF = 256 * 1024

#: coalescing cap: at most 10 pushes/s per client, latest wins.
MIN_PUSH_INTERVAL = 0.1


def compute_accept_key(client_key):
    """Sec-WebSocket-Accept value for a given Sec-WebSocket-Key.

    RFC 6455 §1.3 example: compute_accept_key("dGhlIHNhbXBsZSBub25jZQ==")
    == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" (asserted by tests/unit/test_jnxweb.py).
    """
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class FrameError(Exception):
    """A client frame violated the (narrow) subset of RFC 6455 we accept."""


def encode_frame(opcode, payload):
    """Build one unmasked, final (FIN=1) server->client frame."""
    out = bytearray()
    out.append(0x80 | (opcode & 0x0F))
    length = len(payload)
    if length < 126:
        out.append(length)
    elif length < 65536:
        out.append(126)
        out += struct.pack(">H", length)
    else:
        out.append(127)
        out += struct.pack(">Q", length)
    out += payload
    return bytes(out)


def encode_text_frame(text):
    return encode_frame(OP_TEXT, text.encode("utf-8"))


def encode_close_frame(code=1000, reason=b""):
    if isinstance(reason, str):
        reason = reason.encode("utf-8")
    return encode_frame(OP_CLOSE, struct.pack(">H", code) + reason)


def decode_frame(buf):
    """Parse exactly one masked client frame from the front of `buf`.

    Returns ``(fin, opcode, payload, consumed)`` on a complete frame,
    ``None`` if `buf` doesn't yet hold a whole frame (caller should wait
    for more bytes), or raises FrameError on a protocol violation
    (nonzero RSV bits, an unmasked "client" frame). Handles all three
    length encodings (7-bit, 16-bit, 64-bit).
    """
    n = len(buf)
    if n < 2:
        return None
    b0 = buf[0]
    b1 = buf[1]
    if b0 & 0x70:
        raise FrameError("nonzero RSV bits")
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
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
    if not masked:
        raise FrameError("client frame must be masked")
    if n < offset + 4:
        return None
    mask_key = bytes(buf[offset:offset + 4])
    offset += 4
    if n < offset + length:
        return None
    raw = buf[offset:offset + length]
    payload = bytes(raw[i] ^ mask_key[i % 4] for i in range(length))
    return fin, opcode, payload, offset + length


class WebSocketClient(object):
    """One upgraded WebSocket connection, driven by the shared reactor.

    Owns the socket from the moment the HTTP layer completes the
    101 handshake and hands it over (see jnxweb/httpd.py). Tracks a
    single subscribed ticker at a time; `WsHub` pushes that ticker's
    snapshot on every update via `queue_update`, coalesced to at most
    10 pushes/s (latest payload wins -- earlier pending payloads are
    simply overwritten, never queued).
    """

    def __init__(self, sock, reactor, hub, addr=None):
        self.sock = sock
        self.sock.setblocking(False)
        self.reactor = reactor
        self.hub = hub
        self.addr = addr
        self.recv_buf = bytearray()
        self.send_buf = bytearray()
        self.subscribed = None
        self._pending_payload = None
        self._last_push = 0.0
        self._flush_timer = None
        self._closing = False
        self._torn_down = False
        self.reactor.register_read(self.sock, self._on_readable)

    # -- incoming -----------------------------------------------------------

    def _on_readable(self):
        try:
            data = self.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self._teardown()
            return
        if not data:
            self._teardown()
            return
        self.recv_buf += data
        while True:
            try:
                result = decode_frame(self.recv_buf)
            except FrameError as exc:
                log.warning("ws %s: protocol error, closing: %s",
                           self.addr, exc)
                self._teardown()
                return
            if result is None:
                return
            fin, opcode, payload, consumed = result
            del self.recv_buf[:consumed]
            if not fin:
                log.warning("ws %s: fragmented frame unsupported, closing",
                           self.addr)
                self._initiate_close(1003)
                return
            self._handle_frame(opcode, payload)
            if self._torn_down:
                return

    def _handle_frame(self, opcode, payload):
        if opcode == OP_TEXT:
            self._handle_text(payload)
        elif opcode == OP_CLOSE:
            self._enqueue(encode_close_frame(1000))
            self._closing = True
        elif opcode == OP_PING:
            self._enqueue(encode_frame(OP_PONG, payload))
        elif opcode == OP_PONG:
            pass
        else:
            log.warning("ws %s: unsupported opcode 0x%x, closing",
                       self.addr, opcode)
            self._initiate_close(1003)

    def _handle_text(self, payload):
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(msg, dict) or "sub" not in msg:
            return
        ticker = str(msg["sub"]).strip()
        self.subscribed = ticker
        self._pending_payload = None
        self._last_push = 0.0
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None
        snap = self.hub.lookup(ticker)
        if snap is None:
            self._send_json({"error": "unknown"})
        else:
            self._send_json(snap)

    # -- outgoing (coalesced updates) ----------------------------------------

    def queue_update(self, payload):
        """Latest-wins coalesced push, capped at 10/s (see module docstring)."""
        if self._torn_down or self._closing:
            return
        self._pending_payload = payload
        now = time.monotonic()
        elapsed = now - self._last_push
        if elapsed >= MIN_PUSH_INTERVAL:
            self._flush_pending()
        elif self._flush_timer is None:
            self._flush_timer = self.reactor.call_later(
                MIN_PUSH_INTERVAL - elapsed, self._on_flush_timer)

    def _on_flush_timer(self):
        self._flush_timer = None
        if self._pending_payload is not None:
            self._flush_pending()

    def _flush_pending(self):
        payload = self._pending_payload
        self._pending_payload = None
        self._last_push = time.monotonic()
        self._send_json(payload)

    def send_event(self, obj):
        """Immediate, uncoalesced send -- used for the restart banner."""
        if self._torn_down or self._closing:
            return
        self._send_json(obj)

    def _send_json(self, obj):
        text = json.dumps(obj, sort_keys=True)
        self._enqueue(encode_text_frame(text))

    def _initiate_close(self, code):
        self._enqueue(encode_close_frame(code))
        self._closing = True

    def _enqueue(self, frame_bytes):
        if self._torn_down:
            return
        if len(self.send_buf) + len(frame_bytes) > MAX_SEND_BUF:
            log.warning("ws %s: send buffer cap exceeded, dropping client",
                       self.addr)
            self._teardown()
            return
        self.send_buf += frame_bytes
        self.reactor.register_write(self.sock, self._on_writable)

    def _on_writable(self):
        if not self.send_buf:
            self.reactor.unregister_write(self.sock)
            return
        try:
            n = self.sock.send(bytes(self.send_buf))
        except BlockingIOError:
            return
        except OSError:
            self._teardown()
            return
        del self.send_buf[:n]
        if not self.send_buf:
            self.reactor.unregister_write(self.sock)
            if self._closing:
                self._teardown()

    def close(self):
        """Public teardown, e.g. for process shutdown."""
        self._teardown()

    def _teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None
        self.reactor.unregister(self.sock)
        try:
            self.sock.close()
        except OSError:
            pass
        self.hub.remove(self)


class WsHub(object):
    """Tracks connected WebSocketClients and fans out state changes.

    Bridge between `jnxweb.state.State`'s callbacks and the set of
    live browser connections: `on_ticker_update` pushes a ticker's new
    snapshot to every client currently subscribed to it; `on_restart`
    broadcasts a "feed restarted" event to everyone (epoch change --
    JNX_PLAN2.md F7 boundary conditions).
    """

    def __init__(self, reactor, state):
        self.reactor = reactor
        self.state = state
        self.clients = []

    def attach(self, sock, addr=None):
        client = WebSocketClient(sock, self.reactor, self, addr=addr)
        self.clients.append(client)
        return client

    def remove(self, client):
        try:
            self.clients.remove(client)
        except ValueError:
            pass

    def lookup(self, ticker):
        return self.state.snapshot(ticker)

    def on_ticker_update(self, ticker):
        snap = None
        for client in self.clients:
            if client.subscribed == ticker:
                if snap is None:
                    snap = self.state.snapshot(ticker)
                    if snap is None:
                        return
                client.queue_update(snap)

    def on_restart(self, epoch):
        event = {"event": "restarted", "epoch": epoch}
        for client in self.clients:
            client.send_event(event)
