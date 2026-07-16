"""Mid-day bootstrap: seed jnxweb state from jnxdb's ``SNAP`` snapshot.

On startup (and after every feed-handler restart) jnxweb's per-ticker
image would otherwise be empty until each ticker's next multicast UPDATE
-- an idle instrument could stay invisible for a long time. This module
pulls the whole current image from jnxdb over its TCP query port in ONE
round-trip and merges it with the live UDP feed without ever regressing
fresher live data (see ``State.merge_snapshot``).

``SNAP`` reply framing (cpp/db/query.cpp): a header line
``SNAP epoch=<> last_exch_seq=<> session=<> count=<n>`` followed by ``n``
base64-encoded binary UPDATE records (the exact frozen wire format the
multicast feed uses), terminated by a lone ``.`` line -- the same "read
until \\n.\\n" framing ``dbquery_client.OrdersQuery`` relies on. Because
each row is a real UPDATE record, we decode it with ``jnxweb.records`` --
the identical codec used for multicast -- so there is no bespoke parser
and the merged dicts are shape-identical to live ones.

Everything is non-blocking and driven by the shared reactor (JNX_PLAN.md
§0 -- no threads doing I/O): a slow or wedged jnxdb can only stall this
one bootstrap, never the WebSocket hub or the multicast receiver.
"""
import base64
import logging
import os
import socket

from jnxweb import records

log = logging.getLogger("jnxweb.snapshot")

DEFAULT_TIMEOUT = 15.0       # SNAP reply can be a few MiB; localhost is fast.
DEFAULT_RESTART_DEBOUNCE = 1.5   # let jnxdb settle its post-restart RESET+SYNC
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_MAX_ATTEMPTS = 3


class SnapshotQuery(object):
    """One ``SNAP`` round-trip against jnxdb; calls
    ``on_done(rows, snap_epoch, error)`` exactly once from the reactor
    thread. On success ``rows`` is a list of decoded UPDATE dicts and
    ``snap_epoch`` the header epoch; on failure ``rows``/``snap_epoch`` are
    ``None`` and ``error`` is a human string. Mirrors
    ``dbquery_client.OrdersQuery``'s non-blocking connect/send/recv/timeout
    skeleton and its ``\\n.\\n`` completion framing."""

    def __init__(self, reactor, host, port, on_done, timeout=DEFAULT_TIMEOUT):
        self.reactor = reactor
        self.on_done = on_done
        self.recv_buf = bytearray()
        self.send_buf = bytearray(b"SNAP\n")
        self.finished = False
        self.connecting = True
        self.timer = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        try:
            self.sock.connect((host, port))
        except BlockingIOError:
            pass
        except OSError as exc:
            self._finish(error=str(exc))
            return
        self.reactor.register_write(self.sock, self._on_writable)
        self.timer = reactor.call_later(timeout, self._on_timeout)

    def _on_timeout(self):
        self.timer = None
        self._finish(error="timed out contacting jnxdb")

    def _on_writable(self):
        if self.connecting:
            self.connecting = False
            err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err != 0:
                self._finish(error=os.strerror(err))
                return
        if self.send_buf:
            try:
                n = self.sock.send(bytes(self.send_buf))
            except BlockingIOError:
                return
            except OSError as exc:
                self._finish(error=str(exc))
                return
            del self.send_buf[:n]
            if self.send_buf:
                return
        self.reactor.unregister_write(self.sock)
        self.reactor.register_read(self.sock, self._on_readable)

    def _on_readable(self):
        try:
            data = self.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError as exc:
            self._finish(error=str(exc))
            return
        if not data:
            self._finish(error="jnxdb closed the connection before the "
                               "terminator line")
            return
        self.recv_buf += data
        if self.recv_buf.endswith(b"\n.\n") or self.recv_buf == b".\n":
            self._finish(text=bytes(self.recv_buf).decode("ascii", "replace"))

    def cancel(self):
        """Abandon the query silently (no ``on_done``) -- used when a newer
        restart supersedes an in-flight fetch."""
        if self.finished:
            return
        self.finished = True
        self._teardown()

    def _teardown(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.reactor.unregister(self.sock)
        try:
            self.sock.close()
        except OSError:
            pass

    def _finish(self, text=None, error=None):
        if self.finished:
            return
        self.finished = True
        self._teardown()
        if error is not None:
            self.on_done(None, None, error)
            return
        rows, snap_epoch, parse_err = _parse_snap_reply(text)
        self.on_done(rows, snap_epoch, parse_err)


def _parse_header(line):
    """'SNAP epoch=.. last_exch_seq=.. session=.. count=..' -> dict, or None
    if the first token isn't SNAP (e.g. an ERR line)."""
    parts = line.split()
    if not parts or parts[0] != "SNAP":
        return None
    fields = {}
    for tok in parts[1:]:
        key, sep, val = tok.partition("=")
        if sep:
            fields[key] = val
    return fields


def _parse_snap_reply(text):
    """Reply text (terminator stripped) -> (rows, snap_epoch, error)."""
    lines = text.splitlines()
    if lines and lines[-1] == ".":
        lines = lines[:-1]
    if not lines:
        return None, None, "empty reply from jnxdb"
    if lines[0].startswith("ERR"):
        return None, None, "jnxdb rejected SNAP: {}".format(lines[0])
    header = _parse_header(lines[0])
    if header is None:
        return None, None, "malformed SNAP header: {!r}".format(lines[0])
    try:
        snap_epoch = int(header["epoch"])
        count = int(header["count"])
    except (KeyError, ValueError):
        return None, None, "SNAP header missing epoch/count: {!r}".format(
            lines[0])

    rows = []
    for line in lines[1:]:
        if not line:
            continue
        try:
            raw = base64.b64decode(line, validate=True)
            rec = records.decode_record(raw)
        except (ValueError, records.RecordError) as exc:
            return None, None, "bad SNAP row: {}".format(exc)
        if rec.get("kind") != records.KIND_UPDATE:
            return None, None, "SNAP row was not an UPDATE: {!r}".format(
                rec.get("kind"))
        rows.append(rec)

    if len(rows) != count:
        return None, None, "SNAP truncated: got {} rows, header said {}".format(
            len(rows), count)
    return rows, snap_epoch, None


class SnapshotBootstrap(object):
    """Owns the snapshot lifecycle: the initial fetch, the debounced
    re-fetch on every feed restart, single-in-flight + bounded retry, and
    handing the result to ``State.merge_snapshot``.

    Wired in ``__main__``: ``start()`` once at boot, and ``on_restart``
    called from the same epoch-change hook that clears state. Degrades
    gracefully -- any unrecoverable failure just leaves jnxweb live-only,
    exactly as it behaved before this feature."""

    def __init__(self, reactor, state, db_addr,
                 timeout=DEFAULT_TIMEOUT,
                 restart_debounce=DEFAULT_RESTART_DEBOUNCE,
                 retry_delay=DEFAULT_RETRY_DELAY,
                 max_attempts=DEFAULT_MAX_ATTEMPTS):
        self.reactor = reactor
        self.state = state
        self.db_addr = db_addr  # (host, port) or None when disabled
        self.timeout = timeout
        self.restart_debounce = restart_debounce
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts
        self._query = None          # in-flight SnapshotQuery, if any
        self._debounce_timer = None
        self._retry_timer = None
        self._applying = False      # guards against self-triggered re-arm

    def start(self):
        if self.db_addr is None:
            log.info("snapshot bootstrap disabled (no db query addr)")
            return
        self._schedule(expected_epoch=None, attempt=1, delay=0.0)

    def on_restart(self, epoch):
        """A feed-handler restart cleared state -> re-seed idle tickers once
        jnxdb has settled. Ignored while we are applying our own merge (that
        merge may itself fire the restart hook for the snap_epoch>live
        case, and must not recurse into another fetch)."""
        if self.db_addr is None or self._applying:
            return
        self._schedule(expected_epoch=epoch, attempt=1,
                       delay=self.restart_debounce)

    def close(self):
        self._cancel_pending()
        if self._query is not None:
            self._query.cancel()
            self._query = None

    # -- internals --------------------------------------------------------

    def _cancel_pending(self):
        for attr in ("_debounce_timer", "_retry_timer"):
            timer = getattr(self, attr)
            if timer is not None:
                timer.cancel()
                setattr(self, attr, None)

    def _schedule(self, expected_epoch, attempt, delay):
        # A newer request supersedes anything in flight or pending.
        self._cancel_pending()
        if self._query is not None:
            self._query.cancel()
            self._query = None
        fire = lambda: self._begin(expected_epoch, attempt)
        if delay <= 0.0:
            fire()
        else:
            self._debounce_timer = self.reactor.call_later(delay, fire)

    def _begin(self, expected_epoch, attempt):
        self._debounce_timer = None
        host, port = self.db_addr
        log.info("fetching jnxdb snapshot (attempt %d/%d, expected_epoch=%s)",
                 attempt, self.max_attempts, expected_epoch)
        self._query = SnapshotQuery(
            self.reactor, host, port,
            lambda rows, epoch, err: self._on_done(
                rows, epoch, err, expected_epoch, attempt),
            timeout=self.timeout)

    def _on_done(self, rows, snap_epoch, error, expected_epoch, attempt):
        self._query = None
        if error is not None:
            return self._maybe_retry(expected_epoch, attempt,
                                     "snapshot fetch failed: " + error)
        if expected_epoch is not None and snap_epoch != expected_epoch:
            # jnxdb hasn't caught up to the restart yet -- back off and retry.
            return self._maybe_retry(
                expected_epoch, attempt,
                "snapshot epoch {} != expected {} (jnxdb not settled)".format(
                    snap_epoch, expected_epoch))
        self._applying = True
        try:
            merged = self.state.merge_snapshot(rows, snap_epoch)
        finally:
            self._applying = False
        log.info("snapshot merged: %d/%d rows applied (epoch=%s)",
                 merged, len(rows), snap_epoch)

    def _maybe_retry(self, expected_epoch, attempt, why):
        if attempt >= self.max_attempts:
            log.warning("%s; giving up after %d attempts -- running live-only",
                        why, attempt)
            return
        log.warning("%s; retrying in %.1fs", why, self.retry_delay)
        self._retry_timer = self.reactor.call_later(
            self.retry_delay,
            lambda: self._begin_retry(expected_epoch, attempt + 1))

    def _begin_retry(self, expected_epoch, attempt):
        self._retry_timer = None
        self._begin(expected_epoch, attempt)
