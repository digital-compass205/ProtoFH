"""Reactor glue for a SoupBinTCP client (JNX_PLAN.md T4.1).

``TcpSoupConnector`` owns a non-blocking TCP socket driven by
``jnxfeed.net.reactor.Reactor``, feeds received bytes into a
``jnxfeed.soup.session.SoupSession``, flushes its pending output, drives
a periodic tick timer (heartbeats / peer-silence detection), and
implements reconnect-with-bounded-backoff, resuming the session at its
maintained (session_id, next_seq) resume point. All protocol decisions
(what a byte stream means, when to heartbeat, seq counting) live in
``session.py``; this module only wires sockets to that state machine.
"""
import os
import socket
import time

from jnxfeed.soup import session as soup_session

STATE_IDLE = "IDLE"
STATE_CONNECTING = "CONNECTING"
STATE_CONNECTED = "CONNECTED"
STATE_STOPPED = "STOPPED"

_RECV_CHUNK = 65536


def _noop(*args, **kwargs):
    pass


class TcpSoupConnector(object):
    """Drives one ``SoupSession`` over a reconnecting TCP socket.

    Callbacks (optional, default no-op), beyond whatever the session
    itself was constructed with:
    - ``on_connect_failed(exc)`` -- a connect attempt failed (will retry
      unless retries are exhausted)
    - ``on_give_up()`` -- retries exhausted; the connector has stopped
    - ``on_disconnect()`` -- the TCP connection dropped (will retry,
      unless the session already reached ENDED/FAILED)
    """

    def __init__(self, reactor, host, port, session,
                 tick_interval=0.25,
                 max_retries=None,
                 backoff_initial=0.5, backoff_max=10.0, backoff_factor=2.0,
                 connect_timeout=10.0,
                 on_connect_failed=None, on_give_up=None, on_disconnect=None):
        self.reactor = reactor
        self.host = host
        self.port = port
        self.session = session
        self.tick_interval = tick_interval
        self.max_retries = max_retries
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.backoff_factor = backoff_factor
        self.connect_timeout = connect_timeout

        self.on_connect_failed = on_connect_failed or _noop
        self.on_give_up = on_give_up or _noop
        self.on_disconnect = on_disconnect or _noop

        self.state = STATE_IDLE
        self.attempts = 0
        self._backoff = backoff_initial

        self._sock = None
        self._send_buf = bytearray()
        self._tick_handle = None
        self._connect_deadline_handle = None
        self._retry_handle = None

    # -- public control -------------------------------------------------------

    def start(self):
        """Begin connecting (and reconnecting on failure/drop)."""
        if self.state == STATE_STOPPED:
            self.state = STATE_IDLE
        self._attempt_connect()

    def stop(self):
        """Stop for good: cancel timers, close the socket, no more
        reconnects."""
        self.state = STATE_STOPPED
        self._cancel_timers()
        self._close_socket()

    def flush(self):
        """Push any bytes the session has queued (e.g. a logout) to the
        socket now, without waiting for the next tick."""
        if self.state == STATE_CONNECTED:
            self._flush_send()

    def reconnect(self, exc=None):
        """Force a disconnect/reconnect cycle (e.g. on peer silence)."""
        if self.state != STATE_CONNECTED:
            return
        self._handle_disconnect(exc)

    # -- connecting -------------------------------------------------------------

    def _attempt_connect(self):
        if self.state == STATE_STOPPED:
            return
        self.attempts += 1
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._sock = sock
        self.state = STATE_CONNECTING
        try:
            sock.connect((self.host, self.port))
        except BlockingIOError:
            pass
        except OSError as exc:
            self._handle_connect_error(exc)
            return
        self.reactor.register_write(sock, self._on_connect_writable)
        self._connect_deadline_handle = self.reactor.call_later(
            self.connect_timeout, self._on_connect_timeout
        )

    def _on_connect_timeout(self):
        if self.state != STATE_CONNECTING:
            return
        self._handle_connect_error(OSError("connect timed out"))

    def _on_connect_writable(self):
        if self.state != STATE_CONNECTING:
            return
        sock = self._sock
        err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        self.reactor.unregister_write(sock)
        if self._connect_deadline_handle is not None:
            self._connect_deadline_handle.cancel()
            self._connect_deadline_handle = None
        if err != 0:
            self._handle_connect_error(
                OSError(err, "connect failed: {}".format(os.strerror(err)))
            )
            return
        self._on_connected()

    def _handle_connect_error(self, exc):
        self._close_socket()
        self.on_connect_failed(exc)
        self._schedule_retry()

    def _on_connected(self):
        self.state = STATE_CONNECTED
        # Fresh backoff/attempt bookkeeping resets only once data actually
        # flows (see _on_readable); reaching CONNECTED alone is not enough
        # evidence the peer is healthy.
        self.session.reset()
        self.session.start(time.monotonic())
        self.reactor.register_read(self._sock, self._on_readable)
        self._flush_send()
        self._schedule_tick()

    # -- receiving -------------------------------------------------------------

    def _on_readable(self):
        sock = self._sock
        if sock is None:
            return
        try:
            data = sock.recv(_RECV_CHUNK)
        except BlockingIOError:
            return
        except OSError as exc:
            self._handle_disconnect(exc)
            return
        if not data:
            self._handle_disconnect(None)
            return
        self.session.on_bytes(data, time.monotonic())
        if self.session.state == soup_session.STATE_LIVE:
            # A successful login proves the peer is healthy: a future
            # reconnect cycle starts fresh rather than inheriting backoff
            # accumulated from earlier, unrelated failures.
            self._backoff = self.backoff_initial
            self.attempts = 0
        self._flush_send()
        self._check_terminal()

    def _check_terminal(self):
        if self.session.state == soup_session.STATE_ENDED:
            self.stop()
        elif self.session.state == soup_session.STATE_FAILED:
            # Login was rejected -- a protocol-level failure, not a
            # transient network issue. Don't retry.
            self.stop()

    # -- sending -------------------------------------------------------------------

    def _flush_send(self):
        data = self.session.pending_output()
        if data:
            self._send_buf += data
        self._try_send()

    def _try_send(self):
        if not self._send_buf:
            if self._sock is not None:
                self.reactor.unregister_write(self._sock)
            return
        sock = self._sock
        if sock is None:
            return
        try:
            n = sock.send(bytes(self._send_buf))
        except BlockingIOError:
            self.reactor.register_write(sock, self._try_send)
            return
        except OSError as exc:
            self._handle_disconnect(exc)
            return
        del self._send_buf[:n]
        if self._send_buf:
            self.reactor.register_write(sock, self._try_send)
        else:
            self.reactor.unregister_write(sock)

    # -- ticking -------------------------------------------------------------------

    def _schedule_tick(self):
        self._tick_handle = self.reactor.call_later(self.tick_interval, self._on_tick)

    def _on_tick(self):
        if self.state != STATE_CONNECTED:
            return
        self.session.on_tick(time.monotonic())
        self._flush_send()
        self._check_terminal()
        if self.state == STATE_CONNECTED:
            self._schedule_tick()

    # -- disconnect / retry -------------------------------------------------------

    def _handle_disconnect(self, exc):
        self._close_socket()
        if self.state == STATE_STOPPED:
            return
        self.on_disconnect(exc)
        self._schedule_retry()

    def _schedule_retry(self):
        if self.state == STATE_STOPPED:
            return
        if self.max_retries is not None and self.attempts >= self.max_retries:
            self.state = STATE_STOPPED
            self.on_give_up()
            return
        self.state = STATE_IDLE
        delay = self._backoff
        self._backoff = min(self._backoff * self.backoff_factor, self.backoff_max)
        self._retry_handle = self.reactor.call_later(delay, self._attempt_connect)

    # -- cleanup -------------------------------------------------------------------

    def _cancel_timers(self):
        if self._tick_handle is not None:
            self._tick_handle.cancel()
            self._tick_handle = None
        if self._connect_deadline_handle is not None:
            self._connect_deadline_handle.cancel()
            self._connect_deadline_handle = None
        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None

    def _close_socket(self):
        self._cancel_timers()
        sock = self._sock
        if sock is not None:
            self.reactor.unregister(sock)
            try:
                sock.close()
            except OSError:
                pass
        self._sock = None
        self._send_buf = bytearray()
