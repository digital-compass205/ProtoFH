"""Replay benchmark (JNX_PLAN.md T7.2): ``make bench``.

Measures, over the full official UDP sample (222,189 messages, loaded
into memory first so file I/O is excluded):

  (a) decode-only throughput  -- jnxfeed.itch.codec.decode
  (b) decode + Market.apply   -- the full hot path of a live session

Three repetitions each, best (highest msgs/s) reported; a fresh Market
per repetition. The fixture (tests/fixtures/sample_udp.itch, gitignored)
is regenerated from the official pcap when missing, which requires
/workspace/jnx-specs.

Stdlib only; run with the interpreter you care about:
    python3 bench/bench_replay.py
"""
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from jnxfeed import itchfile                       # noqa: E402
from jnxfeed.book.market import Market             # noqa: E402
from jnxfeed.itch import codec                     # noqa: E402

FIXTURE = os.path.join(_REPO_ROOT, "tests", "fixtures", "sample_udp.itch")
SPECS_DIR = "/workspace/jnx-specs"
REPETITIONS = 3


def ensure_fixture():
    if os.path.exists(FIXTURE):
        return
    if not os.path.isdir(SPECS_DIR):
        sys.stderr.write(
            "error: {} is missing and cannot be regenerated because the\n"
            "official sample captures are not available at {}.\n"
            "Obtain the specs/samples, then run:\n"
            "    python3 -m jnxfeed.cli.fixtures\n".format(FIXTURE, SPECS_DIR))
        sys.exit(1)
    sys.stderr.write("regenerating {} from the official pcaps...\n".format(FIXTURE))
    from jnxfeed.cli import fixtures
    fixtures.main([])


def best_rate(fn, messages):
    best = 0.0
    for _rep in range(REPETITIONS):
        start = time.perf_counter()
        fn(messages)
        elapsed = time.perf_counter() - start
        rate = len(messages) / elapsed
        if rate > best:
            best = rate
    return best


def bench_decode(messages):
    decode = codec.decode
    for raw in messages:
        decode(raw)


def bench_decode_apply(messages):
    market = Market()
    apply_msg = market.apply
    decode = codec.decode
    for raw in messages:
        apply_msg(decode(raw))


def main():
    ensure_fixture()
    messages = list(itchfile.read_file(FIXTURE))
    sys.stderr.write("loaded {} messages; {} repetitions each, "
                     "best reported\n".format(len(messages), REPETITIONS))

    rows = [
        ("decode only", best_rate(bench_decode, messages)),
        ("decode + Market.apply", best_rate(bench_decode_apply, messages)),
    ]

    print()
    print("Python {}.{}.{} -- {} messages (official UDP sample)".format(
        sys.version_info[0], sys.version_info[1], sys.version_info[2],
        len(messages)))
    print("{:<24} {:>12} {:>10}".format("stage", "msgs/s", "ms total"))
    print("-" * 48)
    for name, rate in rows:
        print("{:<24} {:>12,.0f} {:>10.1f}".format(
            name, rate, 1000.0 * len(messages) / rate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
