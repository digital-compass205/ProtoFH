"""Tests for the Short Sell Price (SSP) computation: compute_ssp()
(jnxfeed.book.refdata) and the zero/plus/minus tick classification
maintained in TradeTape.record() (jnxfeed.book.tape.BookStats.uptick).
Scenarios mirror the worked examples in the approved plan
(JNX_Short_Selling_Rules_2.00.pdf) and cpp/test/test_shortsell.cpp.
"""
from jnxfeed import types
from jnxfeed.book import orderbook as ob
from jnxfeed.book import refdata as rd
from jnxfeed.book import tape as tape_mod
from jnxfeed.book.market import Market
from jnxfeed.itch import messages as m


def execution(book="8306", price=15000, qty=100, match=1, side="S",
             group="DAY"):
    return ob.Execution(orderbook_id=book, group=group, side=side,
                        price=price, qty=qty, match_number=match)


# --- compute_ssp: pure-function coverage -----------------------------------

def test_ssp_unrestricted_is_always_zero():
    table = rd.TickTable(1)
    table.add(0, 1)
    assert rd.compute_ssp("0", 1000, 990, True, table) == 0
    assert rd.compute_ssp(None, 1000, 990, False, table) == 0
    assert rd.compute_ssp("?", 1000, 990, False, None) == 0


def test_ssp_restricted_from_open_no_trade_yet():
    # Plan example 2: restricted before any trade -- LTP assumed = base
    # price, uptick defaults False (never an uptick) -> SSP = BP + 1 tick.
    table = rd.TickTable(1)
    table.add(0, 1)
    assert rd.compute_ssp("1", 1000, None, False, table) == 1001


def test_ssp_restricted_no_base_price_no_trade_is_indeterminate():
    table = rd.TickTable(1)
    table.add(0, 1)
    assert rd.compute_ssp("1", None, None, False, table) == types.NO_PRICE


def test_ssp_restricted_uptick_is_ltp_itself():
    table = rd.TickTable(1)
    table.add(0, 1)
    assert rd.compute_ssp("1", 1000, 896, True, table) == 896


def test_ssp_restricted_not_uptick_is_ltp_plus_tick():
    # Plan example 3: circuit breaker trips on a down move (minus tick).
    table = rd.TickTable(1)
    table.add(0, 1)
    assert rd.compute_ssp("1", 1000, 895, False, table) == 896


def test_ssp_restricted_unknown_tick_size_is_indeterminate():
    empty = rd.TickTable(1)
    assert rd.compute_ssp("1", 1000, 895, False, empty) == types.NO_PRICE
    assert rd.compute_ssp("1", 1000, 895, False, None) == types.NO_PRICE


def test_ssp_tick_size_boundary():
    # Plan example 7: tick size changes at the 3000 price band.
    table = rd.TickTable(1)
    table.add(0, 1)
    table.add(3000, 5)
    assert rd.compute_ssp("1", 3000, 2999, False, table) == 3000
    assert rd.compute_ssp("1", 3000, 3000, False, table) == 3005


# --- TradeTape.record: zero/plus/minus tick classification ------------------

def test_tape_first_trade_flat_to_base_price_is_not_uptick():
    tape = tape_mod.TradeTape()
    tape.record(execution(price=1000), 0, base_price=1000)  # flat vs. base
    stats = tape.book_stats("8306")
    assert stats.last_price == 1000
    assert stats.uptick is False


def test_tape_first_trade_above_base_price_is_uptick():
    tape = tape_mod.TradeTape()
    tape.record(execution(price=1010), 0, base_price=1000)
    assert tape.book_stats("8306").uptick is True


def test_tape_repeated_price_is_zero_tick_and_does_not_flip_classification():
    # Plan example 4: 895 (minus tick) -> 896 (plus tick, uptick=True) ->
    # 896 again (zero tick: uptick STAYS True, does not reset to False).
    tape = tape_mod.TradeTape()

    tape.record(execution(price=990, match=1), 0, base_price=1000)
    assert tape.book_stats("8306").uptick is False

    tape.record(execution(price=970, match=2), 0, base_price=1000)
    assert tape.book_stats("8306").uptick is False

    tape.record(execution(price=895, match=3), 0, base_price=1000)
    assert tape.book_stats("8306").uptick is False

    tape.record(execution(price=896, match=4), 0, base_price=1000)
    assert tape.book_stats("8306").uptick is True
    assert tape.book_stats("8306").last_price == 896

    tape.record(execution(price=896, match=5), 0, base_price=1000)  # zero tick
    assert tape.book_stats("8306").uptick is True  # unchanged, still True
    assert tape.book_stats("8306").last_price == 896

    tape.record(execution(price=900, match=6), 0, base_price=1000)
    assert tape.book_stats("8306").uptick is True
    assert tape.book_stats("8306").last_price == 900


def test_tape_minus_tick_after_uptick_flips_classification():
    tape = tape_mod.TradeTape()
    tape.record(execution(price=896, match=1), 0, base_price=1000)  # minus
    tape.record(execution(price=900, match=2), 0, base_price=1000)  # plus
    assert tape.book_stats("8306").uptick is True
    tape.record(execution(price=894, match=3), 0, base_price=1000)  # minus
    assert tape.book_stats("8306").uptick is False
    assert tape.book_stats("8306").last_price == 894


# --- end-to-end through Market.apply ----------------------------------------

def _trade_at(mkt, order_number, match_number, price, book="8306",
              group="DAY"):
    """One resting sell order fully executed at `price` -- mirrors the
    C++ test's trade_at() helper."""
    mkt.apply(m.OrderAdded(ns=1, order_number=order_number, side="S",
                           qty=100, orderbook_id=book, group=group,
                           price=price))
    mkt.apply(m.OrderExecuted(ns=2, order_number=order_number,
                              executed_qty=100, match_number=match_number))


def test_market_ssp_end_to_end_restricted_from_open():
    # Plan example 2: JNX marks the book restricted before any trade has
    # happened today. No BookStats row exists yet for this ticker at all.
    mkt = Market()
    mkt.refdata.tick_table(1).add(0, 1)
    mkt.apply(m.OrderAdded(ns=1, order_number=0, side="B", qty=0,
                           orderbook_id="8306", group="DAY", price=1000))
    mkt.apply(m.ShortSellRestriction(ns=2, orderbook_id="8306", group="DAY",
                                     state="1"))

    inst = mkt.refdata.instruments["8306"]
    assert mkt.tape.book_stats("8306") is None
    ssp = rd.compute_ssp(inst.short_sell_state, inst.reference_price,
                         None, False, mkt.refdata.tick_tables[1])
    assert ssp == 1001  # base price 1000, not-uptick default -> +1 tick


def test_market_ssp_end_to_end_circuit_breaker_trip_and_recovery():
    mkt = Market()
    mkt.refdata.tick_table(1).add(0, 1)
    mkt.apply(m.OrderAdded(ns=1, order_number=0, side="B", qty=0,
                           orderbook_id="8306", group="DAY", price=1000))
    mkt.apply(m.OrderbookDirectory(
        ns=2, orderbook_id="8306", isin="JP0000000000", group="DAY",
        round_lot=100, tick_table_id=1, price_decimals=1,
        upper_limit=20000, lower_limit=10000))

    _trade_at(mkt, 101, 1, 990)  # minus tick
    _trade_at(mkt, 102, 2, 970)  # minus tick
    _trade_at(mkt, 103, 3, 895)  # minus tick, CB trips here
    mkt.apply(m.ShortSellRestriction(ns=3, orderbook_id="8306", group="DAY",
                                     state="1"))

    def ssp():
        inst = mkt.refdata.instruments["8306"]
        stats = mkt.tape.book_stats("8306")
        return rd.compute_ssp(inst.short_sell_state, inst.reference_price,
                              stats.last_price, stats.uptick,
                              mkt.refdata.tick_tables[1])

    assert ssp() == 896

    # A plus tick to 896, then a repeated 896 print (zero tick: SSP must
    # stay at 896, not drift to 897).
    _trade_at(mkt, 104, 4, 896)
    assert ssp() == 896
    _trade_at(mkt, 105, 5, 896)
    assert ssp() == 896

    # Restriction lifted: SSP reports 0 immediately regardless of tape state.
    mkt.apply(m.ShortSellRestriction(ns=4, orderbook_id="8306", group="DAY",
                                     state="0"))
    assert ssp() == 0
