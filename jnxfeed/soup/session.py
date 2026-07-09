"""Sans-I/O SoupBinTCP client session state machine (JNX_PLAN.md T4.1).

``SoupSession`` knows nothing about sockets or the reactor: it is fed raw
bytes as they arrive (``on_bytes``) and a periodic clock tick
(``on_tick``), and produces output as plain bytes to be written to the
wire (``pending_output``). All connection lifecycle events (login
accepted/rejected, a sequenced message, end of session, peer silence) are
reported through optional callbacks supplied at construction.

Sequence numbering: the server's Login Accepted carries the sequence
number of the *next* message it will send; from then on the session
counts sequence numbers itself as each Sequenced Data packet arrives, so
callers never have to trust (or re-derive) a sequence carried on the
wire per-message (plan section 3.4 -- Soup itself carries no per-message
seq, only the client's own count).

Reconnect-and-resume: the session keeps the last known ``session_id``
and ``next_seq`` across a reconnect. Call :meth:`reset` after a TCP
disconnect (before the new socket is up) and :meth:`start` again once
reconnected; by default it logs back in with the maintained session id
and next expected sequence, so no message is lost or duplicated. Callers
that need an explicit resume point (e.g. GLIMPSE-driven bootstrap) may
pass ``requested_session``/``requested_seq`` to :meth:`start` directly.
"""
from jnxfeed.soup import packets as sp

#: Send a client heartbeat after this much outbound silence (seconds).
HEARTBEAT_INTERVAL = 1.0

#: Report the peer as silent after this much inbound silence (seconds).
DEFAULT_SILENCE_TIMEOUT = 15.0

# -- states -------------------------------------------------------------------

STATE_CONNECTED = "CONNECTED"
STATE_LOGIN_SENT = "LOGIN_SENT"
STATE_LIVE = "LIVE"
STATE_ENDED = "ENDED"
STATE_FAILED = "FAILED"


def _noop(*args, **kwargs):
    pass


class SoupSession(object):
    """One logical SoupBinTCP client session, independent of any socket.

    Constructor callbacks (all optional, default no-op):
    - ``on_login_accepted(session_id, next_seq)``
    - ``on_login_rejected(reject_code)``
    - ``on_message(seq, payload_bytes)`` -- one call per Sequenced Data
    - ``on_end_of_session()``
    - ``on_peer_silent()`` -- more than ``silence_timeout`` since the last
      byte was received; the session does NOT reconnect itself, it only
      reports this so the caller (the reactor-driven connector) can.
    """

    def __init__(self, username, password,
                 requested_session="", requested_seq=1,
                 on_login_accepted=None, on_login_rejected=None,
                 on_message=None, on_end_of_session=None, on_peer_silent=None,
                 heartbeat_interval=HEARTBEAT_INTERVAL,
                 silence_timeout=DEFAULT_SILENCE_TIMEOUT):
        self.username = username
        self.password = password
        self.heartbeat_interval = heartbeat_interval
        self.silence_timeout = silence_timeout

        self.on_login_accepted = on_login_accepted or _noop
        self.on_login_rejected = on_login_rejected or _noop
        self.on_message = on_message or _noop
        self.on_end_of_session = on_end_of_session or _noop
        self.on_peer_silent = on_peer_silent or _noop

        # Resume point: maintained across reconnects. Starts at whatever
        # the caller supplied (blank/1 for a brand new session).
        self.session_id = requested_session
        self.next_seq = requested_seq

        self.state = STATE_CONNECTED
        self.reject_code = None

        self._frames = sp.FrameBuffer()
        self._out = bytearray()
        self._last_sent = None
        self._last_received = None

    # -- lifecycle -------------------------------------------------------------

    def reset(self):
        """Prepare for reuse on a fresh TCP connection. Keeps the resume
        point (session_id/next_seq) but clears framing/output state."""
        self._frames = sp.FrameBuffer()
        self._out = bytearray()
        self.state = STATE_CONNECTED
        self.reject_code = None
        self._last_sent = None
        self._last_received = None

    def start(self, now, requested_session=None, requested_seq=None):
        """Queue a Login Request. Call once the TCP connection is up.

        Defaults to the maintained resume point; pass explicit
        ``requested_session``/``requested_seq`` to override (e.g. a fresh
        full-replay login, or a GLIMPSE-derived seq).
        """
        if self.state != STATE_CONNECTED:
            raise RuntimeError(
                "start() requires state CONNECTED, got {}".format(self.state)
            )
        if requested_session is not None:
            self.session_id = requested_session
        if requested_seq is not None:
            self.next_seq = requested_seq

        self._out += sp.encode(sp.LoginRequest(
            username=self.username,
            password=self.password,
            requested_session=self.session_id,
            requested_sequence=self.next_seq,
        ))
        self._last_sent = now
        self.state = STATE_LOGIN_SENT

    def logout(self, now=None):
        """Queue a Logout Request (clean 'O' before closing the socket)."""
        self._out += sp.encode(sp.LogoutRequest())
        if now is not None:
            self._last_sent = now

    # -- input -------------------------------------------------------------------

    def on_bytes(self, data, now):
        """Feed bytes received from the wire."""
        self._last_received = now
        for pkt in self._frames.feed(data):
            self._handle_packet(pkt, now)

    def _handle_packet(self, pkt, now):
        if isinstance(pkt, sp.LoginAccepted):
            self.session_id = pkt.session
            self.next_seq = pkt.sequence
            self.state = STATE_LIVE
            self.on_login_accepted(self.session_id, self.next_seq)
        elif isinstance(pkt, sp.LoginRejected):
            self.state = STATE_FAILED
            self.reject_code = pkt.reject_code
            self.on_login_rejected(pkt.reject_code)
        elif isinstance(pkt, sp.SequencedData):
            seq = self.next_seq
            self.next_seq += 1
            self.on_message(seq, pkt.message)
        elif isinstance(pkt, sp.ServerHeartbeat):
            pass  # any inbound byte already refreshed _last_received
        elif isinstance(pkt, sp.EndOfSession):
            self.state = STATE_ENDED
            self.on_end_of_session()
        elif isinstance(pkt, sp.DebugPacket):
            pass
        # Client->server packet types are never sent by a well-behaved
        # server; silently ignore anything else rather than raise, since a
        # protocol violation here shouldn't crash the reactor.

    def on_tick(self, now):
        """Periodic clock input. Emits a client heartbeat into the output
        buffer after ``heartbeat_interval`` of outbound silence, and fires
        ``on_peer_silent`` after ``silence_timeout`` of inbound silence."""
        if self.state not in (STATE_LOGIN_SENT, STATE_LIVE):
            return
        if (self._last_received is not None and
                now - self._last_received >= self.silence_timeout):
            self.on_peer_silent()
            return
        if (self._last_sent is not None and
                now - self._last_sent >= self.heartbeat_interval):
            self._out += sp.encode(sp.ClientHeartbeat())
            self._last_sent = now

    # -- output -------------------------------------------------------------------

    def pending_output(self):
        """Return and clear buffered bytes to write to the wire."""
        if not self._out:
            return b""
        data = bytes(self._out)
        del self._out[:]
        return data

    def has_pending_output(self):
        return bool(self._out)
