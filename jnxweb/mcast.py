"""Record source for jnxweb: multicast UDP receiver + AF_UNIX test-feed.

Non-blocking, driven by ``jnxfeed.net.reactor.Reactor`` (the same
selectors-based single-threaded event loop the C++/Python prototype
uses elsewhere in this repo -- see ``jnxfeed/net/reactor.py``). Every
UPDATE record carries FULL per-ticker state (docs/wire_spec.md), so a
missed datagram self-heals on the next update for that ticker: this
module only needs to count losses, never recover them.

``--test-feed <path>`` (wired in ``jnxweb/__main__.py``) is an
alternative record source for tests and local tooling: instead of
joining the real multicast group, jnxweb binds an ``AF_UNIX``
``SOCK_DGRAM`` socket at ``<path>`` and a peer (e.g. a test harness)
sends complete wire-format records to it with
``socket.sendto(bytes, path)``. This lets tests inject canned records
deterministically without a real multicast group or the C++ feed
handler running. Exactly one datagram = exactly one record, same as
the real multicast path; ``McastReceiver`` does not care which kind of
socket it was handed.
"""
import logging
import os
import socket
import struct

from jnxweb import records

log = logging.getLogger("jnxweb.mcast")


def open_mcast_socket(group, port, iface):
    """Non-blocking UDP socket joined to `group`:`port` on `iface`.

    Mirrors tools/mcast_spy.py's socket setup (SO_REUSEADDR, best-effort
    large SO_RCVBUF, IP_ADD_MEMBERSHIP), but non-blocking instead of a
    recv timeout since this socket is driven by the reactor.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for size in (67108864, 16777216, 4194304):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
            break
        except OSError:
            continue
    sock.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group),
                       socket.inet_aton(iface or "0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    return sock


def open_test_feed_socket(path):
    """Non-blocking AF_UNIX SOCK_DGRAM socket bound at `path` (see module
    docstring) -- the --test-feed alternative record source."""
    try:
        os.unlink(path)
    except OSError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.setblocking(False)
    return sock


class McastReceiver(object):
    """Drains one datagram socket on the shared reactor, decoding each
    datagram as one record and applying valid UPDATEs to `state`.

    Any decode failure (bad magic/version/kind/length) or a non-UPDATE
    record kind (multicast only ever carries UPDATE records in this
    design) counts as a bad datagram in `state` and is otherwise
    ignored -- clients need loss stats only, never recovery.
    """

    def __init__(self, reactor, sock, state):
        self.reactor = reactor
        self.sock = sock
        self.state = state
        self.reactor.register_read(self.sock, self._on_readable)

    def _on_readable(self):
        # Drain every pending datagram before returning to the loop --
        # a burst should not leave datagrams queued in the kernel buffer
        # while we go do unrelated select() work.
        while True:
            try:
                data = self.sock.recv(65536)
            except BlockingIOError:
                return
            except OSError as exc:
                log.warning("mcast recv error: %s", exc)
                return
            self._handle_datagram(data)

    def _handle_datagram(self, data):
        try:
            rec = records.decode_record(data)
        except records.RecordError as exc:
            log.debug("bad datagram (%d bytes): %s", len(data), exc)
            self.state.record_bad()
            return
        if rec.get("kind") != records.KIND_UPDATE:
            log.debug("unexpected record kind on feed: %r", rec.get("kind"))
            self.state.record_bad()
            return
        self.state.apply_update(rec)

    def close(self):
        self.reactor.unregister(self.sock)
        try:
            self.sock.close()
        except OSError:
            pass
