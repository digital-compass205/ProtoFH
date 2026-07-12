# Short Sell Price (SSP)

This document explains the Short Sell Price (SSP) field: what it means,
the regulation behind it, how this codebase computes it, and worked
examples covering the notable edge cases. For the exact wire byte layout
see `docs/wire_spec.md` (State/T2 section, `short_sell_price`).

## Background

Japannext PTS enforces Japan's short-sale price restriction (the "uptick
rule") on every order book. Japannext's own ITCH/GLIMPSE feed tells
consumers only **whether** a restriction is currently in effect — the `Y`
"Short Selling Price Restriction State" message carries a single flag:
`0` = none, `1` = in effect (`jnxfeed.types.SHORT_SELL_UNRESTRICTED` /
`SHORT_SELL_RESTRICTED`, and `Instrument.short_sell_state` /
`short_sell_restriction` throughout this codebase). Japannext never
transmits the actual **price** a short sell order must respect.

SSP fills that gap: it is a value **computed by this FH**, published
alongside the existing restriction flag, giving downstream consumers the
minimum price at which a short sell order is currently accepted —
`0` when there is no restriction in effect.

Source: `JNX_Short_Selling_Rules_2.00.pdf` ("Short Selling Rules", Japannext
Co., Ltd., effective 2013-11-05 per the FSA's August 2013 announcement).

## The regulation

- **Uptick rule**: "A security is on an uptick if its last traded price is
  higher than its previous price. At the beginning of the trading day, the
  last traded price is assumed to be the base price." "On an uptick, short
  sell orders can be placed only at or above the last traded price. In
  other cases, short sell orders can be placed only above the last traded
  price."
- **Circuit breaker**: "A short sell circuit breaker is tripped when the
  security price falls to or below a threshold of 10% below the base
  price." Before the trip, no uptick restriction is in effect. After the
  trip, the uptick rule activates.
- **Session carryover**: "If the circuit breaker is tripped during the
  Nighttime Session, it remains tripped throughout the following Daytime
  Session." "At the start of each Nighttime Session, if short selling
  price restrictions are to be in effect at the primary exchange (i.e.,
  Tokyo Stock Exchange), the circuit breaker is tripped immediately."

Japannext itself performs the 10%-threshold circuit-breaker determination
and reports the result via `Y`; this codebase does not reimplement that
detection. It only computes the resulting minimum order price once told a
restriction is active.

## Definitions

All of the following are scoped **per order book** — i.e. per
(ticker, group). `DAY`/`NGHT`/`DAYX`/`DAYU` are separate order books with
independent reference prices and trade tapes in this codebase, so each has
its own SSP.

| Term | Meaning | Where it lives |
|------|---------|-----------------|
| BP (base price) | The order book's reference price, from the reference-price `A` message (`order_number == 0`) | `Instrument.reference_price` |
| restricted | Japannext's own restriction flag, taken as-is | `Instrument.short_sell_state` / `short_sell_restriction` |
| LTP | This order book's own last traded price (its own tape, not the primary market's) | `BookStats.last_price` |
| uptick | A persistent classification updated only on a genuine price change (see below) | `BookStats.uptick` |
| tick(price) | The tick size in effect at `price` | `TickTable` |

## SSP calculation

```
if not restricted:
    SSP = 0
else:
    SSP = LTP            if uptick
        = LTP + tick(LTP)  otherwise   # smallest price strictly above LTP
```

`LTP` is assumed to equal `BP` before this book's first trade of the day
(the rule's own "beginning of the trading day" clause).

If SSP cannot be computed — the book is restricted but no base price is
known yet and no trade has happened (e.g. right after a mid-session
GLIMPSE join, before the first live trade), or the tick size at LTP is
unknown — SSP reports the `NO_PRICE` sentinel (`0x7FFFFFFF` /
`214,748,364.7`), **not** `0`. This lets a consumer distinguish "restricted,
price not yet known" from "not restricted" (`0`).

### The uptick classification: a "tick test", not a per-trade comparison

`uptick` only changes on a trade whose price actually **differs** from the
book's current last traded price — a genuine "tick":

- a higher print is a **plus tick** → `uptick = True`
- a lower print is a **minus tick** → `uptick = False`
- a print at the *same* price ("**zero tick**") changes nothing — `uptick`
  keeps whatever value the last real price move gave it

Before any trade (or price move) has ever happened, `uptick` defaults to
`False` — the "beginning of trading day" state is itself a flat, non-uptick
state.

This is the standard zero/plus/minus-tick methodology used for short-sale
price tests (the same style as the historical NYSE/SEC uptick rule): a
repeated print rides along on whatever classification the last genuine
price move established. It neither creates nor cancels an uptick.

Implementation: `BookStats.uptick` (`cpp/market/tape.h` /
`jnxfeed/book/tape.py`), updated in `TradeTape::record()` /
`TradeTape.record()` on every trade. The pure calculation itself is
`compute_ssp()` in `cpp/market/refdata.{h,cpp}` /
`jnxfeed/book/refdata.py`.

## Worked examples

All prices in yen, tick size 1 yen unless noted; base price (BP) = 1,000.

### 1. No restriction all day

`restricted` stays `0` all session. **SSP = 0** throughout, regardless of
trading activity.

### 2. Restricted from the open (inherited from the primary exchange)

Japannext marks the book restricted (`Y = 1`) in the pre-open spin, before
any trade has happened on this book today.

LTP defaults to BP = 1,000; `uptick` defaults to `False` (no price change
has ever happened) → **SSP = 1,000 + 1 = 1,001**. Not 1,000 — the
flat/no-trade state is never an uptick.

### 3. Circuit breaker trips intraday on a down move

BP = 1,000, threshold = 900 (10% below BP). Trades so far:

| Trade price | vs. previous | uptick |
|------------:|--------------|:------:|
| 990 | minus tick vs. BP (1,000) | False |
| 970 | minus tick vs. 990 | False |
| 895 | minus tick vs. 970 — Japannext flips `Y` to `1` here | False |

LTP = 895, `uptick = False` → **SSP = 895 + 1 = 896**.

### 4. Chain of trades while restricted, including a repeated price

Continuing from example 3:

| Trade price | Tick | uptick | SSP |
|------------:|------|:------:|----:|
| 896 | plus tick (896 > 895) | True | **896** (sell orders now allowed at-or-above 896 itself) |
| 896 (again) | **zero tick** (896 == 896) | **True, unchanged** | **896**, unchanged — a repeated print never flips the classification |
| 900 | plus tick (900 > 896) | True | **900** |

If instead the trade after the repeated 896 had printed at 894 (a minus
tick relative to 896), `uptick` would flip to `False` and SSP would become
**895** (894 + 1 tick).

### 5. Mid-session GLIMPSE join, restriction already active, no trade history

GLIMPSE snapshots carry order-book state and the reference price, but not
trade history — the true LTP and `uptick` classification are not known
yet. The FH falls back to the start-of-day assumption (`LTP = BP`,
`uptick = False`) until the first live trade re-establishes real tracking.

If BP itself is unknown too (a `directory_missing` book with no reference
price ever seen), SSP cannot be computed at all: **SSP = NO_PRICE**
(`0x7FFFFFFF`), not `0`.

### 6. Nighttime → Daytime carryover

The `NGHT` book for a ticker trips its circuit breaker late in the
Nighttime session; Japannext inherits that into `Y = 1` on the `DAY` book
at Daytime open (per the rule). `DAY` is a distinct order book (distinct
ticker+group key, distinct tape) from `NGHT` in this system: its own
LTP/`uptick` tracking restarts at `DAY`'s own base price (`LTP = BP`,
`uptick = False`), exactly like example 2 — regardless of where `NGHT`'s
own LTP or classification ended up.

### 7. Tick-size boundary

BP = 3,000; tick size below 3,000 is 1 yen, 5 yen at/above 3,000
(illustrative JPX-style tick table).

- Restricted, `uptick = False`, last trade 2,999 → SSP = 2,999 + tick(2,999)
  = 2,999 + 1 = **3,000**.
- Restricted, `uptick = False`, last trade 3,000 → SSP = 3,000 + tick(3,000)
  = 3,000 + 5 = **3,005**.

`tick()` is always evaluated at LTP's own price band.

### 8. Restriction lifted

Japannext stops sending `Y = 1` for this book (typically the next trading
day, with a fresh base price) — `restricted` flips to `0` and
**SSP = 0** immediately, independent of whatever LTP/`uptick` were
tracking.

## Where it appears

- **Multicast / wire format**: `short_sell_price` in the State (T2)
  section of the `U` UPDATE record — `docs/wire_spec.md`.
- **Current-state table**: `short_sell_price` column in jnxdb's T2 `state`
  table, queryable via `GET <ticker>` and `TABLE state`
  (`cpp/db/query.cpp`).
- **CLI**: the `SSP` column in `jnxfeed static` (`jnxfeed/cli/views.py`).
- **Web UI**: the `short_sell_price` field in the operator page snapshot
  (`jnxweb/static_page.py`).

## Known limitations

- **Ticker-only keying**: like the rest of `Instrument`/`BookStats` in
  this codebase, SSP tracking is keyed by ticker alone, not
  (ticker, group). If a ticker were ever concurrently active in two
  groups, its short-sell state could be shared/overwritten between them.
  Pre-existing simplification, not introduced by SSP.
- **No cross-restart persistence of the uptick classification**: an FH
  restart recovers `last_price` from jnxdb but not the `uptick` bit (it is
  not part of the wire/DB schema). A restarted FH conservatively resumes
  at `uptick = False` until the next genuine price move re-establishes
  real tracking — the same degraded-accuracy window as example 5's
  GLIMPSE gap.
- **No day-rollover reset**: nothing in this codebase resets
  `reference_price`, `short_sell_state`, or `uptick`/`last_price` at a new
  trading day's start-of-messages event — the whole prototype assumes an
  FH process spans a single trading day and is restarted externally each
  day, consistent with every other piece of per-instrument state here.
