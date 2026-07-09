"""Tests for jnxfeed.glimpse (JNX_PLAN.md T5.2, GLIMPSE snapshot client)."""
from jnxfeed.book.market import Market
from jnxfeed.glimpse import GlimpseClient
from jnxfeed.itch import codec
from jnxfeed.itch import messages as m
from jnxfeed.net import reactor as reactor_mod
from jnxfeed.net import tcp as tcp_mod
from jnxfeed.soup import packets as sp

from soup_stub import StubSoupServer

WATCHDOG = 5.0


def run_with_watchdog(reactor, timeout=WATCHDOG):
    handle = reactor.call_later(timeout, reactor.stop)
    reactor.run()
    handle.cancel()


def snapshot_messages():
    """A tiny but representative snapshot: directory, state, open order."""
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
    ]


def make_client(reactor, port, market, results, **kwargs):
    def on_complete(next_seq, count):
        results["complete"] = (next_seq, count)
        reactor.stop()

    def on_failed(reason):
        results["failed"] = reason
        reactor.stop()

    return GlimpseClient(reactor, "127.0.0.1", port, "TEST", "SECRET", market,
                         on_complete=on_complete, on_failed=on_failed, **kwargs)


def test_snapshot_happy_path():
    msgs = snapshot_messages() + [
        codec.encode(m.EndOfSnapshot(sequence_number=4242)),
    ]
    market = Market()
    results = {}
    with StubSoupServer([{"messages": msgs, "linger": 1.0}]) as srv:
        r = reactor_mod.Reactor()
        client = make_client(r, srv.port, market, results)
        client.start()
        run_with_watchdog(r)
        r.close()

        # Spec: blank requested session, seq 1 (plan 3.5).
        assert srv.login_requests[0].requested_session == ""
        assert srv.login_requests[0].requested_sequence == 1

    assert results == {"complete": (4242, 3)}
    assert client.next_live_seq == 4242
    # Snapshot applied to the Market, G recorded.
    assert market.end_of_snapshot_seq == 4242
    assert market.refdata.instruments["8306"].directory_missing is False
    assert market.refdata.instruments["8306"].trading_state == "T"
    assert market.books.books["8306"].bid_levels() == [(15000, 100)]
    # Connector stopped cleanly after G.
    assert client.connector.state == tcp_mod.STATE_STOPPED


def test_login_rejected_surfaced():
    market = Market()
    results = {}
    with StubSoupServer([{"reject": sp.REJECT_NOT_AUTHORIZED}]) as srv:
        r = reactor_mod.Reactor()
        client = make_client(r, srv.port, market, results)
        client.start()
        run_with_watchdog(r)
        r.close()

    assert "login rejected" in results["failed"]
    assert "'A'" in results["failed"]
    assert "complete" not in results


def test_connection_lost_before_g_is_failure():
    # Server drops abruptly after 2 of the snapshot messages, before G.
    msgs = snapshot_messages()
    market = Market()
    results = {}
    with StubSoupServer([{"messages": msgs, "drop_after": 2}]) as srv:
        r = reactor_mod.Reactor()
        client = make_client(r, srv.port, market, results,
                             tick_interval=0.05)
        client.start()
        run_with_watchdog(r)
        r.close()

    assert "connection lost before end of snapshot" in results["failed"]
    assert "complete" not in results
    # No G ever arrived.
    assert market.end_of_snapshot_seq is None


def test_z_before_g_is_failure():
    msgs = snapshot_messages()
    market = Market()
    results = {}
    with StubSoupServer([{"messages": msgs, "end_of_session": True}]) as srv:
        r = reactor_mod.Reactor()
        client = make_client(r, srv.port, market, results)
        client.start()
        run_with_watchdog(r)
        r.close()

    assert "server sent Z" in results["failed"]


def test_timeout_budget_exceeded():
    # Server logs us in then serves nothing; the watchdog must fire.
    market = Market()
    results = {}
    with StubSoupServer([{"messages": [], "linger": 3.0}]) as srv:
        r = reactor_mod.Reactor()
        client = make_client(r, srv.port, market, results,
                             timeout=0.3, tick_interval=0.05)
        client.start()
        run_with_watchdog(r)
        r.close()

    assert "timeout" in results["failed"]
    assert client.connector.state == tcp_mod.STATE_STOPPED
