import pytest

from jnxfeed import types


def test_price_sentinels():
    assert types.NO_PRICE == 0x7FFFFFFF
    assert types.MAX_PRICE == 0x7FFFFFFE
    assert types.is_no_price(types.NO_PRICE)
    assert not types.is_no_price(types.MAX_PRICE)
    assert not types.is_no_price(0)


def test_price_to_str():
    assert types.price_to_str(12345) == "1234.5"
    assert types.price_to_str(10) == "1.0"
    assert types.price_to_str(7) == "0.7"
    assert types.price_to_str(0) == "0.0"
    # Spec's stated maximum representable value.
    assert types.price_to_str(types.MAX_PRICE) == "214748364.6"
    assert types.price_to_str(types.NO_PRICE) == "-"


def test_price_from_str_roundtrip():
    for raw in (0, 7, 10, 12345, types.MAX_PRICE, types.NO_PRICE):
        assert types.price_from_str(types.price_to_str(raw)) == raw


def test_price_from_str_whole_number():
    assert types.price_from_str("1234") == 12340
    assert types.price_from_str(" 1234.5 ") == 12345


def test_price_from_str_rejects_garbage():
    for bad in ("", "abc", "1.23", "1.", "-5.0", "1,0"):
        with pytest.raises(ValueError):
            types.price_from_str(bad)


def test_sides():
    assert types.BUY == "B"
    assert types.SELL == "S"
    assert types.SIDES == ("B", "S")


def test_groups():
    assert types.GROUPS == ("DAY", "NGHT", "DAYX", "DAYU")
    # Every group has a display name.
    assert sorted(types.GROUP_NAMES) == sorted(types.GROUPS)


def test_absence_semantics_defaults():
    # Plan section 3.3(4): absent from trading-state spin => suspended;
    # absent from short-sell spin => unrestricted.
    assert types.DEFAULT_TRADING_STATE == types.TRADING_STATE_SUSPENDED
    assert types.DEFAULT_SHORT_SELL_STATE == types.SHORT_SELL_UNRESTRICTED


def test_system_event_codes():
    events = (
        types.EVENT_START_OF_MESSAGES,
        types.EVENT_START_OF_SYSTEM_HOURS,
        types.EVENT_START_OF_MARKET_HOURS,
        types.EVENT_END_OF_MARKET_HOURS,
        types.EVENT_END_OF_SYSTEM_HOURS,
        types.EVENT_END_OF_MESSAGES,
    )
    assert events == ("O", "S", "Q", "M", "E", "C")
    assert len(set(events)) == len(events)
