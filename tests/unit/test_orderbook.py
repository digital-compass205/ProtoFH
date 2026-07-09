"""Tests for jnxfeed.book.orderbook (JNX_PLAN.md T5.1b)."""
import os
import random

import pytest

from jnxfeed import itchfile
from jnxfeed.book import orderbook as ob
from jnxfeed.itch import codec
from jnxfeed.itch import messages as m

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")
FULL_SAMPLE = os.path.join(FIXTURES_DIR, "sample_udp.itch")

needs_full_sample = pytest.mark.skipif(
    not os.path.exists(FULL_SAMPLE),
    reason="full sample fixture not extracted (python3 -m jnxfeed.cli.fixtures)",
)


def add(store, number, side="B", qty=100, book="8306", group="DAY", price=15000):
    store.apply(m.OrderAdded(ns=1, order_number=number, side=side, qty=qty,
                             orderbook_id=book, group=group, price=price))


# --- A/F insert ----------------------------------------------------------

def test_add_inserts_order_and_level():
    store = ob.OrderBookStore()
    add(store, 1, side="B", qty=100, price=15000)
    add(store, 2, side="B", qty=50, price=15000)   # same level aggregates
    add(store, 3, side="S", qty=70, price=15010)
    assert store.live_order_count() == 3
    book = store.books["8306"]
    assert book.bid_levels() == [(15000, 150)]
    assert book.ask_levels() == [(15010, 70)]


def test_f_message_same_book_handling_as_a():
    store = ob.OrderBookStore()
    store.apply(m.OrderAddedWithAttributes(
        ns=1, order_number=9, side="S", qty=30, orderbook_id="8306",
        group="DAY", price=15020, attribution="", order_type="Q",
    ))
    assert store.orders[9].side == "S"
    assert store.books["8306"].ask_levels() == [(15020, 30)]


def test_ref_price_a_never_reaches_the_book():
    store = ob.OrderBookStore()
    store.apply(m.OrderAdded(ns=1, order_number=0, side="B", qty=0,
                             orderbook_id="8306", group="DAY", price=15000))
    assert store.live_order_count() == 0
    assert store.books == {}
    assert store.ref_price_ignored == 1


def test_level_sorting_bids_desc_asks_asc():
    store = ob.OrderBookStore()
    add(store, 1, side="B", price=14990, qty=10)
    add(store, 2, side="B", price=15010, qty=20)
    add(store, 3, side="B", price=15000, qty=30)
    add(store, 4, side="S", price=15040, qty=40)
    add(store, 5, side="S", price=15020, qty=50)
    book = store.books["8306"]
    assert book.bid_levels() == [(15010, 20), (15000, 30), (14990, 10)]
    assert book.ask_levels() == [(15020, 50), (15040, 40)]
    bids, asks = book.top(2)
    assert bids == [(15010, 20), (15000, 30)]
    assert asks == [(15020, 50), (15040, 40)]
    assert book.best_bid() == (15010, 20)
    assert book.best_ask() == (15020, 50)


# --- E execution -------------------------------------------------------------

def test_execute_reduces_and_reports_passive_price():
    events = []
    store = ob.OrderBookStore(on_execution=events.append)
    add(store, 1, side="B", qty=100, price=15000)
    execution = store.apply(m.OrderExecuted(ns=2, order_number=1,
                                            executed_qty=40, match_number=777))
    assert execution == ob.Execution(orderbook_id="8306", group="DAY",
                                     side="B", price=15000, qty=40,
                                     match_number=777)
    assert events == [execution]
    assert store.orders[1].remaining_qty == 60
    assert store.books["8306"].bid_levels() == [(15000, 60)]
    assert store.executed_volume == 40


def test_cumulative_executions_remove_order_at_zero():
    store = ob.OrderBookStore()
    add(store, 1, qty=100)
    store.apply(m.OrderExecuted(ns=2, order_number=1, executed_qty=60,
                                match_number=1))
    store.apply(m.OrderExecuted(ns=3, order_number=1, executed_qty=40,
                                match_number=2))
    assert 1 not in store.orders
    assert store.books["8306"].bid_levels() == []
    assert store.executed_volume == 100
    assert store.execution_count == 2


def test_orphan_execute_counted_and_ignored():
    store = ob.OrderBookStore()
    result = store.apply(m.OrderExecuted(ns=1, order_number=999,
                                         executed_qty=10, match_number=1))
    assert result is None
    assert store.orphan_executes == 1
    assert store.executed_volume == 0


# --- D delete -----------------------------------------------------------------

def test_delete_removes_order_and_level():
    store = ob.OrderBookStore()
    add(store, 1, qty=100, price=15000)
    add(store, 2, qty=50, price=15000)
    store.apply(m.OrderDeleted(ns=2, order_number=1))
    assert 1 not in store.orders
    assert store.books["8306"].bid_levels() == [(15000, 50)]


def test_orphan_delete_counted():
    store = ob.OrderBookStore()
    store.apply(m.OrderDeleted(ns=1, order_number=999))
    assert store.orphan_deletes == 1


# --- U replace ----------------------------------------------------------------

def test_replace_inherits_book_and_side_takes_new_price_qty():
    store = ob.OrderBookStore()
    add(store, 1, side="S", qty=100, book="9984", group="NGHT", price=15000)
    store.apply(m.OrderReplaced(ns=2, orig_order_number=1, new_order_number=2,
                                qty=80, price=15020))
    assert 1 not in store.orders
    new = store.orders[2]
    assert new.orderbook_id == "9984"
    assert new.group == "NGHT"
    assert new.side == "S"      # inherited
    assert new.price == 15020   # from the message
    assert new.remaining_qty == 80
    book = store.books["9984"]
    assert book.ask_levels() == [(15020, 80)]
    assert book.bid_levels() == []


def test_orphan_replace_counted_and_new_order_not_created():
    store = ob.OrderBookStore()
    store.apply(m.OrderReplaced(ns=1, orig_order_number=999,
                                new_order_number=1000, qty=10, price=100))
    assert store.orphan_replaces == 1
    assert 1000 not in store.orders


# --- collisions -----------------------------------------------------------------

def test_collision_counted_warned_and_replaces(caplog):
    store = ob.OrderBookStore()
    add(store, 1, side="B", qty=100, price=15000)
    import logging
    with caplog.at_level(logging.WARNING, logger="jnxfeed.book.orderbook"):
        add(store, 1, side="S", qty=50, price=15010)
    assert store.collisions == 1
    assert any("collision" in rec.message for rec in caplog.records)
    # Stale record replaced; levels consistent.
    assert store.orders[1].side == "S"
    book = store.books["8306"]
    assert book.bid_levels() == []
    assert book.ask_levels() == [(15010, 50)]


def test_over_execution_clamped_not_negative(caplog):
    store = ob.OrderBookStore()
    add(store, 1, qty=50)
    import logging
    with caplog.at_level(logging.WARNING, logger="jnxfeed.book.orderbook"):
        execution = store.apply(m.OrderExecuted(ns=2, order_number=1,
                                                executed_qty=80, match_number=1))
    assert execution.qty == 50
    assert 1 not in store.orders
    assert store.books["8306"].bid_levels() == []


# --- property test: levels always equal the sum of live orders -----------------

def check_invariant(store):
    """Book level totals must equal the aggregation of live orders."""
    expect = {}  # (book, side, price) -> qty
    for order in store.orders.values():
        key = (order.orderbook_id, order.side, order.price)
        expect[key] = expect.get(key, 0) + order.remaining_qty
    actual = {}
    for book_id, book in store.books.items():
        for price, qty in book.bid_levels():
            actual[(book_id, "B", price)] = qty
        for price, qty in book.ask_levels():
            actual[(book_id, "S", price)] = qty
    assert actual == expect


def test_property_random_stream_levels_match_live_orders():
    rng = random.Random(20260709)
    store = ob.OrderBookStore()
    live = []       # order numbers believed live
    next_number = [1]
    books = ["8306", "9984", "7203"]

    def new_number():
        n = next_number[0]
        next_number[0] += 1
        return n

    for step in range(600):
        op = rng.random()
        if op < 0.45 or not live:
            number = new_number()
            store.apply(m.OrderAdded(
                ns=step, order_number=number,
                side=rng.choice("BS"), qty=rng.randint(1, 500),
                orderbook_id=rng.choice(books), group="DAY",
                price=rng.randrange(14900, 15100, 5),
            ))
            live.append(number)
        elif op < 0.70:
            number = rng.choice(live)
            order = store.orders[number]
            qty = rng.randint(1, order.remaining_qty)
            store.apply(m.OrderExecuted(ns=step, order_number=number,
                                        executed_qty=qty, match_number=step))
            if number not in store.orders:
                live.remove(number)
        elif op < 0.85:
            number = rng.choice(live)
            store.apply(m.OrderDeleted(ns=step, order_number=number))
            live.remove(number)
        else:
            number = rng.choice(live)
            new = new_number()
            store.apply(m.OrderReplaced(ns=step, orig_order_number=number,
                                        new_order_number=new,
                                        qty=rng.randint(1, 500),
                                        price=rng.randrange(14900, 15100, 5)))
            live.remove(number)
            live.append(new)

        if step % 50 == 0:
            check_invariant(store)

    check_invariant(store)
    assert store.collisions == 0
    assert store.orphans_total() == 0


# --- full-sample replay golden numbers -------------------------------------------

@needs_full_sample
def test_full_sample_replay_golden():
    """Replay all 222,189 messages of the official UDP sample.

    The capture starts mid-session at seq 12562 -- but empirically that
    point still precedes all order flow (only spins/timestamps were
    missed), so every E/D/U resolves against a stored order: the pinned
    orphan and collision counts are exactly zero. The volume/live-order
    numbers below were produced by this very replay, inspected once, and
    hard-coded as regression pins.
    """
    store = ob.OrderBookStore()
    n = 0
    for raw in itchfile.read_file(FULL_SAMPLE):
        store.apply(codec.decode(raw))  # zero exceptions over the whole day
        n += 1
    assert n == 222189

    # Pinned orphan counts for pre-capture orders (see docstring).
    assert store.orphan_executes == 0
    assert store.orphan_deletes == 0
    assert store.orphan_replaces == 0
    assert store.collisions == 0
    assert store.ref_price_ignored == 0  # sample contains no ref-price A

    # Pinned execution totals: one Execution per E message.
    assert store.execution_count == 67902
    assert store.executed_volume == 6516729

    # Pinned end-of-capture book state.
    assert store.live_order_count() == 50736
    assert len(store.books) == 144

    check_invariant(store)
