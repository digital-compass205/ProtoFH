"""Tests for jnxfeed.book.refdata (JNX_PLAN.md T5.1a)."""
from jnxfeed import types
from jnxfeed.book import refdata as rd
from jnxfeed.itch import messages as m


def make_directory(book="8306", group="DAY", tick_table_id=1):
    return m.OrderbookDirectory(
        ns=1, orderbook_id=book, isin="JP0000000000", group=group,
        round_lot=100, tick_table_id=tick_table_id, price_decimals=1,
        upper_limit=20000, lower_limit=10000,
    )


# --- directory + auto-create ---------------------------------------------

def test_directory_message_populates_instrument():
    store = rd.RefData()
    assert store.apply(make_directory()) is True
    inst = store.instruments["8306"]
    assert inst.isin == "JP0000000000"
    assert inst.group == "DAY"
    assert inst.round_lot == 100
    assert inst.tick_table_id == 1
    assert inst.price_decimals == 1
    assert inst.upper_limit == 20000
    assert inst.lower_limit == 10000
    assert inst.directory_missing is False


def test_auto_create_on_first_reference_flags_directory_missing():
    store = rd.RefData()
    # A trading-state message for a book never announced by `R` --
    # exactly what a mid-session join looks like (plan 3.3(5)).
    store.apply(m.TradingState(ns=1, orderbook_id="9984", group="DAY", state="T"))
    inst = store.instruments["9984"]
    assert inst.directory_missing is True
    assert inst.group == "DAY"      # learned from the H message
    assert inst.trading_state == "T"
    # A later directory message clears the flag.
    store.apply(make_directory(book="9984"))
    assert inst.directory_missing is False


def test_get_auto_creates():
    store = rd.RefData()
    inst = store.get("7203")
    assert inst.directory_missing is True
    assert store.get("7203") is inst  # same record on re-lookup


# --- spin / absence semantics ------------------------------------------------

def test_absence_semantics_defaults():
    """Plan 3.3(4): absent from the H spin => suspended; absent from the
    Y spin => unrestricted."""
    store = rd.RefData()
    store.apply(make_directory())
    inst = store.instruments["8306"]
    assert inst.trading_state == types.DEFAULT_TRADING_STATE
    assert inst.trading_state == types.TRADING_STATE_SUSPENDED
    assert inst.short_sell_state == types.DEFAULT_SHORT_SELL_STATE
    assert inst.short_sell_state == types.SHORT_SELL_UNRESTRICTED


def test_spin_updates_states():
    store = rd.RefData()
    store.apply(make_directory())
    store.apply(m.TradingState(ns=2, orderbook_id="8306", group="DAY", state="T"))
    store.apply(m.ShortSellRestriction(ns=3, orderbook_id="8306", group="DAY", state="1"))
    inst = store.instruments["8306"]
    assert inst.trading_state == types.TRADING_STATE_TRADING
    assert inst.short_sell_state == types.SHORT_SELL_RESTRICTED
    # And back.
    store.apply(m.TradingState(ns=4, orderbook_id="8306", group="DAY", state="V"))
    store.apply(m.ShortSellRestriction(ns=5, orderbook_id="8306", group="DAY", state="0"))
    assert inst.trading_state == types.TRADING_STATE_SUSPENDED
    assert inst.short_sell_state == types.SHORT_SELL_UNRESTRICTED


# --- reference price --------------------------------------------------------

def test_reference_price_a_message_updates_refdata():
    store = rd.RefData()
    store.apply(make_directory())
    ref = m.OrderAdded(ns=1, order_number=0, side="B", qty=0,
                       orderbook_id="8306", group="DAY", price=15005)
    assert rd.RefData.is_reference_price(ref) is True
    assert store.apply(ref) is True
    assert store.instruments["8306"].reference_price == 15005


def test_reference_price_no_price_sentinel():
    store = rd.RefData()
    ref = m.OrderAdded(ns=1, order_number=0, side="B", qty=0,
                       orderbook_id="8306", group="DAY", price=types.NO_PRICE)
    store.apply(ref)
    inst = store.instruments["8306"]
    assert inst.reference_price == types.NO_PRICE
    assert types.is_no_price(inst.reference_price)


def test_mid_session_reference_price_update_overwrites():
    store = rd.RefData()
    store.apply(m.OrderAdded(ns=1, order_number=0, side="B", qty=0,
                             orderbook_id="8306", group="DAY", price=15000))
    store.apply(m.OrderAdded(ns=2, order_number=0, side="B", qty=0,
                             orderbook_id="8306", group="DAY", price=15100))
    assert store.instruments["8306"].reference_price == 15100


def test_real_order_a_message_is_not_consumed():
    store = rd.RefData()
    order = m.OrderAdded(ns=1, order_number=42, side="B", qty=100,
                         orderbook_id="8306", group="DAY", price=15000)
    assert rd.RefData.is_reference_price(order) is False
    assert store.apply(order) is False
    # It must not create an instrument or touch the reference price.
    assert "8306" not in store.instruments


# --- tick tables --------------------------------------------------------------

def test_tick_table_assembly_and_lookup():
    store = rd.RefData()
    # Rows delivered out of order on purpose.
    store.apply(m.PriceTickSize(ns=1, tick_table_id=1, tick_size=10, price_start=10000))
    store.apply(m.PriceTickSize(ns=2, tick_table_id=1, tick_size=1, price_start=0))
    store.apply(m.PriceTickSize(ns=3, tick_table_id=1, tick_size=5, price_start=5000))
    table = store.tick_tables[1]
    assert table.rows() == [(0, 1), (5000, 5), (10000, 10)]
    assert table.tick_size(0) == 1
    assert table.tick_size(4999) == 1
    assert table.tick_size(5000) == 5
    assert table.tick_size(9999) == 5
    assert table.tick_size(10000) == 10
    assert table.tick_size(10 ** 9) == 10


def test_tick_size_per_instrument():
    store = rd.RefData()
    store.apply(make_directory(tick_table_id=7))
    store.apply(m.PriceTickSize(ns=1, tick_table_id=7, tick_size=2, price_start=0))
    store.apply(m.PriceTickSize(ns=2, tick_table_id=7, tick_size=4, price_start=1000))
    assert store.tick_size("8306", 500) == 2
    assert store.tick_size("8306", 1500) == 4
    assert store.tick_size("unknown", 500) is None


def test_tick_table_duplicate_start_replaces():
    store = rd.RefData()
    store.apply(m.PriceTickSize(ns=1, tick_table_id=1, tick_size=5, price_start=100))
    store.apply(m.PriceTickSize(ns=2, tick_table_id=1, tick_size=7, price_start=100))
    assert store.tick_tables[1].rows() == [(100, 7)]


# --- system events ---------------------------------------------------------------

def test_system_events_recorded_in_order():
    store = rd.RefData()
    store.apply(m.SystemEvent(ns=1, group="", event="O"))
    store.apply(m.SystemEvent(ns=2, group="DAY", event="Q"))
    assert store.system_events == [(1, "", "O"), (2, "DAY", "Q")]


def test_non_refdata_messages_not_consumed():
    store = rd.RefData()
    assert store.apply(m.OrderExecuted(ns=1, order_number=5, executed_qty=10,
                                       match_number=1)) is False
    assert store.apply(m.OrderDeleted(ns=1, order_number=5)) is False
    assert store.apply(m.TimestampSeconds(seconds=34200)) is False
