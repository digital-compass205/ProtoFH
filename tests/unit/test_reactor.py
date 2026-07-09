"""Tests for jnxfeed.net.reactor (JNX_PLAN.md T4.0)."""
import socket

from jnxfeed.net import reactor as reactor_mod


def test_loopback_socketpair_echo():
    """Data written to one end of a socketpair is echoed back through the
    reactor's read-readiness callback, driven entirely by run()/stop()."""
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    r = reactor_mod.Reactor()
    received = []

    def on_b_readable():
        data = b.recv(4096)
        received.append(data)
        r.stop()

    r.register_read(b, on_b_readable)
    a.sendall(b"hello reactor")
    try:
        r.run()
    finally:
        r.close()
        a.close()
        b.close()

    assert received == [b"hello reactor"]


def test_loopback_echo_both_directions():
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    r = reactor_mod.Reactor()
    seen = []

    def on_a_readable():
        data = a.recv(4096)
        seen.append(("a", data))
        r.stop()

    def on_b_readable():
        data = b.recv(4096)
        seen.append(("b", data))
        b.sendall(b"pong")

    r.register_read(a, on_a_readable)
    r.register_read(b, on_b_readable)
    a.sendall(b"ping")
    try:
        r.run()
    finally:
        r.close()
        a.close()
        b.close()

    assert seen == [("b", b"ping"), ("a", b"pong")]


def test_timer_ordering_out_of_order_schedule():
    r = reactor_mod.Reactor()
    fired = []

    def make_cb(label):
        def cb():
            fired.append(label)
            if len(fired) == 3:
                r.stop()
        return cb

    # Schedule out of deadline order: "third" has the largest delay but is
    # scheduled first; timers must still fire in deadline order.
    r.call_later(0.15, make_cb("third"))
    r.call_later(0.01, make_cb("first"))
    r.call_later(0.08, make_cb("second"))
    try:
        r.run()
    finally:
        r.close()

    assert fired == ["first", "second", "third"]


def test_cancelled_timer_never_fires():
    r = reactor_mod.Reactor()
    fired = []

    handle = r.call_later(0.02, lambda: fired.append("cancelled"))
    handle.cancel()
    r.call_later(0.05, lambda: (fired.append("survivor"), r.stop()))
    try:
        r.run()
    finally:
        r.close()

    assert fired == ["survivor"]


def test_stop_exits_run():
    r = reactor_mod.Reactor()
    calls = []

    def stopper():
        calls.append(1)
        r.stop()

    r.call_later(0.01, stopper)
    # A second timer scheduled after the stop-triggering one must not fire:
    # stop() should exit run() promptly.
    r.call_later(0.02, lambda: calls.append(2))
    try:
        r.run()
    finally:
        r.close()

    assert calls == [1]


def test_unregister_stops_callbacks():
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    r = reactor_mod.Reactor()
    calls = []

    def on_readable():
        calls.append(b.recv(4096))

    r.register_read(b, on_readable)
    r.unregister_read(b)
    a.sendall(b"should not be seen")

    r.call_later(0.03, r.stop)
    try:
        r.run()
    finally:
        r.close()
        a.close()
        b.close()

    assert calls == []
