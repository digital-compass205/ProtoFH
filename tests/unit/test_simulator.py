"""Tests for jnxfeed.sim.exchange (JNX_PLAN.md T6.1).

The blocking T3.3 diagnostic client (jnxfeed.cli.soupclient) is used as
the test counterpart -- simple, linear, and independent of the reactor
stack the simulator exists to exercise.
"""
import os

import pytest

from jnxfeed.book.market import Market
from jnxfeed.cli import soupclient
from jnxfeed.itch import codec
from jnxfeed.itch import messages as m
from jnxfeed.sim.exchange import ExchangeSimulator
from jnxfeed.soup import packets as sp

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")
SLICE_FIXTURE = os.path.join(FIXTURES_DIR, "sample_udp_head.itch")


def synthetic_messages():
    """Small hand-built fixture exercising R/H/A/E/U/D/T + ref-price."""
    return [
        codec.encode(m.TimestampSeconds(seconds=34200)),
        codec.encode(m.OrderbookDirectory(
            ns=1, orderbook_id="8306", isin="JP0000000000", group="DAY",
            round_lot=100, tick_table_id=1, price_decimals=1,
            upper_limit=20000, lower_limit=10000)),
        codec.encode(m.PriceTickSize(ns=2, tick_table_id=1, tick_size=5,
                                     price_start=0)),
        codec.encode(m.TradingState(ns=3, orderbook_id="8306", group="DAY",
                                    state="T")),
        codec.encode(m.OrderAdded(ns=4, order_number=0, side="B", qty=0,
                                  orderbook_id="8306", group="DAY",
                                  price=15005)),
        codec.encode(m.OrderAdded(ns=5, order_number=1, side="B", qty=100,
                                  orderbook_id="8306", group="DAY",
                                  price=15000)),
        codec.encode(m.OrderAdded(ns=6, order_number=2, side="S", qty=80,
                                  orderbook_id="8306", group="DAY",
                                  price=15010)),
        codec.encode(m.OrderExecuted(ns=7, order_number=1, executed_qty=40,
                                     match_number=900)),
        codec.encode(m.OrderReplaced(ns=8, orig_order_number=2,
                                     new_order_number=3, qty=60, price=15020)),
        codec.encode(m.OrderDeleted(ns=9, order_number=3)),
    ]


def connect_and_login(port, requested_session="", requested_seq=1,
                      user="TEST", password="SECRET"):
    client = soupclient.SoupClient("127.0.0.1", port, silence_timeout=5.0)
    client.connect(timeout=3.0)
    accepted = client.login(user, password,
                            requested_session=requested_session,
                            requested_seq=requested_seq, timeout=3.0)
    return client, accepted


def drain_sequenced(client):
    """Read until Z or EOF; returns (messages, ended_cleanly)."""
    received = []
    while True:
        try:
            pkt = client.next_packet(timeout=3.0)
        except soupclient.ConnectionLost:
            return received, False
        if isinstance(pkt, sp.SequencedData):
            received.append(pkt.message)
        elif isinstance(pkt, sp.EndOfSession):
            return received, True
        # heartbeats/debug ignored


# --- ITCH server ----------------------------------------------------------

def test_full_replay_from_seq_1_exact_count_then_z():
    msgs = synthetic_messages()
    with ExchangeSimulator(messages=msgs) as sim:
        client, accepted = connect_and_login(sim.itch_port)
        assert accepted.session == sim.session_id
        assert accepted.sequence == 1
        received, clean = drain_sequenced(client)
        client.logout()
    assert clean is True
    assert received == msgs  # exact fixture, in order, nothing else


def test_requested_seq_k_receives_exact_tail():
    msgs = synthetic_messages()
    with ExchangeSimulator(messages=msgs) as sim:
        client, accepted = connect_and_login(sim.itch_port, requested_seq=7)
        assert accepted.sequence == 7  # LoginAccepted echoes the request
        received, clean = drain_sequenced(client)
        client.logout()
    assert clean is True
    assert received == msgs[6:]


def test_requested_seq_0_means_most_recent():
    msgs = synthetic_messages()
    with ExchangeSimulator(messages=msgs) as sim:
        client, accepted = connect_and_login(sim.itch_port, requested_seq=0)
        # "Most recent" = next-to-be-generated = one past the fixture.
        assert accepted.sequence == len(msgs) + 1
        received, clean = drain_sequenced(client)
        client.logout()
    assert clean is True
    assert received == []


def test_wrong_credentials_rejected_a():
    with ExchangeSimulator(messages=synthetic_messages()) as sim:
        client = soupclient.SoupClient("127.0.0.1", sim.itch_port)
        client.connect(timeout=3.0)
        with pytest.raises(soupclient.LoginRejected) as excinfo:
            client.login("TEST", "WRONG", timeout=3.0)
        client.close()
    assert excinfo.value.code == sp.REJECT_NOT_AUTHORIZED


def test_unknown_session_rejected_s_and_known_session_accepted():
    msgs = synthetic_messages()
    with ExchangeSimulator(messages=msgs) as sim:
        client = soupclient.SoupClient("127.0.0.1", sim.itch_port)
        client.connect(timeout=3.0)
        with pytest.raises(soupclient.LoginRejected) as excinfo:
            client.login("TEST", "SECRET", requested_session="NOPE",
                         timeout=3.0)
        client.close()
        assert excinfo.value.code == sp.REJECT_SESSION_UNAVAILABLE

        # The simulator's own session id is accepted (resume path).
        client, accepted = connect_and_login(
            sim.itch_port, requested_session=sim.session_id, requested_seq=3)
        assert accepted.sequence == 3
        client.logout()


def test_scripted_disconnect_fires_once_then_resume_works():
    msgs = synthetic_messages()
    with ExchangeSimulator(messages=msgs, drop_after=3) as sim:
        client, _ = connect_and_login(sim.itch_port)
        received, clean = drain_sequenced(client)
        client.close()
        assert clean is False           # abrupt close, no Z
        assert received == msgs[:3]     # exactly N packets then drop

        # Resume: connect again at the next needed seq; the drop is
        # spent, so the tail replays fully and ends in Z.
        client, accepted = connect_and_login(
            sim.itch_port, requested_session=sim.session_id, requested_seq=4)
        assert accepted.sequence == 4
        tail, clean = drain_sequenced(client)
        client.logout()
        assert clean is True
        assert tail == msgs[3:]
        assert received + tail == msgs  # no loss, no duplication


def test_heartbeats_sent_while_paced():
    msgs = synthetic_messages()[:3]
    with ExchangeSimulator(messages=msgs, speed=4.0,
                           heartbeat_interval=0.1) as sim:
        client, _ = connect_and_login(sim.itch_port)
        heartbeats = 0
        received = []
        while True:
            pkt = client.next_packet(timeout=3.0)
            if isinstance(pkt, sp.ServerHeartbeat):
                heartbeats += 1
            elif isinstance(pkt, sp.SequencedData):
                received.append(pkt.message)
            elif isinstance(pkt, sp.EndOfSession):
                break
        client.logout()
    assert received == msgs
    assert heartbeats >= 1  # 0.25s inter-message gaps vs 0.1s hb interval


# --- GLIMPSE server ---------------------------------------------------------

def test_glimpse_requires_blank_session():
    with ExchangeSimulator(messages=synthetic_messages()) as sim:
        client = soupclient.SoupClient("127.0.0.1", sim.glimpse_port)
        client.connect(timeout=3.0)
        with pytest.raises(soupclient.LoginRejected) as excinfo:
            client.login("TEST", "SECRET", requested_session=sim.session_id,
                         timeout=3.0)
        client.close()
    assert excinfo.value.code == sp.REJECT_SESSION_UNAVAILABLE


def read_snapshot(port):
    """Log in to the GLIMPSE port, apply everything to a fresh Market
    until G; returns (market, next_live_seq)."""
    client, _ = connect_and_login(port)
    market = Market()
    next_seq = None
    while next_seq is None:
        pkt = client.next_packet(timeout=3.0)
        if isinstance(pkt, sp.SequencedData):
            msg = codec.decode(pkt.message)
            market.apply(msg)
            if type(msg) is m.EndOfSnapshot:
                next_seq = msg.sequence_number
    client.logout()
    return market, next_seq


def instruments_digest(market):
    return dict(
        (bid, (inst.group, inst.trading_state, inst.short_sell_state,
               inst.reference_price, inst.directory_missing, inst.isin,
               inst.round_lot, inst.tick_table_id))
        for bid, inst in market.refdata.instruments.items()
    )


def orders_digest(market):
    return dict(
        (num, (o.orderbook_id, o.group, o.side, o.price, o.remaining_qty))
        for num, o in market.books.orders.items()
    )


def books_digest(market):
    return dict(
        (bid, (book.bid_levels(), book.ask_levels()))
        for bid, book in market.books.books.items()
        if len(book.bids) or len(book.asks)
    )


def assert_snapshot_equals_direct_replay(sim, cut):
    snap_market, next_seq = read_snapshot(sim.glimpse_port)
    assert next_seq == cut + 1

    direct = Market()
    for raw in sim.messages[:cut]:
        direct.apply(codec.decode(raw))

    # Books, live orders, and refdata instruments must be identical.
    assert books_digest(snap_market) == books_digest(direct)
    assert orders_digest(snap_market) == orders_digest(direct)
    assert instruments_digest(snap_market) == instruments_digest(direct)
    tick_rows = lambda mkt: dict(
        (tid, table.rows()) for tid, table in mkt.refdata.tick_tables.items()
    )
    assert tick_rows(snap_market) == tick_rows(direct)
    assert snap_market.seconds == direct.seconds

    # Documented exclusion: the tape. A snapshot carries open orders but
    # no execution history, so the snapshot-filled Market's tape is empty
    # regardless of what traded before the cut.
    assert snap_market.tape.trade_count == 0


def test_glimpse_differential_synthetic():
    msgs = synthetic_messages()
    with ExchangeSimulator(messages=msgs, glimpse_cut=8) as sim:
        assert_snapshot_equals_direct_replay(sim, 8)


def test_glimpse_differential_real_sample_slice():
    """Differential test on the committed 2000-message real-data slice."""
    with ExchangeSimulator(itch_file=SLICE_FIXTURE, glimpse_cut=0.5) as sim:
        cut = sim.glimpse_cut
        assert cut == 1000
        assert_snapshot_equals_direct_replay(sim, cut)
