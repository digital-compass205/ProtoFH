"""Minimal single-threaded event loop on ``selectors.DefaultSelector``.

No protocol knowledge and no socket creation live here — this module only
drives whatever sockets/timers are registered with it. Callers (T4.1's
``TcpSoupConnector`` and friends) own sockets, connect/close them, and
register read/write interest plus timers.

Design (JNX_PLAN.md T4.0):
- ``register_read(sock, callback)`` / ``register_write(sock, callback)`` /
  ``unregister_read(sock)`` / ``unregister_write(sock)`` — a socket may be
  registered for read and/or write independently; callbacks take no args.
- ``call_later(delay, fn) -> TimerHandle`` — schedule ``fn`` (no args) to
  run after ``delay`` seconds (monotonic clock). ``TimerHandle.cancel()``
  prevents it from firing; timers fire in deadline order.
- ``run()`` loops until ``stop()`` is called (or no more work is
  registered); ``stop()`` may be called from within a callback.
"""
import heapq
import itertools
import selectors
import time

# Selector event masks re-exported for callers that need them directly.
EVENT_READ = selectors.EVENT_READ
EVENT_WRITE = selectors.EVENT_WRITE


class TimerHandle(object):
    """Handle returned by :meth:`Reactor.call_later`; supports cancel()."""

    __slots__ = ("deadline", "seq", "callback", "cancelled")

    def __init__(self, deadline, seq, callback):
        self.deadline = deadline
        self.seq = seq
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    # Ordering for heapq: earliest deadline first; ``seq`` breaks ties in
    # scheduling order so timers with an identical deadline still fire in
    # the order they were scheduled.
    def __lt__(self, other):
        return (self.deadline, self.seq) < (other.deadline, other.seq)


class _SocketState(object):
    __slots__ = ("read_cb", "write_cb")

    def __init__(self):
        self.read_cb = None
        self.write_cb = None


class Reactor(object):
    """A minimal selectors-based event loop with monotonic timers."""

    def __init__(self):
        self._selector = selectors.DefaultSelector()
        self._states = {}  # fileobj -> _SocketState
        self._timers = []  # heap of TimerHandle
        self._timer_seq = itertools.count()
        self._running = False

    # -- socket readiness --------------------------------------------------

    def register_read(self, sock, callback):
        """Call ``callback()`` (no args) whenever ``sock`` is read-ready."""
        self._set_interest(sock, read_cb=callback)

    def register_write(self, sock, callback):
        """Call ``callback()`` (no args) whenever ``sock`` is write-ready."""
        self._set_interest(sock, write_cb=callback)

    def unregister_read(self, sock):
        self._set_interest(sock, read_cb=False)

    def unregister_write(self, sock):
        self._set_interest(sock, write_cb=False)

    def unregister(self, sock):
        """Drop all interest (read and write) for ``sock``."""
        state = self._states.pop(sock, None)
        if state is None:
            return
        try:
            self._selector.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass

    def _set_interest(self, sock, read_cb=None, write_cb=None):
        # ``False`` means "clear this callback"; ``None`` means "leave as is".
        state = self._states.get(sock)
        if state is None:
            state = _SocketState()
            self._states[sock] = state

        if read_cb is False:
            state.read_cb = None
        elif read_cb is not None:
            state.read_cb = read_cb

        if write_cb is False:
            state.write_cb = None
        elif write_cb is not None:
            state.write_cb = write_cb

        mask = 0
        if state.read_cb is not None:
            mask |= EVENT_READ
        if state.write_cb is not None:
            mask |= EVENT_WRITE

        try:
            self._selector.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass

        if mask == 0:
            self._states.pop(sock, None)
            return

        self._selector.register(sock, mask, state)

    # -- timers ---------------------------------------------------------------

    def call_later(self, delay, fn):
        """Schedule ``fn()`` to run after ``delay`` seconds. Returns a handle
        with a ``.cancel()`` method."""
        deadline = time.monotonic() + delay
        handle = TimerHandle(deadline, next(self._timer_seq), fn)
        heapq.heappush(self._timers, handle)
        return handle

    def _next_timeout(self):
        """Seconds until the next live timer fires, or None (block forever
        if nothing is registered at all)."""
        while self._timers and self._timers[0].cancelled:
            heapq.heappop(self._timers)
        if not self._timers:
            return None
        return max(0.0, self._timers[0].deadline - time.monotonic())

    def _run_due_timers(self):
        now = time.monotonic()
        while self._timers and self._timers[0].deadline <= now:
            handle = heapq.heappop(self._timers)
            if handle.cancelled:
                continue
            handle.callback()
            if not self._running:
                return

    # -- run loop ---------------------------------------------------------------

    def run(self):
        """Run until :meth:`stop` is called."""
        self._running = True
        while self._running:
            self._run_due_timers()
            if not self._running:
                break
            timeout = self._next_timeout()
            events = self._selector.select(timeout)
            if not self._running:
                break
            for key, mask in events:
                state = key.data
                if mask & EVENT_READ and state.read_cb is not None:
                    state.read_cb()
                    if not self._running:
                        break
                if mask & EVENT_WRITE and state.write_cb is not None:
                    state.write_cb()
                    if not self._running:
                        break

    def stop(self):
        """Ask :meth:`run` to exit after the current callback returns."""
        self._running = False

    def close(self):
        self._selector.close()
