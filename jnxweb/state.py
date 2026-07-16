"""jnxweb in-memory state: latest UPDATE per ticker, trade rings, stats.

Single-threaded (same reactor thread as everything else in jnxweb) --
no locking needed. ``State`` owns:

- ``tickers``: ticker -> latest decoded UPDATE dict (jnxweb.records
  shape).
- a per-ticker ring of the last 50 trades, appended only when
  ``trigger == 'E'`` (a trade-triggering exchange message).
- global stats: total updates applied, pub_seq gap count, bad
  datagram count, last epoch seen, restart count.

Epoch handling: every UPDATE carries the FH's `epoch` (bumped on a
cold/warm restart with a fresh session -- see JNX_PLAN2.md §1 restart
matrix). If the epoch changes mid-session, jnxweb has no way to know
whether old per-ticker state is still valid, so on an epoch change
`State` clears everything and calls the `on_restart` callback so the
caller can push a "feed restarted" event to connected browsers.

pub_seq gaps are tracked globally (pub_seq is the FH's global publish
sequence across all tickers, not per-ticker) -- mirrors the exact
gap-counting logic in tools/mcast_spy.py's --stats mode, so the two
tools agree on what counts as a gap.
"""
import time
from collections import OrderedDict, deque

TRADE_RING_SIZE = 50


def _noop_ticker(ticker):
    pass


def _noop_restart(epoch):
    pass


class State(object):
    def __init__(self, on_ticker_update=None, on_restart=None):
        self.tickers = {}
        self._trades = {}
        self.updates = 0
        self.bad = 0
        self.gaps = 0
        self.restarts = 0
        self.snapshots = 0
        self.snapshot_rows = 0
        self.last_epoch = None
        self.last_restart_ts = None
        self.start_ts = time.time()
        self._expected_pub_seq = None
        self.on_ticker_update = on_ticker_update or _noop_ticker
        self.on_restart = on_restart or _noop_restart

    # -- ingestion --------------------------------------------------------

    def record_bad(self):
        """Count a datagram that failed to decode or wasn't an UPDATE."""
        self.bad += 1

    def apply_update(self, rec):
        """Apply one decoded UPDATE dict (jnxweb.records shape)."""
        epoch = rec["epoch"]
        if self.last_epoch is not None and epoch != self.last_epoch:
            self._clear_all()
            self.restarts += 1
            self.last_restart_ts = time.time()
            self.on_restart(epoch)
        if epoch != self.last_epoch:
            self.last_epoch = epoch
            self._expected_pub_seq = None

        seq = rec["pub_seq"]
        if self._expected_pub_seq is not None and seq > self._expected_pub_seq:
            self.gaps += seq - self._expected_pub_seq
        self._expected_pub_seq = seq + 1

        ticker = rec["ticker"]
        self.tickers[ticker] = rec
        if rec["trigger"] == "E":
            ring = self._trades.get(ticker)
            if ring is None:
                ring = deque(maxlen=TRADE_RING_SIZE)
                self._trades[ticker] = ring
            ring.append(OrderedDict([
                ("exch_seq", rec["exch_seq"]),
                ("exch_ns", rec["exch_ns"]),
                ("price", rec["last_price"]),
                ("qty", rec["last_qty"]),
            ]))
        self.updates += 1
        self.on_ticker_update(ticker)

    def merge_snapshot(self, rows, snap_epoch):
        """Seed state from a jnxdb SNAP snapshot without regressing live data.

        `rows` are decoded UPDATE dicts (identical shape to the multicast
        path -- they ARE binary UPDATE records, base64'd over the query
        port), each carrying the DB row's `exch_seq` in its envelope.
        `snap_epoch` is the DB's epoch from the SNAP header (equal to every
        row's own `epoch`).

        Runs on the reactor thread, so it is atomic w.r.t. `apply_update`;
        no locking. Reconciliation:

        - Epoch first. The canonical position is `(epoch, exch_seq)`;
          `pub_seq` is per-epoch-local (the DB stores 0 for dump rows) and
          is never compared here. If live is a NEWER incarnation than the
          snapshot (snap_epoch < live), the snapshot is stale -> discard.
          If the snapshot is newer (snap_epoch > live), we started around a
          feed-handler restart the live feed hasn't caught up to -> treat
          like a restart (clear + adopt), then merge.
        - Then per-ticker last-writer-wins by `exch_seq`: insert if the
          ticker is unknown, overwrite only if the snapshot row is strictly
          newer than the cached one, else keep the (fresher) live row.

        Returns the number of rows actually merged (inserted/overwritten).
        """
        if self.last_epoch is not None and snap_epoch < self.last_epoch:
            # Snapshot is from an older FH incarnation than the live feed we
            # are already tracking -- live is authoritative and newer.
            return 0
        if self.last_epoch is not None and snap_epoch > self.last_epoch:
            # DB is a newer incarnation than any UDP we've applied.
            self._clear_all()
            self.restarts += 1
            self.last_restart_ts = time.time()
            self.last_epoch = snap_epoch
            self._expected_pub_seq = None
            self.on_restart(snap_epoch)
        elif self.last_epoch is None:
            # No live data yet -- adopt the snapshot's epoch as the working
            # one; the next live UDP re-anchors pub_seq gap counting.
            self.last_epoch = snap_epoch
            self._expected_pub_seq = None

        merged = 0
        for rec in rows:
            ticker = rec["ticker"]
            cur = self.tickers.get(ticker)
            if cur is not None and cur["exch_seq"] >= rec["exch_seq"]:
                continue  # live row is as-fresh-or-fresher; keep it
            self.tickers[ticker] = rec
            self._seed_trade(ticker, rec)
            merged += 1
            self.on_ticker_update(ticker)
        self.snapshots += 1
        self.snapshot_rows += merged
        return merged

    def _seed_trade(self, ticker, rec):
        """Seed a single 'last trade' ring entry from a snapshot row's trade
        summary (SNAP carries the summary, not the 50-deep tape). Skipped
        when the ticker has never traded, or when a live ring already holds
        this or a newer trade for it."""
        if not rec.get("last_trade_ns") or not rec.get("last_qty"):
            return
        ring = self._trades.get(ticker)
        if ring:
            newest = ring[-1]
            if newest.get("exch_seq", 0) >= rec["exch_seq"]:
                return
        else:
            ring = deque(maxlen=TRADE_RING_SIZE)
            self._trades[ticker] = ring
        ring.append(OrderedDict([
            ("exch_seq", rec["exch_seq"]),
            ("exch_ns", rec["exch_ns"]),
            ("price", rec["last_price"]),
            ("qty", rec["last_qty"]),
        ]))

    def _clear_all(self):
        self.tickers.clear()
        self._trades.clear()

    # -- queries ------------------------------------------------------------

    def ticker_list(self):
        """Sorted list of tickers with at least one known UPDATE."""
        return sorted(self.tickers.keys())

    def snapshot(self, ticker):
        """Full JSON-able state for `ticker`, or None if unknown."""
        rec = self.tickers.get(ticker)
        if rec is None:
            return None
        out = OrderedDict(rec)
        ring = self._trades.get(ticker)
        # newest first, per the plan's UI spec.
        out["trades"] = list(reversed(ring)) if ring else []
        return out

    def stats(self):
        return OrderedDict([
            ("updates", self.updates),
            ("bad", self.bad),
            ("gaps", self.gaps),
            ("restarts", self.restarts),
            ("snapshots", self.snapshots),
            ("snapshot_rows", self.snapshot_rows),
            ("last_epoch", self.last_epoch),
            ("tickers", len(self.tickers)),
            ("uptime", time.time() - self.start_ts),
        ])
