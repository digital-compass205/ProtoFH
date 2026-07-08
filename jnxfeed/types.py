"""Shared types and constants for the Japannext PTS ITCH feed handler.

All values follow JNX_PLAN.md section 3.1:
- Price fields are unsigned big-endian 4-byte ints with 1 implied decimal
  (a raw value is tenths of a yen).
- 0x7FFFFFFF in a reference-price message means "no reference price".
- Alpha fields arrive space-padded; decoders strip them, so the constants
  here are the stripped forms.

Target runtime is Python 3.6.4 — see plan section 0 for the allowed
language subset.
"""

# --- Price ------------------------------------------------------------

#: Sentinel in reference-price `A` messages: no reference price available.
NO_PRICE = 0x7FFFFFFF

#: Largest valid price value (214,748,364.6).
MAX_PRICE = 0x7FFFFFFE

#: Implied decimal places in every price field (ITCH spec section 3).
PRICE_DECIMALS = 1


def is_no_price(raw):
    """True if a raw price field carries the "no reference price" sentinel."""
    return raw == NO_PRICE


def price_to_str(raw):
    """Render a raw price int as a decimal string ("12345" -> "1234.5").

    The sentinel NO_PRICE renders as "-" so it can never be mistaken for a
    real price in tables or logs.
    """
    if raw == NO_PRICE:
        return "-"
    return "{}.{}".format(raw // 10, raw % 10)


def price_from_str(text):
    """Parse a decimal price string back to the raw int ("1234.5" -> 12345).

    Accepts at most PRICE_DECIMALS fractional digits; "-" maps to NO_PRICE.
    """
    text = text.strip()
    if text == "-":
        return NO_PRICE
    if "." in text:
        whole, frac = text.split(".", 1)
        if len(frac) != PRICE_DECIMALS or not frac.isdigit():
            raise ValueError("bad price string: {!r}".format(text))
    else:
        whole, frac = text, "0"
    if not whole.isdigit():
        raise ValueError("bad price string: {!r}".format(text))
    return int(whole) * 10 + int(frac)


# --- Sides ------------------------------------------------------------

BUY = "B"
SELL = "S"
SIDES = (BUY, SELL)


# --- Order book groups (4-byte Alpha, stripped) ------------------------

GROUP_JMARKET_DAY = "DAY"
GROUP_JMARKET_NIGHT = "NGHT"
GROUP_XMARKET = "DAYX"
GROUP_UMARKET = "DAYU"

GROUPS = (GROUP_JMARKET_DAY, GROUP_JMARKET_NIGHT, GROUP_XMARKET, GROUP_UMARKET)

GROUP_NAMES = {
    GROUP_JMARKET_DAY: "J-Market Daytime Session",
    GROUP_JMARKET_NIGHT: "J-Market Nighttime Session",
    GROUP_XMARKET: "X-Market",
    GROUP_UMARKET: "U-Market",
}


# --- Trading state (`H` message) ---------------------------------------

TRADING_STATE_TRADING = "T"
TRADING_STATE_SUSPENDED = "V"

#: Absence semantics (plan section 3.3(4)): a book absent from the trading
#: state spin is suspended until told otherwise.
DEFAULT_TRADING_STATE = TRADING_STATE_SUSPENDED


# --- Short selling price restriction state (`Y` message) ---------------

SHORT_SELL_UNRESTRICTED = "0"
SHORT_SELL_RESTRICTED = "1"

#: Absence semantics: a book absent from the short-sell spin has no
#: restriction in effect.
DEFAULT_SHORT_SELL_STATE = SHORT_SELL_UNRESTRICTED


# --- System events (`S` message) ---------------------------------------

EVENT_START_OF_MESSAGES = "O"
EVENT_START_OF_SYSTEM_HOURS = "S"
EVENT_START_OF_MARKET_HOURS = "Q"
EVENT_END_OF_MARKET_HOURS = "M"
EVENT_END_OF_SYSTEM_HOURS = "E"
EVENT_END_OF_MESSAGES = "C"
