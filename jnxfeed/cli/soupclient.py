"""Minimal blocking SoupBinTCP client for the connectivity kit (T3.3).

Deliberately simple, linear code on plain blocking stdlib sockets with
timeouts — a diagnostic probe and a capture tool want to be obviously
correct, not fast. The production transport (non-blocking sockets on a
selectors reactor) is a separate later task (T4.0/T4.1); nothing here
is imported by it.

Responsibilities:
- TCP connect with a timeout, measuring connect latency.
- SoupBinTCP login (jnxfeed.soup.packets does the framing/padding).
- A ``next_packet()`` pump that maintains client heartbeats (send `R`
  after >1 s idle, plan section 3.4) and detects a dead peer after a
  configurable silence window (spec default 15 s) while waiting.
"""
import socket
import time

from jnxfeed.soup import packets as sp

#: Reject-code -> human meaning (plan section 3.4).
REJECT_MEANINGS = {
    sp.REJECT_NOT_AUTHORIZED: (
        "not authorized: bad username/password or username not valid "
        "on this TCP port (username-port pairs are fixed per assignment)"
    ),
    sp.REJECT_SESSION_UNAVAILABLE: "requested session is not available",
}

#: Send a client heartbeat after this much outbound silence (seconds).
HEARTBEAT_INTERVAL = 1.0

#: Assume the connection is dead after this much inbound silence (seconds).
DEFAULT_SILENCE_TIMEOUT = 15.0

_RECV_CHUNK = 65536


class SoupClientError(Exception):
    """Base class for all connectivity-kit client errors."""


class ConnectFailed(SoupClientError):
    """TCP connection could not be established."""


class LoginRejected(SoupClientError):
    """Server answered the login with a `J` packet."""

    def __init__(self, code):
        self.code = code
        self.meaning = REJECT_MEANINGS.get(code, "unknown reject code")
        super(LoginRejected, self).__init__(
            "login rejected: code {!r} ({})".format(code, self.meaning)
        )


class ConnectionLost(SoupClientError):
    """Peer closed the connection (or a socket error) mid-session."""


class PeerSilent(SoupClientError):
    """Nothing received for longer than the silence timeout — peer is dead."""


class WaitTimeout(SoupClientError):
    """The caller-supplied deadline expired before a packet arrived."""


class ProtocolError(SoupClientError):
    """Unexpected packet where a specific one was required."""


class SoupClient(object):
    """One blocking SoupBinTCP client connection."""

    def __init__(self, host, port, silence_timeout=DEFAULT_SILENCE_TIMEOUT):
        self.host = host
        self.port = port
        self.silence_timeout = silence_timeout
        self._sock = None
        self._frames = sp.FrameBuffer()
        self._pending = []
        self._last_sent = 0.0
        self._last_received = 0.0
        self.connect_latency = None

    # -- connection lifecycle ---------------------------------------------

    def connect(self, timeout=10.0):
        """TCP connect; records ``connect_latency`` in seconds."""
        start = time.monotonic()
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout)
        except OSError as exc:
            raise ConnectFailed(
                "cannot connect to {}:{}: {}".format(self.host, self.port, exc)
            )
        self.connect_latency = time.monotonic() - start
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        now = time.monotonic()
        self._last_sent = now
        self._last_received = now
        return self.connect_latency

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- sending ------------------------------------------------------------

    def _send(self, wire):
        if self._sock is None:
            raise ConnectionLost("not connected")
        try:
            self._sock.sendall(wire)
        except OSError as exc:
            raise ConnectionLost("send failed: {}".format(exc))
        self._last_sent = time.monotonic()

    def send_login(self, username, password, requested_session="", requested_seq=1):
        self._send(
            sp.encode(
                sp.LoginRequest(
                    username=username,
                    password=password,
                    requested_session=requested_session,
                    requested_sequence=requested_seq,
                )
            )
        )

    def send_heartbeat(self):
        self._send(sp.encode(sp.ClientHeartbeat()))

    def send_logout(self):
        self._send(sp.encode(sp.LogoutRequest()))

    # -- receiving -----------------------------------------------------------

    def next_packet(self, timeout=None):
        """Return the next decoded SoupBinTCP packet.

        Blocks up to ``timeout`` seconds (None = no caller deadline).
        While waiting, sends a client heartbeat whenever more than
        HEARTBEAT_INTERVAL has passed since our last send, and raises
        PeerSilent if nothing at all arrives for ``silence_timeout``.

        Raises WaitTimeout when the caller deadline expires,
        ConnectionLost on EOF/socket error.
        """
        if self._pending:
            return self._pending.pop(0)
        if self._sock is None:
            raise ConnectionLost("not connected")

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            now = time.monotonic()
            if now - self._last_sent >= HEARTBEAT_INTERVAL:
                self.send_heartbeat()
            if now - self._last_received >= self.silence_timeout:
                raise PeerSilent(
                    "no data from peer for {:.1f}s".format(now - self._last_received)
                )
            if deadline is not None and now >= deadline:
                raise WaitTimeout("no packet within {:.1f}s".format(timeout))

            # Wake up in time for whichever comes first: next heartbeat
            # due, silence cutoff, or the caller's deadline.
            wait = HEARTBEAT_INTERVAL - (now - self._last_sent)
            wait = min(wait, self.silence_timeout - (now - self._last_received))
            if deadline is not None:
                wait = min(wait, deadline - now)
            wait = max(wait, 0.05)

            self._sock.settimeout(wait)
            try:
                data = self._sock.recv(_RECV_CHUNK)
            except socket.timeout:
                continue
            except OSError as exc:
                raise ConnectionLost("recv failed: {}".format(exc))
            if not data:
                raise ConnectionLost("peer closed the connection")
            self._last_received = time.monotonic()
            self._pending.extend(self._frames.feed(data))
            if self._pending:
                return self._pending.pop(0)

    def login(self, username, password, requested_session="", requested_seq=1,
              timeout=10.0):
        """Send a Login Request and wait for the verdict.

        Returns the LoginAccepted packet on success; raises LoginRejected
        on a `J` answer (the server closes the socket after that),
        ProtocolError on anything else arriving first.
        """
        self.send_login(username, password, requested_session, requested_seq)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeout("no login response within {:.1f}s".format(timeout))
            pkt = self.next_packet(timeout=remaining)
            if isinstance(pkt, sp.LoginAccepted):
                return pkt
            if isinstance(pkt, sp.LoginRejected):
                raise LoginRejected(pkt.reject_code)
            if isinstance(pkt, (sp.ServerHeartbeat, sp.DebugPacket)):
                continue  # harmless before the verdict
            raise ProtocolError(
                "expected Login Accepted/Rejected, got {}".format(type(pkt).__name__)
            )

    def logout(self):
        """Best-effort clean logout: send `O` and close."""
        try:
            self.send_logout()
        except SoupClientError:
            pass
        self.close()
