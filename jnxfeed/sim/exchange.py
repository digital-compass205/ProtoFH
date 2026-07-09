"""Exchange simulator (JNX_PLAN.md T6.1).

Replays a ``.itch`` fixture (or an in-memory message list) as a pair of
SoupBinTCP servers so the whole client stack is testable end-to-end
offline:

- **ITCH server**: login validation (configurable credentials -> reject
  `A`; non-blank unknown requested session -> reject `S`), sequenced
  replay from EXACTLY the client's requested seq (LoginAccepted echoes
  it; requested seq 0 = "most recent" = next-to-be-generated, i.e. after
  the last fixture message), server heartbeats while pacing idles the
  line, `Z` at end of stream, one optional scripted abrupt disconnect
  after N sequenced packets (once per simulator), and any number of
  sequential client connections so reconnect-resume works.
- **GLIMPSE server**: login must use a blank requested session (else
  reject `S`); the snapshot is built by running the fixture through a
  fresh Market up to a configurable cut point and re-emitting the state
  as ITCH messages (T clock, L tick rows, R per announced instrument, H/Y
  where the state differs from the absence default, ref-price `A`, one
  `A` per live order with its original order number), then `G` carrying
  next-live-seq = cut+1. The connection then lingers for the client's
  logout; the client closing first is tolerated; `Z` is never sent
  before `G`.
- **Speed control**: as-fast-as-possible (default, for tests), fixed
  messages/sec, or realtime pacing derived from `T` messages.

Implementation is deliberately threaded + blocking: this is test
infrastructure, not the production path (which is the sans-I/O reactor
stack this simulator exists to exercise). Still Python-3.6.4-clean.
"""
import logging
import socket
import threading
import time

from jnxfeed import itchfile
from jnxfeed.book.market import Market
from jnxfeed.itch import codec
from jnxfeed.itch import messages as m
from jnxfeed.soup import packets as sp

logger = logging.getLogger("jnxfeed.sim")

SPEED_MAX = None          # as fast as possible
SPEED_REALTIME = "realtime"

_LOGIN_TIMEOUT = 5.0
_LINGER_TIMEOUT = 5.0


class ExchangeSimulator(object):
    """One ITCH + one GLIMPSE SoupBinTCP server over a shared fixture.

    ``speed``: None = as-fast-as-possible; a number = messages/sec;
    ``"realtime"`` = sleep the deltas between `T` timestamp messages.
    ``drop_after``: abruptly close the ITCH connection after this many
    sequenced packets -- once per simulator lifetime (the resumed
    connection replays normally).
    ``glimpse_cut``: snapshot cut point -- an int message count, or a
    float fraction of the fixture (default 0.5).
    """

    def __init__(self, itch_file=None, messages=None,
                 username="TEST", password="SECRET",
                 itch_port=0, glimpse_port=0, session_id="SIM0000001",
                 speed=SPEED_MAX, drop_after=None, glimpse_cut=None,
                 heartbeat_interval=1.0):
        if messages is not None:
            self.messages = list(messages)
        elif itch_file is not None:
            self.messages = list(itchfile.read_file(itch_file))
        else:
            raise ValueError("need itch_file or messages")
        self.username = username
        self.password = password
        self.session_id = session_id
        self.speed = speed
        self.drop_after = drop_after
        self.heartbeat_interval = heartbeat_interval

        if glimpse_cut is None:
            self.glimpse_cut = len(self.messages) // 2
        elif isinstance(glimpse_cut, float) and glimpse_cut < 1.0:
            self.glimpse_cut = int(len(self.messages) * glimpse_cut)
        else:
            self.glimpse_cut = int(glimpse_cut)

        #: LoginRequest packets seen, for test assertions.
        self.itch_logins = []
        self.glimpse_logins = []

        self._dropped = False  # scripted disconnect fired already?
        self._stopping = False
        self._threads = []
        self._conns = []
        self._lock = threading.Lock()

        self._itch_listener = self._listen(itch_port)
        self._glimpse_listener = self._listen(glimpse_port)
        self.itch_port = self._itch_listener.getsockname()[1]
        self.glimpse_port = self._glimpse_listener.getsockname()[1]

    @staticmethod
    def _listen(port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(5)
        # Poll with a timeout: closing a listening socket from another
        # thread does not reliably wake a blocked accept() on Linux.
        sock.settimeout(0.1)
        return sock

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        for listener, handler in ((self._itch_listener, self._handle_itch),
                                  (self._glimpse_listener, self._handle_glimpse)):
            t = threading.Thread(target=self._accept_loop,
                                 args=(listener, handler))
            t.daemon = True
            t.start()
            self._threads.append(t)
        logger.info("simulator up: ITCH :%d, GLIMPSE :%d, %d messages",
                    self.itch_port, self.glimpse_port, len(self.messages))
        return self

    def stop(self):
        self._stopping = True
        for listener in (self._itch_listener, self._glimpse_listener):
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- accept/connection plumbing -----------------------------------------------

    def _accept_loop(self, listener, handler):
        while not self._stopping:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)
            with self._lock:
                self._conns.append(conn)
            t = threading.Thread(target=self._run_conn, args=(handler, conn))
            t.daemon = True
            t.start()
            with self._lock:
                self._threads.append(t)

    def _run_conn(self, handler, conn):
        try:
            handler(conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)

    def _read_login(self, conn):
        fb = sp.FrameBuffer()
        conn.settimeout(_LOGIN_TIMEOUT)
        while True:
            data = conn.recv(4096)
            if not data:
                return None
            for pkt in fb.feed(data):
                if isinstance(pkt, sp.LoginRequest):
                    conn.settimeout(None)
                    return pkt

    def _validate_credentials(self, conn, login):
        if login.username != self.username or login.password != self.password:
            conn.sendall(sp.encode(sp.LoginRejected(
                reject_code=sp.REJECT_NOT_AUTHORIZED)))
            return False
        return True

    def _linger(self, conn):
        """Keep reading (and discarding) client bytes until the client
        closes or the linger timeout passes -- tolerates close-first."""
        conn.settimeout(_LINGER_TIMEOUT)
        try:
            while conn.recv(4096):
                pass
        except (OSError, socket.timeout):
            pass

    # -- ITCH server ------------------------------------------------------------

    def _handle_itch(self, conn):
        login = self._read_login(conn)
        if login is None:
            return
        self.itch_logins.append(login)
        if not self._validate_credentials(conn, login):
            return
        requested = login.requested_session
        if requested != "" and requested != self.session_id:
            conn.sendall(sp.encode(sp.LoginRejected(
                reject_code=sp.REJECT_SESSION_UNAVAILABLE)))
            return

        seq = login.requested_sequence
        if seq == 0:
            # "most recent" = the next message this session would
            # generate; for a fully pre-recorded fixture that is one past
            # the last message.
            seq = len(self.messages) + 1
        seq = max(seq, 1)
        conn.sendall(sp.encode(sp.LoginAccepted(session=self.session_id,
                                                sequence=seq)))
        logger.info("ITCH login %r: replay from seq %d", login.username, seq)

        state = {"last_send": time.monotonic(), "prev_t": None}
        sent = 0
        for message in self.messages[seq - 1:]:
            if self._stopping:
                return
            if (self.drop_after is not None and not self._dropped
                    and sent >= self.drop_after):
                self._dropped = True
                logger.info("scripted disconnect after %d packets", sent)
                return  # abrupt close, no Z
            if not self._pace(conn, message, state):
                return
            conn.sendall(sp.encode(sp.SequencedData(message=message)))
            state["last_send"] = time.monotonic()
            sent += 1

        conn.sendall(sp.encode(sp.EndOfSession()))
        self._linger(conn)

    def _pace(self, conn, message, state):
        """Sleep per the speed setting; send heartbeats while idle.
        Returns False if the simulator is stopping."""
        if self.speed is SPEED_MAX:
            return True
        if self.speed == SPEED_REALTIME:
            delay = 0.0
            if message[0:1] == b"T":
                seconds = int.from_bytes(message[1:5], "big")
                prev = state["prev_t"]
                state["prev_t"] = seconds
                if prev is not None and seconds > prev:
                    delay = float(seconds - prev)
        else:
            delay = 1.0 / float(self.speed)
        deadline = time.monotonic() + delay
        while not self._stopping:
            now = time.monotonic()
            if now >= deadline:
                return True
            if now - state["last_send"] >= self.heartbeat_interval:
                conn.sendall(sp.encode(sp.ServerHeartbeat()))
                state["last_send"] = now
            time.sleep(min(0.05, deadline - now))
        return False

    # -- GLIMPSE server -----------------------------------------------------------

    def _handle_glimpse(self, conn):
        login = self._read_login(conn)
        if login is None:
            return
        self.glimpse_logins.append(login)
        if not self._validate_credentials(conn, login):
            return
        if login.requested_session != "":
            # Spec (plan 3.5): snapshot login MUST use a blank session.
            conn.sendall(sp.encode(sp.LoginRejected(
                reject_code=sp.REJECT_SESSION_UNAVAILABLE)))
            return

        conn.sendall(sp.encode(sp.LoginAccepted(session=self.session_id,
                                                sequence=1)))
        snapshot, next_seq = self.build_snapshot()
        logger.info("GLIMPSE snapshot: %d messages, next live seq %d",
                    len(snapshot), next_seq)
        for message in snapshot:
            if self._stopping:
                return
            conn.sendall(sp.encode(sp.SequencedData(message=message)))
        conn.sendall(sp.encode(sp.SequencedData(
            message=codec.encode(m.EndOfSnapshot(sequence_number=next_seq)))))
        # Never Z before the client is done; just linger for its logout.
        self._linger(conn)

    def build_snapshot(self):
        """Run the fixture through a fresh Market up to the cut point and
        re-emit the state as raw ITCH messages (without the final `G`).
        Returns ``(messages, next_live_seq)`` with next_live_seq = cut+1.

        Note the snapshot carries no execution history: a Market filled
        from it has an empty trade tape (matching GLIMPSE semantics --
        open orders arrive as `A`, trades do not reappear).
        """
        cut = min(self.glimpse_cut, len(self.messages))
        market = Market()
        for raw in self.messages[:cut]:
            market.apply(codec.decode(raw))

        out = []
        if market.seconds:
            out.append(codec.encode(m.TimestampSeconds(seconds=market.seconds)))
        # Tick tables.
        for table_id in sorted(market.refdata.tick_tables):
            for price_start, tick_size in market.refdata.tick_tables[table_id].rows():
                out.append(codec.encode(m.PriceTickSize(
                    ns=0, tick_table_id=table_id, tick_size=tick_size,
                    price_start=price_start)))
        # Directory, states, reference prices.
        for inst in market.refdata.instruments.values():
            group = inst.group or ""
            if not inst.directory_missing:
                out.append(codec.encode(m.OrderbookDirectory(
                    ns=0, orderbook_id=inst.orderbook_id, isin=inst.isin,
                    group=group, round_lot=inst.round_lot,
                    tick_table_id=inst.tick_table_id,
                    price_decimals=inst.price_decimals,
                    upper_limit=inst.upper_limit,
                    lower_limit=inst.lower_limit)))
            # Emit the current states for every known instrument. (A real
            # spin would omit books whose state equals the absence default
            # -- plan 3.3(4) -- but emitting them keeps a snapshot-filled
            # Market bit-identical to a direct replay, which the
            # differential tests rely on; semantically equivalent.)
            out.append(codec.encode(m.TradingState(
                ns=0, orderbook_id=inst.orderbook_id, group=group,
                state=inst.trading_state)))
            out.append(codec.encode(m.ShortSellRestriction(
                ns=0, orderbook_id=inst.orderbook_id, group=group,
                state=inst.short_sell_state)))
            if inst.reference_price is not None:
                out.append(codec.encode(m.OrderAdded(
                    ns=0, order_number=0, side="B", qty=0,
                    orderbook_id=inst.orderbook_id, group=group,
                    price=inst.reference_price)))
        # Live orders, original order numbers, remaining quantities.
        for order in market.books.orders.values():
            out.append(codec.encode(m.OrderAdded(
                ns=0, order_number=order.order_number, side=order.side,
                qty=order.remaining_qty, orderbook_id=order.orderbook_id,
                group=order.group or "", price=order.price)))
        return out, cut + 1
