"""Market facade (JNX_PLAN.md T5.1d).

``Market.apply(msg)`` is the single entry point routing every decoded
ITCH message to the right consumer -- refdata (T5.1a), the order book
store (T5.1b) and the trade tape (T5.1c) -- and it is the ONLY API the
CLI views (T7.1) and the FeedHandler (T5.2) use.

Routing rules (plan section 3.3):
- `T` updates the session clock; tape timestamps combine that clock with
  each message's ns field.
- `R`/`L`/`H`/`Y`/`S` and reference-price `A` (order_number == 0) go to
  refdata only.
- Book `A`/`F`/`E`/`D`/`U` go to the order store; each Execution the
  store derives from an `E` is recorded on the tape with the current
  timestamp.
- `G` End of Snapshot (GLIMPSE only) is recorded (``end_of_snapshot_seq``)
  and never an error -- a Market being filled from a snapshot stream sees
  exactly one.

Sans-I/O; this module imports only book/, itch/ and stdlib.
"""
from collections import OrderedDict

from jnxfeed.book.orderbook import OrderBookStore
from jnxfeed.book.refdata import RefData
from jnxfeed.book.tape import DEFAULT_MAX_ENTRIES, TradeTape, make_timestamp
from jnxfeed.itch import messages as m

#: Message class -> ITCH type char (inverse of messages.MESSAGE_CLASSES).
_TYPE_CHARS = dict((cls, char) for char, cls in m.MESSAGE_CLASSES.items())


class Market(object):
    """One coherent market state: refdata + books + tape + counters."""

    def __init__(self, tape_max_entries=DEFAULT_MAX_ENTRIES):
        self.refdata = RefData()
        self.books = OrderBookStore()
        self.tape = TradeTape(max_entries=tape_max_entries)

        #: Last `T` message value: seconds past midnight of the session
        #: start day (0 until the first `T` arrives).
        self.seconds = 0
        #: Sequence number carried by a `G` End of Snapshot, or None.
        self.end_of_snapshot_seq = None

        #: Applied-message counters by ITCH type char, insertion-ordered.
        self.message_counts = OrderedDict()
        #: Messages whose type Market did not recognize (never raises).
        self.unknown_count = 0

    # -- the one entry point ------------------------------------------------

    def apply(self, msg):
        """Apply one decoded ITCH message. Returns the Execution for an
        `E` that matched a stored order, else None. Never raises on
        unknown/unexpected message types."""
        cls = type(msg)
        char = _TYPE_CHARS.get(cls)
        if char is None:
            self.unknown_count += 1
            return None
        self.message_counts[char] = self.message_counts.get(char, 0) + 1

        if cls is m.TimestampSeconds:
            self.seconds = msg.seconds
            return None
        if cls is m.EndOfSnapshot:
            self.end_of_snapshot_seq = msg.sequence_number
            return None
        if cls is m.OrderAdded and msg.order_number == 0:
            # Reference-price pseudo-order: refdata only (plan 3.3(1)).
            self.refdata.apply(msg)
            return None
        if cls in (m.OrderAdded, m.OrderAddedWithAttributes, m.OrderDeleted,
                   m.OrderReplaced):
            self.books.apply(msg)
            return None
        if cls is m.OrderExecuted:
            execution = self.books.apply(msg)
            if execution is not None:
                self.tape.record(execution,
                                 make_timestamp(self.seconds, msg.ns))
            return execution
        # R / L / H / Y / S
        self.refdata.apply(msg)
        return None

    # -- diagnostics ---------------------------------------------------------------

    def counters(self):
        """One flat OrderedDict of everything the stats view needs."""
        counters = OrderedDict()
        counters["messages"] = sum(self.message_counts.values())
        counters["by_type"] = OrderedDict(sorted(self.message_counts.items()))
        counters["unknown"] = self.unknown_count
        counters["instruments"] = len(self.refdata.instruments)
        counters["live_orders"] = self.books.live_order_count()
        counters["books"] = len(self.books.books)
        counters["collisions"] = self.books.collisions
        counters["orphan_executes"] = self.books.orphan_executes
        counters["orphan_deletes"] = self.books.orphan_deletes
        counters["orphan_replaces"] = self.books.orphan_replaces
        counters["executions"] = self.books.execution_count
        counters["executed_volume"] = self.books.executed_volume
        counters["trades_on_tape"] = len(self.tape.entries)
        return counters
