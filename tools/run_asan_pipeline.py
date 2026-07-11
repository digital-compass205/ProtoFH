#!/usr/bin/env python3
"""run_asan_pipeline.py — F8 ASAN/UBSAN gate for the assembled pipeline.

Runs the ``baseline`` scenario from ``run_e2e.py`` (sim -> jnxdb -> jnxfh
over the head fixture) but pointed at ASAN/UBSAN-instrumented jnxdb/jnxfh
binaries (``cpp/build-asan/``), then scans every process log produced by
the run for sanitizer report markers. Exits 0 (and prints "ASAN CLEAN")
only if the scenario itself passed AND no log contains an ASAN/UBSAN
finding. This is the thing `make -C cpp test-asan` shells out to after
running the sanitizer-built unit test binaries.

One-shot tools (itch_replay, book_dump, gen_record_vectors) are not
covered by this script — they're short-lived processes whose allocator
state at exit is irrelevant; LeakSanitizer would only flag genuine
process-lifetime leaks in long-running jnxdb/jnxfh, which is what this
checks by running the full pipeline end to end.

Usage:
    JNXDB_BIN=cpp/build-asan/jnxdb JNXFH_BIN=cpp/build-asan/jnxfh \\
        python3 tools/run_asan_pipeline.py
"""
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import run_e2e  # noqa: E402

FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures", "sample_udp_head.itch")

_SANITIZER_MARKERS = (
    "ERROR: AddressSanitizer",
    "ERROR: LeakSanitizer",
    "runtime error:",  # UBSAN
    "SUMMARY: AddressSanitizer",
    "SUMMARY: UndefinedBehaviorSanitizer",
)


def scan_logs(out_dir):
    """Returns a list of (path, line) findings across every *.log file."""
    findings = []
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".log"):
            continue
        path = os.path.join(out_dir, name)
        with open(path, "r", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                if any(marker in line for marker in _SANITIZER_MARKERS):
                    findings.append("{}:{}: {}".format(
                        name, lineno, line.rstrip()))
    return findings


def main(argv=None):
    jnxdb_bin = os.environ.get(
        "JNXDB_BIN", os.path.join(REPO_ROOT, "cpp", "build-asan", "jnxdb"))
    jnxfh_bin = os.environ.get(
        "JNXFH_BIN", os.path.join(REPO_ROOT, "cpp", "build-asan", "jnxfh"))
    for b in (jnxdb_bin, jnxfh_bin):
        if not os.path.exists(b):
            print("run_asan_pipeline: missing binary {} (build "
                  "cpp/build-asan first)".format(b), file=sys.stderr)
            return 2

    run_e2e.JNXDB = jnxdb_bin
    run_e2e.JNXFH = jnxfh_bin

    out_dir = tempfile.mkdtemp(prefix="jnx-asan-pipeline-")
    fixture = os.path.abspath(FIXTURE)
    total_msgs = run_e2e.fixture_msg_count(fixture)
    runner = run_e2e.Runner(fixture, out_dir, "max")
    # ASAN adds real overhead; give the pipeline generous headroom before
    # declaring it hung (still bounded — this is not an infinite wait).
    try:
        ok, dest = run_e2e.run_baseline(runner, total_msgs)
    finally:
        runner.teardown()

    print("scenario: baseline (asan)")
    print("fixture:", fixture)
    for line in runner.evidence:
        print(" -", line)
    print("results dir:", out_dir)

    findings = scan_logs(out_dir)
    for f in findings:
        print("SANITIZER FINDING:", f)

    passed = ok and not findings
    print("ASAN CLEAN" if passed else "ASAN FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
