"""Tests for jnxfeed.handler (JNX_PLAN.md T5.2, FeedHandler orchestrator)."""
from jnxfeed import handler as handler_mod
from jnxfeed.book.market import Market
from jnxfeed.handler import FeedHandler
from jnxfeed.itch import codec
from jnxfeed.itch import messages as m
from jnxfeed.net import reactor as reactor_mod
from jnxfeed.soup import packets as sp

from soup_stub import StubSoupServer

WATCHDOG = 5.0


def run_with_watchdog(reactor, timeout=WATCHDOG):
    handle = reactor.call_later(timeout, reactor.stop)
    reactor.run()
    handle.cancel()


def itch_t(seconds):
    return codec.encode(m.TimestampSeconds(seconds=seconds))


def itch_s(event, group=""):
    return codec.encode(m.SystemEvent(ns=1, group=group, event=event))


def snapshot_msgs(next_live_seq):
    return [
        codec.encode(m.OrderbookDirectory(
            ns=1, orderbook_id="8306", isin="JP0000000000", group="DAY",
            round_lot=100, tick_table_id=1, price_decimals=1,
            upper_limit=20000, lower_limit=10000)),
        codec.encode(m.TradingState(ns=2, orderbook_id="8306", group="DAY",
                                    state="T")),
        codec.encode(m.OrderAdded(ns=3, order_number=42, side="B", qty=100,
                                  orderbook_id="8306", group="DAY",
                                  price=15000)),
        codec.encode(m.EndOfSnapshot(sequence_number=next_live_seq)),
    ]


class Recorder(object):
    """Collects handler callbacks; stops the reactor on end/failure."""

    def __init__(self, reactor):
        self.reactor = reactor
        self.live = []
        self.ended = []
        self.failed = []
        self.seqs = []
        self.messages = []

    def on_live(self, next_seq):
        self.live.append(next_seq)

    def on_ended(self, reason):
        self.ended.append(reason)
        self.reactor.stop()

    def on_failed(self, reason):
        self.failed.append(reason)
        self.reactor.stop()

    def on_message(self, seq, msg):
        self.seqs.append(seq)
        self.messages.append(msg)

    def kwargs(self):
        return dict(on_live=self.on_live, on_ended=self.on_ended,
                    on_failed=self.on_failed, on_message=self.on_message)


def make_handler(reactor, market, rec, itch_port, glimpse_port=None, **kwargs):
    return FeedHandler(
        reactor, market, "127.0.0.1", itch_port, "TEST", "SECRET",
        glimpse_host=("127.0.0.1" if glimpse_port is not None else None),
        glimpse_port=glimpse_port,
        tick_interval=0.05, backoff_initial=0.05, backoff_max=0.2,
        snapshot_timeout=3.0,
        **dict(rec.kwargs(), **kwargs)
    )


# --- full-replay mode ---------------------------------------------------------

def test_full_replay_mode_from_seq_1_then_z_ends():
    msgs = [itch_t(34200 + i) for i in range(5)]
    market = Market()
    with StubSoupServer([{"messages": msgs, "end_of_session": True}]) as srv:
        r = reactor_mod.Reactor()
        rec = Recorder(r)
        fh = make_handler(r, market, rec, srv.port)
        assert fh.state == handler_mod.STATE_INIT
        fh.start()
        run_with_watchdog(r)
        r.close()

        assert srv.login_requests[0].requested_sequence == 1
        assert srv.login_requests[0].requested_session == ""

    assert rec.live == [1]
    assert rec.seqs == [1, 2, 3, 4, 5]
    assert all(type(msg) is m.TimestampSeconds for msg in rec.messages)
    # Applied to the Market exactly once each.
    assert market.counters()["by_type"] == {"T": 5}
    # (f) Z -> ENDED, clean stop.
    assert fh.state == handler_mod.STATE_ENDED
    assert rec.ended == ["SoupBinTCP end of session (Z)"]
    assert rec.failed == []


def test_full_replay_custom_requested_seq():
    msgs = [itch_t(34200 + i) for i in range(6)]
    market = Market()
    with StubSoupServer([{"messages": msgs, "end_of_session": True}]) as srv:
        r = reactor_mod.Reactor()
        rec = Recorder(r)
        fh = make_handler(r, market, rec, srv.port, requested_seq=4)
        fh.start()
        run_with_watchdog(r)
        r.close()
        assert srv.login_requests[0].requested_sequence == 4

    assert rec.live == [4]
    assert rec.seqs == [4, 5, 6]


# --- snapshot mode ------------------------------------------------------------

def test_snapshot_mode_end_to_end_exactly_once():
    """GLIMPSE snapshot ends with G(5); ITCH must be asked for exactly
    seq 5 and the Market must hold snapshot + live state with no message
    applied twice or skipped."""
    n = 5
    live_msgs = [itch_t(34200 + i) for i in range(8)]  # seqs 1..8
    market = Market()
    with StubSoupServer([{"messages": snapshot_msgs(n), "linger": 1.0}]) as gsrv, \
         StubSoupServer([{"messages": live_msgs, "end_of_session": True}]) as isrv:
        r = reactor_mod.Reactor()
        rec = Recorder(r)
        fh = make_handler(r, market, rec, isrv.port, glimpse_port=gsrv.port)
        fh.start()
        assert fh.state == handler_mod.STATE_SNAPSHOT
        run_with_watchdog(r)
        r.close()

        # GLIMPSE login: blank session, seq 1. ITCH login: exactly seq N.
        assert gsrv.login_requests[0].requested_session == ""
        assert gsrv.login_requests[0].requested_sequence == 1
        assert [lr.requested_sequence for lr in isrv.login_requests] == [n]

    assert rec.live == [n]
    # Live messages N..8 exactly once, in order; N-1 never re-applied.
    assert rec.seqs == [5, 6, 7, 8]
    # Market = snapshot state + live tail, each message exactly once.
    assert market.counters()["by_type"] == {
        "R": 1, "H": 1, "A": 1, "G": 1,  # snapshot
        "T": 4,                          # live seqs 5..8 only
    }
    assert market.end_of_snapshot_seq == n
    assert market.books.books["8306"].bid_levels() == [(15000, 100)]
    assert fh.state == handler_mod.STATE_ENDED


def test_snapshot_failure_fails_handler():
    market = Market()
    with StubSoupServer([{"reject": sp.REJECT_SESSION_UNAVAILABLE}]) as gsrv:
        r = reactor_mod.Reactor()
        rec = Recorder(r)
        # ITCH port never used: snapshot fails first. Port 1 is safe to
        # pass because no connection is ever attempted.
        fh = make_handler(r, market, rec, 1, glimpse_port=gsrv.port)
        fh.start()
        run_with_watchdog(r)
        r.close()

    assert fh.state == handler_mod.STATE_FAILED
    assert len(rec.failed) == 1
    assert "snapshot failed" in rec.failed[0]
    assert "login rejected" in rec.failed[0]
    assert rec.live == []


# --- live-phase behaviors --------------------------------------------------------

def test_mid_live_disconnect_reconnect_resume_no_gap_no_dup():
    msgs = [itch_t(34200 + i) for i in range(6)]
    scripts = [
        {"messages": msgs, "drop_after": 3},
        {"messages": msgs, "end_of_session": True},
    ]
    market = Market()
    with StubSoupServer(scripts) as srv:
        r = reactor_mod.Reactor()
        rec = Recorder(r)
        fh = make_handler(r, market, rec, srv.port)
        fh.start()
        run_with_watchdog(r)
        r.close()
        assert [lr.requested_sequence for lr in srv.login_requests] == [1, 4]

    # No gap, no duplicate across the reconnect.
    assert rec.seqs == [1, 2, 3, 4, 5, 6]
    assert market.counters()["by_type"] == {"T": 6}
    assert fh.reconnects == 1
    # on_live fires again on resume.
    assert rec.live == [1, 4]
    assert fh.state == handler_mod.STATE_ENDED


def test_itch_login_rejected_fails():
    market = Market()
    with StubSoupServer([{"reject": sp.REJECT_NOT_AUTHORIZED}]) as srv:
        r = reactor_mod.Reactor()
        rec = Recorder(r)
        fh = make_handler(r, market, rec, srv.port)
        fh.start()
        run_with_watchdog(r)
        r.close()

    assert fh.state == handler_mod.STATE_FAILED
    assert "ITCH login rejected" in rec.failed[0]
    assert "'A'" in rec.failed[0]


def test_system_event_c_ends_session():
    # (g) ITCH `S` event `C` (end of messages) -> ENDED, even with the
    # transport still up (server lingers instead of closing).
    msgs = [itch_t(34200), itch_s("C")]
    market = Market()
    with StubSoupServer([{"messages": msgs, "linger": 2.0}]) as srv:
        r = reactor_mod.Reactor()
        rec = Recorder(r)
        fh = make_handler(r, market, rec, srv.port)
        fh.start()
        run_with_watchdog(r)
        r.close()

    assert fh.state == handler_mod.STATE_ENDED
    assert rec.ended == ["ITCH end of messages (S event C)"]
    # Both messages were still applied (S event C included).
    assert market.counters()["by_type"] == {"T": 1, "S": 1}
    assert market.refdata.system_events == [(1, "", "C")]


def test_stop_by_caller_is_clean_end():
    msgs = [itch_t(34200 + i) for i in range(3)]
    market = Market()
    with StubSoupServer([{"messages": msgs, "linger": 2.0}]) as srv:
        r = reactor_mod.Reactor()
        rec = Recorder(r)
        fh = make_handler(r, market, rec, srv.port)
        fh.start()

        def stop_all():
            fh.stop()

        r.call_later(0.4, stop_all)
        run_with_watchdog(r)
        r.close()

    assert fh.state == handler_mod.STATE_ENDED
    assert rec.ended == ["stopped by caller"]
    assert rec.seqs == [1, 2, 3]
