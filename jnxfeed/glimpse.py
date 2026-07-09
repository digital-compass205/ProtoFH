"""GLIMPSE snapshot client (JNX_PLAN.md T5.2 / section 3.5).

GLIMPSE is an ordinary SoupBinTCP session serving a point-in-time
snapshot in ITCH message formats: log in (Requested Session MUST be
blank per the spec; requested seq 1 per plan default, open question Q3),
apply every sequenced message to a caller-supplied ``Market`` until the
`G` End of Snapshot arrives, whose 8-byte sequence number is the next
sequence of the real-time ITCH feed. Then log out, stop, and report
``(next_live_seq, snapshot_message_count)``.

A snapshot is one-shot: reconnecting mid-snapshot would re-apply
messages into a partially-filled Market, so the connector is created
with ``max_retries=1`` and any transport loss before `G` is a failure
(the orchestrator may retry with a FRESH Market if it wants to).

Failure paths are surfaced distinctly through ``on_failed(reason)``:
``"login rejected: ..."``, ``"connection lost before end of snapshot"``,
``"snapshot timeout after Xs"``.
"""
import logging
import time

from jnxfeed.itch import codec
from jnxfeed.itch import messages as itch_messages
from jnxfeed.net.tcp import TcpSoupConnector
from jnxfeed.soup import packets as sp
from jnxfeed.soup.session import SoupSession

logger = logging.getLogger("jnxfeed.glimpse")

#: Reject-code -> human meaning (plan section 3.4).
REJECT_MEANINGS = {
    sp.REJECT_NOT_AUTHORIZED: "not authorized (bad credentials or username-port pairing)",
    sp.REJECT_SESSION_UNAVAILABLE: "requested session is not available",
}

DEFAULT_SNAPSHOT_TIMEOUT = 30.0


def _noop(*args, **kwargs):
    pass


class GlimpseClient(object):
    """One GLIMPSE snapshot download into a Market.

    Callbacks:
    - ``on_complete(next_live_seq, snapshot_message_count)`` -- the `G`
      arrived; count excludes the `G` itself.
    - ``on_failed(reason)`` -- login rejected / connection lost before
      `G` / timeout. Fired at most once, mutually exclusive with
      on_complete.
    """

    def __init__(self, reactor, host, port, username, password, market,
                 on_complete=None, on_failed=None,
                 timeout=DEFAULT_SNAPSHOT_TIMEOUT, tick_interval=0.25,
                 connect_timeout=10.0):
        self.reactor = reactor
        self.market = market
        self.on_complete = on_complete or _noop
        self.on_failed = on_failed or _noop
        self.timeout = timeout

        self.next_live_seq = None
        self.message_count = 0
        self.done = False

        # Spec (plan 3.5): requested_session MUST be blank; seq 1 (Q3).
        self.session = SoupSession(
            username, password,
            requested_session="", requested_seq=1,
            on_login_rejected=self._on_login_rejected,
            on_message=self._on_message,
            on_end_of_session=self._on_end_of_session,
        )
        self.connector = TcpSoupConnector(
            reactor, host, port, self.session,
            tick_interval=tick_interval,
            connect_timeout=connect_timeout,
            max_retries=1,  # a snapshot cannot be resumed mid-way
            on_connect_failed=self._on_connect_failed,
            on_disconnect=self._on_disconnect,
            on_give_up=self._on_give_up,
        )
        self._watchdog = None

    # -- control -----------------------------------------------------------

    def start(self):
        logger.info("GLIMPSE snapshot: connecting to %s:%d",
                    self.connector.host, self.connector.port)
        self._watchdog = self.reactor.call_later(self.timeout, self._on_timeout)
        self.connector.start()

    def stop(self):
        """Abort/finish: cancel the watchdog and stop the connector."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        self.connector.stop()

    # -- session events ---------------------------------------------------------

    def _on_message(self, seq, payload):
        if self.done:
            return
        msg = codec.decode(payload)
        self.market.apply(msg)
        if type(msg) is itch_messages.EndOfSnapshot:
            self._complete(msg.sequence_number)
        else:
            self.message_count += 1

    def _complete(self, next_live_seq):
        self.done = True
        self.next_live_seq = next_live_seq
        logger.info(
            "GLIMPSE snapshot complete: %d messages, next live ITCH seq %d",
            self.message_count, next_live_seq,
        )
        # Clean logout, best effort, then stop.
        self.session.logout(time.monotonic())
        self.connector.flush()
        self.stop()
        self.on_complete(next_live_seq, self.message_count)

    def _on_login_rejected(self, code):
        meaning = REJECT_MEANINGS.get(code, "unknown reject code")
        self._fail("login rejected: code {!r} ({})".format(code, meaning))

    def _on_end_of_session(self):
        # A `Z` before `G` means the snapshot ended prematurely.
        self._fail("connection lost before end of snapshot (server sent Z)")

    def _on_connect_failed(self, exc):
        logger.warning("GLIMPSE connect failed: %s", exc)

    def _on_disconnect(self, exc):
        # A snapshot cannot be resumed mid-way: any transport loss before
        # `G` is terminal for this attempt (the orchestrator may retry
        # with a fresh Market). Failing here also stops the connector, so
        # no reconnect is attempted.
        self._fail("connection lost before end of snapshot")

    def _on_give_up(self):
        self._fail("connection lost before end of snapshot")

    def _on_timeout(self):
        self._watchdog = None
        self._fail("snapshot timeout after {:.1f}s".format(self.timeout))

    def _fail(self, reason):
        if self.done:
            return
        self.done = True
        logger.error("GLIMPSE snapshot failed: %s", reason)
        self.stop()
        self.on_failed(reason)
