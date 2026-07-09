"""Tests for jnxfeed.book.market (JNX_PLAN.md T5.1d).

The Market facade is the only API the CLI and FeedHandler use; the full
sample replay here re-asserts the T5.1a-c outcomes through Market.apply
alone.
"""
import os

import pytest

from jnxfeed import itchfile, types
from jnxfeed.book.market import Market
from jnxfeed.itch import codec
from jnxfeed.itch import messages as m

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")
FULL_SAMPLE = os.path.join(FIXTURES_DIR, "sample_udp.itch")

needs_full_sample = pytest.mark.skipif(
    not os.path.exists(FULL_SAMPLE),
    reason="full sample fixture not extracted (python3 -m jnxfeed.cli.fixtures)",
)


def test_routing_all_layers():
    mkt = Market()
    mkt.apply(m.TimestampSeconds(seconds=34200))
    assert mkt.seconds == 34200

    mkt.apply(m.SystemEvent(ns=1, group="", event="O"))
    assert mkt.refdata.system_events == [(1, "", "O")]

    mkt.apply(m.OrderbookDirectory(
        ns=2, orderbook_id="8306", isin="JP0000000000", group="DAY",
        round_lot=100, tick_table_id=1, price_decimals=1,
        upper_limit=20000, lower_limit=10000,
    ))
    assert mkt.refdata.instruments["8306"].directory_missing is False

    mkt.apply(m.PriceTickSize(ns=3, tick_table_id=1, tick_size=5, price_start=0))
    assert mkt.refdata.tick_size("8306", 15000) == 5

    mkt.apply(m.TradingState(ns=4, orderbook_id="8306", group="DAY", state="T"))
    assert mkt.refdata.instruments["8306"].trading_state == "T"

    # Reference-price A: refdata only, never the book.
    mkt.apply(m.OrderAdded(ns=5, order_number=0, side="B", qty=0,
                           orderbook_id="8306", group="DAY", price=15005))
    assert mkt.refdata.instruments["8306"].reference_price == 15005
    assert mkt.books.live_order_count() == 0

    # Real order flow.
    mkt.apply(m.OrderAdded(ns=6, order_number=1, side="B", qty=100,
                           orderbook_id="8306", group="DAY", price=15000))
    assert mkt.books.books["8306"].bid_levels() == [(15000, 100)]

    execution = mkt.apply(m.OrderExecuted(ns=7, order_number=1,
                                          executed_qty=40, match_number=9))
    assert execution.price == 15000
    # Tape timestamp = T clock + ns.
    entry = mkt.tape.entries[-1]
    assert entry.timestamp == 34200 * 10 ** 9 + 7
    assert entry.qty == 40

    mkt.apply(m.OrderDeleted(ns=8, order_number=1))
    assert mkt.books.live_order_count() == 0

    counts = mkt.counters()
    assert counts["by_type"] == {"A": 2, "D": 1, "E": 1, "H": 1, "L": 1,
                                 "R": 1, "S": 1, "T": 1}
    assert counts["messages"] == 9
    assert counts["executions"] == 1
    assert counts["executed_volume"] == 40


def test_replace_via_market():
    mkt = Market()
    mkt.apply(m.OrderAdded(ns=1, order_number=1, side="S", qty=100,
                           orderbook_id="9984", group="DAY", price=200))
    mkt.apply(m.OrderReplaced(ns=2, orig_order_number=1, new_order_number=2,
                              qty=50, price=210))
    assert mkt.books.orders[2].side == "S"
    assert mkt.books.books["9984"].ask_levels() == [(210, 50)]


def test_end_of_snapshot_handled_gracefully():
    mkt = Market()
    result = mkt.apply(m.EndOfSnapshot(sequence_number=424242))
    assert result is None
    assert mkt.end_of_snapshot_seq == 424242
    assert mkt.counters()["by_type"] == {"G": 1}


def test_unknown_message_type_counted_not_raised():
    mkt = Market()
    assert mkt.apply(("not", "a", "message")) is None
    assert mkt.unknown_count == 1
    assert mkt.counters()["unknown"] == 1


def test_no_price_reference_routed():
    mkt = Market()
    mkt.apply(m.OrderAdded(ns=1, order_number=0, side="B", qty=0,
                           orderbook_id="8306", group="DAY",
                           price=types.NO_PRICE))
    assert mkt.refdata.instruments["8306"].reference_price == types.NO_PRICE


@needs_full_sample
def test_full_sample_replay_through_market_only():
    """Replay the whole official UDP sample through Market.apply alone,
    re-asserting the T5.1a-c pinned outcomes."""
    mkt = Market(tape_max_entries=100)
    for raw in itchfile.read_file(FULL_SAMPLE):
        mkt.apply(codec.decode(raw))

    counters = mkt.counters()
    # T3.2 golden type counts, now via Market's own counting.
    assert dict(counters["by_type"]) == {
        "A": 128366, "E": 67902, "D": 10772, "U": 9287,
        "T": 5843, "Y": 16, "S": 2, "H": 1,
    }
    assert counters["messages"] == 222189
    assert counters["unknown"] == 0

    # T5.1b pins.
    assert counters["collisions"] == 0
    assert counters["orphan_executes"] == 0
    assert counters["orphan_deletes"] == 0
    assert counters["orphan_replaces"] == 0
    assert counters["executions"] == 67902
    assert counters["executed_volume"] == 6516729
    assert counters["live_orders"] == 50736
    assert counters["books"] == 144

    # T5.1c pins through the tape.
    assert mkt.tape.trade_count == 67902
    assert mkt.tape.total_volume == 6516729
    assert len(mkt.tape.entries) == 100

    # T5.1a: the capture starts mid-session (seq 12562, no R/L), so every
    # instrument was auto-created with directory_missing set.
    assert counters["instruments"] > 0
    assert all(inst.directory_missing
               for inst in mkt.refdata.instruments.values())
    # And the session clock advanced with the 5843 T messages.
    assert mkt.seconds > 0
