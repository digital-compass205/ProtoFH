"""Hand-rolled HTTP server on the shared selectors reactor.

Deliberately NOT ``http.server`` (which is either single-connection
blocking or threaded) -- this stays on the one reactor thread the rest
of jnxweb uses, per JNX_PLAN.md §0's no-asyncio/no-threading-I/O rule.
Request parsing is minimal: method + path + headers only, no query
string handling, no chunked/keep-alive support -- every non-WebSocket
response closes the connection after it's flushed (``Connection:
close``), which is fine for a low-traffic ops GUI.

Routes:
  GET /              -> the embedded HTML/JS/CSS page
  GET /tickers       -> JSON sorted ticker list
  GET /snap/<ticker> -> JSON full state incl. trades ring (404 if unknown)
  GET /stats         -> JSON global stats
  GET /ws            -> WebSocket upgrade (handed off to jnxweb.wsock)
"""
import json
import logging
import socket

from jnxweb import wsock

log = logging.getLogger("jnxweb.httpd")

_REASON = {
    200: "OK",
    101: "Switching Protocols",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
}

#: refuse to buffer an unbounded request; ours never need more than a
#: normal browser GET's headers.
_MAX_REQUEST_HEADER_BYTES = 16384


def _json_bytes(obj):
    return json.dumps(obj, sort_keys=True).encode("utf-8")


class HttpConnection(object):
    """One accepted TCP connection: parses exactly one request, responds,
    then either closes (default) or -- for a successful /ws upgrade --
    hands the raw socket to `WsHub.attach` once the 101 response is
    fully flushed."""

    def __init__(self, sock, addr, reactor, server):
        self.sock = sock
        self.sock.setblocking(False)
        self.addr = addr
        self.reactor = reactor
        self.server = server
        self.recv_buf = bytearray()
        self.send_buf = bytearray()
        self._after_flush = self._close
        self.reactor.register_read(self.sock, self._on_readable)

    # -- request read/parse ---------------------------------------------------

    def _on_readable(self):
        try:
            data = self.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self._close()
            return
        if not data:
            self._close()
            return
        self.recv_buf += data
        if len(self.recv_buf) > _MAX_REQUEST_HEADER_BYTES:
            self.reactor.unregister_read(self.sock)
            self._respond_json(400, {"error": "request too large"})
            return
        idx = self.recv_buf.find(b"\r\n\r\n")
        if idx == -1:
            return
        self.reactor.unregister_read(self.sock)
        self._handle_request(bytes(self.recv_buf[:idx]))

    def _handle_request(self, header_bytes):
        lines = header_bytes.split(b"\r\n")
        request_line = lines[0].decode("iso-8859-1", "replace")
        parts = request_line.split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            self._respond_json(400, {"error": "bad request"})
            return
        method, path, _version = parts
        headers = {}
        for line in lines[1:]:
            if b":" not in line:
                continue
            key, _, value = line.partition(b":")
            headers[key.decode("ascii", "replace").strip().lower()] = \
                value.decode("iso-8859-1", "replace").strip()
        if method != "GET":
            self._respond_json(405, {"error": "method not allowed"})
            return
        self.server.route(self, path, headers)

    # -- response helpers -------------------------------------------------------

    def respond_html(self, status, body_text):
        self._respond(status, body_text.encode("utf-8"),
                     content_type="text/html; charset=utf-8")

    def respond_json(self, status, obj):
        self._respond(status, _json_bytes(obj),
                     content_type="application/json")

    def _respond(self, status, body, content_type):
        reason = _REASON.get(status, "OK")
        head = (
            "HTTP/1.1 {} {}\r\n"
            "Content-Type: {}\r\n"
            "Content-Length: {}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).format(status, reason, content_type, len(body))
        self._after_flush = self._close
        self._enqueue(head.encode("iso-8859-1") + body)

    def upgrade_to_websocket(self, sec_websocket_key):
        accept = wsock.compute_accept_key(sec_websocket_key)
        head = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: {}\r\n"
            "\r\n"
        ).format(accept)
        self._after_flush = self._complete_ws_upgrade
        self._enqueue(head.encode("iso-8859-1"))

    def _complete_ws_upgrade(self):
        # Ownership of the socket moves to the WebSocket hub; don't close
        # it here, and don't touch it again after this call.
        self.server.hub.attach(self.sock, self.addr)

    def _enqueue(self, data):
        self.send_buf += data
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
            self._close()
            return
        del self.send_buf[:n]
        if not self.send_buf:
            self.reactor.unregister_write(self.sock)
            action, self._after_flush = self._after_flush, None
            if action is not None:
                action()

    def _close(self):
        self.reactor.unregister(self.sock)
        try:
            self.sock.close()
        except OSError:
            pass


class HttpServer(object):
    """Listening socket + router, driven by the shared reactor."""

    def __init__(self, reactor, state, hub, page_html,
                host="0.0.0.0", port=8080):
        self.reactor = reactor
        self.state = state
        self.hub = hub
        self.page_html = page_html
        self.host = host
        self.port = port

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(64)
        self.sock.setblocking(False)
        # Actual bound port -- useful for tests that bind to port 0 to get
        # a collision-safe ephemeral port.
        self.port = self.sock.getsockname()[1]
        self.reactor.register_read(self.sock, self._on_accept)

    def _on_accept(self):
        while True:
            try:
                conn, addr = self.sock.accept()
            except BlockingIOError:
                return
            except OSError as exc:
                log.warning("accept() failed: %s", exc)
                return
            HttpConnection(conn, addr, self.reactor, self)

    def route(self, conn, path, headers):
        if path == "/":
            conn.respond_html(200, self.page_html)
        elif path == "/tickers":
            conn.respond_json(200, self.state.ticker_list())
        elif path.startswith("/snap/"):
            ticker = path[len("/snap/"):]
            snap = self.state.snapshot(ticker)
            if snap is None:
                conn.respond_json(404, {"error": "unknown"})
            else:
                conn.respond_json(200, snap)
        elif path == "/stats":
            conn.respond_json(200, self.state.stats())
        elif path == "/ws":
            self._handle_ws_upgrade(conn, headers)
        else:
            conn.respond_json(404, {"error": "not found"})

    def _handle_ws_upgrade(self, conn, headers):
        upgrade_ok = headers.get("upgrade", "").lower() == "websocket"
        key = headers.get("sec-websocket-key")
        version_ok = headers.get("sec-websocket-version") == "13"
        if not (upgrade_ok and key and version_ok):
            conn.respond_json(400, {"error": "bad websocket upgrade"})
            return
        conn.upgrade_to_websocket(key)

    def close(self):
        self.reactor.unregister(self.sock)
        try:
            self.sock.close()
        except OSError:
            pass
