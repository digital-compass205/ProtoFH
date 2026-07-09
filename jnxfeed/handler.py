"""FeedHandler bootstrap orchestrator (JNX_PLAN.md T5.2 / section 3.6).

Implements both bootstrap modes on top of the reactor stack:

1. **Full replay** (no GLIMPSE endpoint given): log in to ITCH-TCP with
   the caller's requested seq (default 1 = whole-session replay), apply
   every sequenced message to the Market, stay live.
2. **Snapshot sync** (GLIMPSE endpoint given): run the GLIMPSE client to
   completion first, read the next live seq `N` from its `G`, then --
   and only then -- construct and start the ITCH session with requested
   seq `N`. Soup replays from exactly the requested seq, so message
   `N-1` is never applied twice and `N` onward exactly once.

States: INIT -> (SNAPSHOT) -> LIVE -> ENDED / FAILED. Every transition
is logged on the "jnxfeed.handler" logger.

Mid-LIVE transport drops are absorbed by TcpSoupConnector's
reconnect-with-backoff: the SoupSession re-logs-in with its maintained
(session id, next expected seq), so nothing is lost or duplicated.
Session end is either Soup `Z` or the ITCH `S` System Event `C` (end of
messages); both stop the handler cleanly with state ENDED.
"""
import logging
import time

from jnxfeed.glimpse import REJECT_MEANINGS, GlimpseClient
from jnxfeed.itch import codec
from jnxfeed.itch import messages as itch_messages
from jnxfeed.net.tcp import TcpSoupConnector
from jnxfeed.soup.session import SoupSession
from jnxfeed.types import EVENT_END_OF_MESSAGES

logger = logging.getLogger("jnxfeed.handler")

STATE_INIT = "INIT"
STATE_SNAPSHOT = "SNAPSHOT"
STATE_LIVE = "LIVE"
STATE_ENDED = "ENDED"
STATE_FAILED = "FAILED"


def _noop(*args, **kwargs):
    pass


class FeedHandler(object):
    """Orchestrates bootstrap + live ITCH consumption into one Market.

    Callbacks (all optional):
    - ``on_live(next_seq)`` -- ITCH login accepted, sequenced flow
      starting at ``next_seq`` (fires again after a reconnect-resume).
    - ``on_ended(reason)`` -- clean end (`Z`, `S` event `C`, or stop()).
    - ``on_failed(reason)`` -- login reject, snapshot failure, or
      retries exhausted.
    - ``on_message(seq, decoded_msg)`` -- every live ITCH message, called
      AFTER Market.apply, for CLI taps (T7.1). Snapshot messages do NOT
      pass through this hook.
    """

    def __init__(self, reactor, market, itch_host, itch_port,
                 username, password,
                 glimpse_host=None, glimpse_port=None, requested_seq=1,
                 on_live=None, on_ended=None, on_failed=None, on_message=None,
                 snapshot_timeout=30.0, tick_interval=0.25,
                 max_retries=None, backoff_initial=0.5, backoff_max=10.0,
                 connect_timeout=10.0):
        self.reactor = reactor
        self.market = market
        self.itch_host = itch_host
        self.itch_port = itch_port
        self.username = username
        self.password = password
        self.glimpse_host = glimpse_host
        self.glimpse_port = glimpse_port
        self.requested_seq = requested_seq

        self.on_live = on_live or _noop
        self.on_ended = on_ended or _noop
        self.on_failed = on_failed or _noop
        self.on_message = on_message or _noop

        self.snapshot_timeout = snapshot_timeout
        self.tick_interval = tick_interval
        self.max_retries = max_retries
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.connect_timeout = connect_timeout

        self.state = STATE_INIT
        self.end_reason = None
        self.reconnects = 0

        self._glimpse = None
        self._session = None
        self._connector = None

    @property
    def session_id(self):
        """Soup session id once logged in (None before)."""
        return self._session.session_id if self._session is not None else None

    @property
    def next_seq(self):
        """Next expected sequence number (None before login)."""
        return self._session.next_seq if self._session is not None else None

    # -- control -----------------------------------------------------------

    def start(self):
        """Begin the configured bootstrap (plan 3.6)."""
        if self.state != STATE_INIT:
            raise RuntimeError("start() requires state INIT, got {}".format(self.state))
        if self.glimpse_host is not None:
            self._transition(STATE_SNAPSHOT,
                             "GLIMPSE snapshot from {}:{}".format(
                                 self.glimpse_host, self.glimpse_port))
            self._glimpse = GlimpseClient(
                self.reactor, self.glimpse_host, self.glimpse_port,
                self.username, self.password, self.market,
                on_complete=self._on_snapshot_complete,
                on_failed=self._on_snapshot_failed,
                timeout=self.snapshot_timeout,
                tick_interval=self.tick_interval,
                connect_timeout=self.connect_timeout,
            )
            self._glimpse.start()
        else:
            logger.info("full-replay bootstrap: ITCH from seq %d",
                        self.requested_seq)
            self._start_itch(self.requested_seq)

    def stop(self):
        """Clean shutdown: logout if live, stop everything."""
        if self._glimpse is not None:
            self._glimpse.stop()
        if self._connector is not None:
            if self._session is not None:
                self._session.logout(time.monotonic())
                self._connector.flush()
            self._connector.stop()
        if self.state not in (STATE_ENDED, STATE_FAILED):
            self.end_reason = "stopped by caller"
            self._transition(STATE_ENDED, self.end_reason)
            self.on_ended(self.end_reason)

    # -- snapshot phase ----------------------------------------------------------

    def _on_snapshot_complete(self, next_live_seq, message_count):
        logger.info("snapshot applied (%d messages); going live from seq %d",
                    message_count, next_live_seq)
        self._glimpse = None
        self._start_itch(next_live_seq)

    def _on_snapshot_failed(self, reason):
        self._glimpse = None
        self._fail("snapshot failed: {}".format(reason))

    # -- live ITCH phase ------------------------------------------------------------

    def _start_itch(self, requested_seq):
        # Constructed only once the requested seq is known (T4.1 design
        # note: the connector re-logs-in from the session's maintained
        # resume point on every (re)connect).
        self._session = SoupSession(
            self.username, self.password,
            requested_session="", requested_seq=requested_seq,
            on_login_accepted=self._on_login_accepted,
            on_login_rejected=self._on_login_rejected,
            on_message=self._on_itch_message,
            on_end_of_session=self._on_end_of_session,
            on_peer_silent=self._on_peer_silent,
        )
        self._connector = TcpSoupConnector(
            self.reactor, self.itch_host, self.itch_port, self._session,
            tick_interval=self.tick_interval,
            max_retries=self.max_retries,
            backoff_initial=self.backoff_initial,
            backoff_max=self.backoff_max,
            connect_timeout=self.connect_timeout,
            on_disconnect=self._on_disconnect,
            on_give_up=self._on_give_up,
        )
        self._connector.start()

    def _on_login_accepted(self, session_id, next_seq):
        went_live = self.state != STATE_LIVE
        self._transition(STATE_LIVE,
                         "ITCH login accepted: session {!r}, next seq {}".format(
                             session_id, next_seq))
        if not went_live:
            logger.info("resumed after reconnect at seq %d", next_seq)
        self.on_live(next_seq)

    def _on_login_rejected(self, code):
        meaning = REJECT_MEANINGS.get(code, "unknown reject code")
        self._fail("ITCH login rejected: code {!r} ({})".format(code, meaning))

    def _on_itch_message(self, seq, payload):
        msg = codec.decode(payload)
        self.market.apply(msg)
        self.on_message(seq, msg)
        if (type(msg) is itch_messages.SystemEvent
                and msg.event == EVENT_END_OF_MESSAGES):
            self._end("ITCH end of messages (S event C)")

    def _on_end_of_session(self):
        self._end("SoupBinTCP end of session (Z)")

    def _on_peer_silent(self):
        logger.warning("peer silent beyond timeout; forcing reconnect")
        if self._connector is not None:
            self._connector.reconnect()

    def _on_disconnect(self, exc):
        if self.state != STATE_LIVE:
            return
        self.reconnects += 1
        logger.warning("ITCH connection lost (%s); reconnect #%d will resume "
                       "at seq %d", exc, self.reconnects,
                       self._session.next_seq)

    def _on_give_up(self):
        self._fail("ITCH retries exhausted")

    # -- transitions -----------------------------------------------------------------

    def _end(self, reason):
        if self.state in (STATE_ENDED, STATE_FAILED):
            return
        self.end_reason = reason
        if self._connector is not None:
            self._connector.stop()
        self._transition(STATE_ENDED, reason)
        self.on_ended(reason)

    def _fail(self, reason):
        if self.state in (STATE_ENDED, STATE_FAILED):
            return
        self.end_reason = reason
        if self._connector is not None:
            self._connector.stop()
        self._transition(STATE_FAILED, reason)
        self.on_failed(reason)

    def _transition(self, state, detail):
        if state != self.state:
            logger.info("state %s -> %s: %s", self.state, state, detail)
            self.state = state
        else:
            logger.info("state %s: %s", state, detail)
