"""Async one-shot ``ORDERS <ticker>`` query against jnxdb's TCP query
port (cpp/db/query.h/.cpp).

jnxdb's query protocol is one command line in, a text reply terminated
by a lone "." line, over a connection jnxdb otherwise leaves open (same
framing ``tools/dbquery.py`` relies on) -- so completion is detected by
scanning for a trailing "\\n.\\n" (or a bare ".\\n" reply), not by the
peer closing. This queries it the same way the rest of jnxweb talks to
the network: non-blocking, driven by the shared reactor (JNX_PLAN.md §0
-- no threads doing I/O), so a slow or wedged jnxdb can only stall this
one on-demand query, never the WebSocket hub or the multicast receiver.

Reply parsing mirrors query.cpp's ORDERS branch exactly:
  - ``"ERR unknown\\n"`` -> ticker not known to jnxdb.
  - otherwise a header line (``"order_number side price qty_remaining
    type\\n"``) followed by zero or more rows, one resting order per
    line, already-formatted price (query.cpp's ``price_str``), then the
    terminating "." line (stripped before parsing).
"""
import logging
import os
import socket

log = logging.getLogger("jnxweb.dbquery")

DEFAULT_TIMEOUT = 3.0


class OrdersQuery(object):
    """One ``ORDERS <ticker>`` round-trip; calls ``on_done(rows, error)``
    exactly once from the reactor thread -- ``rows`` is a list of dicts
    on success (``None`` on error), ``error`` is a human string (``None``
    on success). An unknown ticker is reported as ``error="unknown"``,
    not as an empty ``rows`` list, so callers can tell "no resting
    orders" apart from "jnxdb doesn't know this ticker"."""

    def __init__(self, reactor, host, port, ticker, on_done,
                timeout=DEFAULT_TIMEOUT):
        self.reactor = reactor
        self.on_done = on_done
        self.recv_buf = bytearray()
        self.send_buf = bytearray("ORDERS {}\n".format(ticker).encode("ascii"))
        self.finished = False
        self.connecting = True
        self.timer = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        try:
            self.sock.connect((host, port))
        except BlockingIOError:
            pass
        except OSError as exc:
            self._finish(error=str(exc))
            return
        self.reactor.register_write(self.sock, self._on_writable)
        self.timer = reactor.call_later(timeout, self._on_timeout)

    def _on_timeout(self):
        self.timer = None
        self._finish(error="timed out contacting jnxdb")

    def _on_writable(self):
        if self.connecting:
            self.connecting = False
            err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err != 0:
                self._finish(error=os.strerror(err))
                return
        if self.send_buf:
            try:
                n = self.sock.send(bytes(self.send_buf))
            except BlockingIOError:
                return
            except OSError as exc:
                self._finish(error=str(exc))
                return
            del self.send_buf[:n]
            if self.send_buf:
                return
        self.reactor.unregister_write(self.sock)
        self.reactor.register_read(self.sock, self._on_readable)

    def _on_readable(self):
        try:
            data = self.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError as exc:
            self._finish(error=str(exc))
            return
        if not data:
            self._finish(error="jnxdb closed the connection before the "
                                "terminator line")
            return
        self.recv_buf += data
        if self.recv_buf.endswith(b"\n.\n") or self.recv_buf == b".\n":
            self._finish(text=bytes(self.recv_buf).decode("ascii", "replace"))

    def _finish(self, text=None, error=None):
        if self.finished:
            return
        self.finished = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.reactor.unregister(self.sock)
        try:
            self.sock.close()
        except OSError:
            pass
        if error is not None:
            self.on_done(None, error)
            return
        rows, parse_err = _parse_orders_reply(text)
        self.on_done(rows, parse_err)


def _parse_orders_reply(text):
    lines = text.splitlines()
    if lines and lines[-1] == ".":
        lines = lines[:-1]
    if not lines:
        return None, "empty reply from jnxdb"
    if lines[0].startswith("ERR"):
        return None, "unknown"
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            continue
        order_number, side, price, qty_remaining, order_type = parts
        rows.append({
            "order_number": order_number,
            "side": side,
            "price": price,
            "qty_remaining": int(qty_remaining),
            "order_type": order_type,
        })
    return rows, None
