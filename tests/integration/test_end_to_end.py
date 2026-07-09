"""End-to-end integration tests (JNX_PLAN.md T6.2).

Full production stack -- Reactor + FeedHandler (+ GlimpseClient) + Market
-- against the T6.1 exchange simulator over localhost TCP, replaying the
committed 2000-message real-data slice (tests/fixtures/
sample_udp_head.itch, the first 2000 messages of the official UDP
sample, which starts mid-session).

The key invariant (plan T6.2): the final Market state is identical
across every path -- full replay, GLIMPSE-snapshot sync, and forced
disconnect+resume -- and equal to a direct in-process replay. The trade
tape is the one documented exception for the snapshot path: a snapshot
carries open orders but no execution history, so only post-cut trades
appear (asserted exactly, derived from the direct replays).
"""
import os

from jnxfeed import handler as handler_mod
from jnxfeed import itchfile
from jnxfeed.book.market import Market
from jnxfeed.handler import FeedHandler
from jnxfeed.itch import codec
from jnxfeed.net import reactor as reactor_mod
from jnxfeed.sim.exchange import ExchangeSimulator

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")
SLICE_FIXTURE = os.path.join(FIXTURES_DIR, "sample_udp_head.itch")

WATCHDOG = 15.0

_MESSAGES = list(itchfile.read_file(SLICE_FIXTURE))
assert len(_MESSAGES) == 2000


def direct_replay(messages):
    market = Market()
    for raw in messages:
        market.apply(codec.decode(raw))
    return market


#: The ground truth every networked path must reproduce.
DIRECT_FULL = direct_replay(_MESSAGES)


# --- state digests -----------------------------------------------------------

def books_digest(market):
    return dict(
        (bid, (book.bid_levels(), book.ask_levels()))
        for bid, book in market.books.books.items()
        if len(book.bids) or len(book.asks)
    )


def orders_digest(market):
    return dict(
        (num, (o.orderbook_id, o.group, o.side, o.price, o.remaining_qty))
        for num, o in market.books.orders.items()
    )


def instruments_digest(market):
    return dict(
        (bid, (inst.group, inst.trading_state, inst.short_sell_state,
               inst.reference_price, inst.directory_missing))
        for bid, inst in market.refdata.instruments.items()
    )


def assert_same_market_state(market, reference):
    """Books, live orders and instruments identical (tape asserted
    separately per scenario)."""
    assert books_digest(market) == books_digest(reference)
    assert orders_digest(market) == orders_digest(reference)
    assert instruments_digest(market) == instruments_digest(reference)
    # Orphan/collision counters must match the direct replay -- derived,
    # not guessed (the slice starts mid-session at seq 12562).
    ref_counters = reference.counters()
    counters = market.counters()
    for key in ("collisions", "orphan_executes", "orphan_deletes",
                "orphan_replaces", "live_orders", "books"):
        assert counters[key] == ref_counters[key], key


# --- harness ----------------------------------------------------------------

def run_stack(sim, glimpse=False, requested_seq=1):
    """Run Reactor+FeedHandler against ``sim`` until on_ended (or the
    watchdog). Returns (market, handler, results dict)."""
    r = reactor_mod.Reactor()
    market = Market()
    results = {"live": [], "seqs": [], "ended": [], "failed": []}

    def on_ended(reason):
        results["ended"].append(reason)
        r.stop()

    def on_failed(reason):
        results["failed"].append(reason)
        r.stop()

    fh = FeedHandler(
        r, market, "127.0.0.1", sim.itch_port, sim.username, sim.password,
        glimpse_host=("127.0.0.1" if glimpse else None),
        glimpse_port=(sim.glimpse_port if glimpse else None),
        requested_seq=requested_seq,
        on_live=lambda seq: results["live"].append(seq),
        on_ended=on_ended,
        on_failed=on_failed,
        on_message=lambda seq, msg: results["seqs"].append(seq),
        tick_interval=0.05, backoff_initial=0.05, backoff_max=0.2,
        snapshot_timeout=10.0,
    )
    fh.start()
    watchdog = r.call_later(WATCHDOG, r.stop)
    r.run()
    watchdog.cancel()
    r.close()
    return market, fh, results


# --- scenarios ------------------------------------------------------------------

def test_full_replay_mode_equals_direct_replay():
    with ExchangeSimulator(itch_file=SLICE_FIXTURE) as sim:
        market, fh, results = run_stack(sim)
        assert [lr.requested_sequence for lr in sim.itch_logins] == [1]

    assert results["failed"] == []
    assert results["live"] == [1]
    # Every fixture message applied exactly once, in sequence.
    assert results["seqs"] == list(range(1, 2001))
    assert dict(market.counters()["by_type"]) == dict(
        DIRECT_FULL.counters()["by_type"])
    assert_same_market_state(market, DIRECT_FULL)
    # Tape identical too on this path.
    assert market.tape.trade_count == DIRECT_FULL.tape.trade_count
    assert market.tape.total_volume == DIRECT_FULL.tape.total_volume
    # (4) Z at EOF -> ENDED cleanly.
    assert fh.state == handler_mod.STATE_ENDED
    assert results["ended"] == ["SoupBinTCP end of session (Z)"]


def test_snapshot_mode_equals_direct_replay():
    cut = 1000
    direct_cut = direct_replay(_MESSAGES[:cut])

    with ExchangeSimulator(itch_file=SLICE_FIXTURE, glimpse_cut=cut) as sim:
        market, fh, results = run_stack(sim, glimpse=True)
        # GLIMPSE login blank session; ITCH asked for exactly cut+1.
        assert sim.glimpse_logins[0].requested_session == ""
        assert [lr.requested_sequence for lr in sim.itch_logins] == [cut + 1]

    assert results["failed"] == []
    assert results["live"] == [cut + 1]
    # Live phase: seqs cut+1..2000 exactly once (message `cut` from the
    # snapshot is never re-applied; cut+1 onward exactly once).
    assert results["seqs"] == list(range(cut + 1, 2001))

    # Final books/orders/instruments identical to the full direct replay.
    assert_same_market_state(market, DIRECT_FULL)

    # Tape: only post-cut executions can appear (snapshot carries none).
    expected_trades = (DIRECT_FULL.tape.trade_count
                       - direct_cut.tape.trade_count)
    expected_volume = (DIRECT_FULL.tape.total_volume
                       - direct_cut.tape.total_volume)
    assert market.tape.trade_count == expected_trades
    assert market.tape.total_volume == expected_volume

    assert fh.state == handler_mod.STATE_ENDED
    assert results["ended"] == ["SoupBinTCP end of session (Z)"]


def test_forced_disconnect_resume_equals_direct_replay():
    with ExchangeSimulator(itch_file=SLICE_FIXTURE, drop_after=500) as sim:
        market, fh, results = run_stack(sim)
        # Scripted drop after 500 packets; resume asked for exactly 501.
        assert [lr.requested_sequence for lr in sim.itch_logins] == [1, 501]

    assert results["failed"] == []
    assert fh.reconnects == 1
    assert results["live"] == [1, 501]
    # No gap, no duplicate across the reconnect.
    assert results["seqs"] == list(range(1, 2001))

    assert dict(market.counters()["by_type"]) == dict(
        DIRECT_FULL.counters()["by_type"])
    assert_same_market_state(market, DIRECT_FULL)
    assert market.tape.trade_count == DIRECT_FULL.tape.trade_count
    assert market.tape.total_volume == DIRECT_FULL.tape.total_volume

    assert fh.state == handler_mod.STATE_ENDED
    assert results["ended"] == ["SoupBinTCP end of session (Z)"]


def test_snapshot_mode_with_disconnect_after_going_live():
    """Snapshot sync AND a mid-live drop: both recovery mechanisms in one
    run still converge to the direct-replay state."""
    cut = 1000
    with ExchangeSimulator(itch_file=SLICE_FIXTURE, glimpse_cut=cut,
                           drop_after=200) as sim:
        market, fh, results = run_stack(sim, glimpse=True)
        # Live from 1001; drop after 200 packets -> resume at 1201.
        assert [lr.requested_sequence for lr in sim.itch_logins] == [1001, 1201]

    assert results["failed"] == []
    assert fh.reconnects == 1
    assert results["seqs"] == list(range(cut + 1, 2001))
    assert_same_market_state(market, DIRECT_FULL)
    assert fh.state == handler_mod.STATE_ENDED
