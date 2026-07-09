"""Tests for jnxfeed.book.tape (JNX_PLAN.md T5.1c)."""
import os

import pytest

from jnxfeed import itchfile
from jnxfeed.book import orderbook as ob
from jnxfeed.book import tape as tape_mod
from jnxfeed.itch import codec
from jnxfeed.itch import messages as m

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")
FULL_SAMPLE = os.path.join(FIXTURES_DIR, "sample_udp.itch")

needs_full_sample = pytest.mark.skipif(
    not os.path.exists(FULL_SAMPLE),
    reason="full sample fixture not extracted (python3 -m jnxfeed.cli.fixtures)",
)


def execution(book="8306", price=15000, qty=100, match=1, side="B", group="DAY"):
    return ob.Execution(orderbook_id=book, group=group, side=side,
                        price=price, qty=qty, match_number=match)


def test_timestamp_combination():
    assert tape_mod.make_timestamp(34200, 123456789) == 34200 * 10 ** 9 + 123456789


def test_record_appends_entry_and_updates_stats():
    tape = tape_mod.TradeTape()
    ts = tape_mod.make_timestamp(34200, 500)
    entry = tape.record(execution(price=15000, qty=100, match=77), ts)
    assert entry == tape_mod.TapeEntry(timestamp=ts, orderbook_id="8306",
                                       price=15000, qty=100, match_number=77)
    assert list(tape.entries) == [entry]
    stats = tape.book_stats("8306")
    assert stats.trade_count == 1
    assert stats.volume == 100
    assert stats.last_price == 15000
    assert stats.last_qty == 100
    assert stats.vwap() == 15000.0


def test_multi_fill_order_distinct_matches():
    """Several E for one resting order -- one tape entry per match."""
    events = []
    store = ob.OrderBookStore(on_execution=events.append)
    tape = tape_mod.TradeTape()
    store.apply(m.OrderAdded(ns=1, order_number=1, side="S", qty=300,
                             orderbook_id="8306", group="DAY", price=15020))
    for i, qty in enumerate((100, 150, 50)):
        store.apply(m.OrderExecuted(ns=10 + i, order_number=1,
                                    executed_qty=qty, match_number=1000 + i))
    assert len(events) == 3
    for i, ev in enumerate(events):
        tape.record(ev, tape_mod.make_timestamp(34200, 10 + i))

    assert [e.match_number for e in tape.entries] == [1000, 1001, 1002]
    assert [e.qty for e in tape.entries] == [100, 150, 50]
    # All at the passive order's stored price.
    assert all(e.price == 15020 for e in tape.entries)
    stats = tape.book_stats("8306")
    assert stats.trade_count == 3
    assert stats.volume == 300
    assert stats.vwap() == 15020.0
    assert stats.last_qty == 50
    # The resting order is now fully filled.
    assert 1 not in store.orders


def test_vwap_weighted_correctly():
    tape = tape_mod.TradeTape()
    tape.record(execution(price=100, qty=10, match=1), 0)
    tape.record(execution(price=200, qty=30, match=2), 1)
    stats = tape.book_stats("8306")
    assert stats.vwap() == pytest.approx((100 * 10 + 200 * 30) / 40)
    assert stats.last_price == 200


def test_rolling_bound_keeps_cumulative_stats():
    tape = tape_mod.TradeTape(max_entries=3)
    for i in range(10):
        tape.record(execution(qty=1, match=i), i)
    assert len(tape.entries) == 3
    assert [e.match_number for e in tape.entries] == [7, 8, 9]
    # Cumulative counters unaffected by the rolling window.
    assert tape.trade_count == 10
    assert tape.total_volume == 10
    assert tape.book_stats("8306").volume == 10


def test_recent_filtering():
    tape = tape_mod.TradeTape()
    tape.record(execution(book="8306", match=1), 0)
    tape.record(execution(book="9984", match=2), 1)
    tape.record(execution(book="8306", match=3), 2)
    assert [e.match_number for e in tape.recent()] == [1, 2, 3]
    assert [e.match_number for e in tape.recent(n=2)] == [2, 3]
    assert [e.match_number for e in tape.recent(orderbook_id="8306")] == [1, 3]
    assert tape.book_stats("never-traded") is None


@needs_full_sample
def test_full_sample_tape_totals_match_orderbook_pins():
    """Cross-check the tape's cumulative totals against the volume and
    execution count pinned by the T5.1b replay test."""
    tape = tape_mod.TradeTape(max_entries=100)
    store = ob.OrderBookStore(
        on_execution=lambda ev: tape.record(ev, 0)
    )
    for raw in itchfile.read_file(FULL_SAMPLE):
        store.apply(codec.decode(raw))

    assert tape.trade_count == 67902           # == pinned execution_count
    assert tape.total_volume == 6516729        # == pinned executed_volume
    assert sum(s.volume for s in tape.stats.values()) == 6516729
    assert sum(s.trade_count for s in tape.stats.values()) == 67902
    assert len(tape.entries) == 100            # rolling bound respected
