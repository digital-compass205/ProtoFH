"""F6 integration: restart & recovery end-to-end proof.

Drives tools/run_e2e.py's scenarios against the HEAD fixture (paced, so
kills land mid-stream deterministically -- see run_e2e.poll_progress,
which triggers on OBSERVED jnxdb STATS progress, never a bare sleep) and
asserts the §1 restart-matrix invariants hold: kill anything mid-day and
the final DB state converges to the same uninterrupted-baseline state.

    a. kill_fh        SIGKILL jnxfh mid-stream, restart: resumes via
                       GET_STATE recovery at last_seq+1, zero dups, final
                       state identical to baseline.
    b. kill_db        SIGKILL jnxdb mid-stream, restart 2s later: jnxfh
                       keeps multicasting throughout (mcast gap-free),
                       resyncs on DB return, final state identical.
    c. drop_exchange  simulator scripts an abrupt ITCH disconnect
                       mid-stream: Soup resume, zero dups, pub_seq
                       gap-free, final state identical.
    d. glimpse_cold   cold start bootstrapping from a mid-fixture GLIMPSE
                       cut: orders/books/static identical to a full
                       replay; trades EXPECTEDLY differ by exactly the
                       pre-cut trade history (proven against an exact
                       computed reference, not just "documented").
    e. kill_both      SIGKILL jnxfh AND jnxdb mid-stream, restart DB then
                       FH: DB comes up empty so FH cold-bootstraps
                       (replay from seq 1 -- this simulator tolerates a
                       fresh connection re-requesting the whole session,
                       so replay reproduces the baseline exactly,
                       INCLUDING trade history, unlike glimpse_cold).

All five scenarios run on tests/fixtures/sample_udp_head.itch (2000
messages), each under 60s. See run_e2e.compare_dump_dirs / normalize_csv
for exactly what is excluded from the "identical" comparison and why
(EXCLUDED_STATS_FIELDS, SECTION_DROP_COLUMNS, SECTIONS_SKIP_EMPTY_TICKER
docstrings) -- short version: process-restart bookkeeping (epoch, sync
counters) and recency stamps a resync legitimately refreshes wholesale
(last_exch_seq/last_update_ns/last_system_event per row, and
last_trade_ns), plus system-wide pseudo-rows with an empty ticker column
(same exclusion tools/compare_db_dump.py already makes).

A full (non-head) fixture baseline-vs-book_dump comparison is already
covered by tests/integration/test_jnxfh.py; this file intentionally
stays on the head fixture for speed. See RECOVERY.md for the operator
runbook these scenarios are proving.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
JNXDB = os.path.join(REPO_ROOT, "cpp", "build", "jnxdb")
JNXFH = os.path.join(REPO_ROOT, "cpp", "build", "jnxfh")
BOOK_DUMP = os.path.join(REPO_ROOT, "cpp", "build", "book_dump")
FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures",
                       "sample_udp_head.itch")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(JNXDB) and os.path.exists(JNXFH) and
        os.path.exists(BOOK_DUMP)),
    reason="C++ binaries not built (make -C cpp all)",
)

sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
import run_e2e  # noqa: E402

PACED_SPEED = 300.0  # msgs/sec: ~6.7s for the 2000-msg head fixture


@pytest.fixture(scope="module")
def total_msgs():
    return run_e2e.fixture_msg_count(FIXTURE)


@pytest.fixture(scope="module")
def baseline_dump(tmp_path_factory, total_msgs):
    """One uninterrupted replay-bootstrap run of the head fixture, used
    as the reference every restart scenario is compared against."""
    out_dir = str(tmp_path_factory.mktemp("baseline"))
    runner = run_e2e.Runner(FIXTURE, out_dir, "max")
    try:
        ok, dest = run_e2e.run_baseline(runner, total_msgs)
    finally:
        runner.teardown()
    assert ok, "baseline run itself failed:\n" + "\n".join(runner.evidence)
    return dest


def _run_scenario(tmp_path, fn, total_msgs, speed=PACED_SPEED):
    out_dir = str(tmp_path)
    runner = run_e2e.Runner(FIXTURE, out_dir, speed)
    try:
        result = fn(runner, total_msgs)
    finally:
        runner.teardown()
    return result, runner.evidence


# --------------------------------------------------------------------------
# a. FH killed mid-stream
# --------------------------------------------------------------------------

def test_kill_fh_resumes_at_last_seq_plus_one(tmp_path, total_msgs,
                                              baseline_dump):
    (ok, dump_dir), evidence = _run_scenario(
        tmp_path, run_e2e.run_kill_fh, total_msgs)
    evidence_text = "\n".join(evidence)

    assert ok, evidence_text
    resume_lines = [e for e in evidence if "resume mode:" in e]
    assert resume_lines, "no resume-mode evidence:\n" + evidence_text
    assert "resumed seq=" in "\n".join(evidence)

    same, problems = run_e2e.compare_dump_dirs(dump_dir, baseline_dump)
    assert same, "final state diverged from baseline:\n" + \
        "\n".join(problems) + "\n\nrun evidence:\n" + evidence_text


# --------------------------------------------------------------------------
# b. DB killed mid-stream
# --------------------------------------------------------------------------

def test_kill_db_fh_stays_live_and_resyncs(tmp_path, total_msgs,
                                           baseline_dump):
    (ok, dump_dir), evidence = _run_scenario(
        tmp_path, run_e2e.run_kill_db, total_msgs)
    evidence_text = "\n".join(evidence)

    assert ok, evidence_text
    assert any("syncs_completed=" in e and "want >=1" in e
              for e in evidence)
    assert any("gaps=0" in e for e in evidence), \
        "expected mcast to stay gap-free through the DB outage:\n" + \
        evidence_text

    same, problems = run_e2e.compare_dump_dirs(dump_dir, baseline_dump)
    assert same, "final state diverged from baseline:\n" + \
        "\n".join(problems) + "\n\nrun evidence:\n" + evidence_text


# --------------------------------------------------------------------------
# c. exchange connection dropped mid-stream
# --------------------------------------------------------------------------

def test_drop_exchange_resumes_no_dups(tmp_path, total_msgs, baseline_dump):
    (ok, dump_dir), evidence = _run_scenario(
        tmp_path, run_e2e.run_drop_exchange, total_msgs)
    evidence_text = "\n".join(evidence)

    assert ok, evidence_text
    assert any("dups_dropped=0" in e for e in evidence)
    assert any("gaps=0" in e for e in evidence), \
        "pub_seq must be contiguous across the resume:\n" + evidence_text

    same, problems = run_e2e.compare_dump_dirs(dump_dir, baseline_dump)
    assert same, "final state diverged from baseline:\n" + \
        "\n".join(problems) + "\n\nrun evidence:\n" + evidence_text


# --------------------------------------------------------------------------
# d. cold start with GLIMPSE bootstrap from a mid-fixture cut
# --------------------------------------------------------------------------

def test_glimpse_cold_bootstrap(tmp_path, total_msgs):
    out_dir = str(tmp_path)
    runner = run_e2e.Runner(FIXTURE, out_dir, "max")
    try:
        ok, dump_dir, expected_trades = run_e2e.run_glimpse_cold(
            runner, total_msgs)
        evidence = list(runner.evidence)
    finally:
        runner.teardown()
    evidence_text = "\n".join(evidence)

    assert ok, evidence_text
    # orders/books/static identical to a full from-seq-1 replay.
    assert any("orders=OK books=OK static=OK" in e for e in evidence), \
        evidence_text
    # trades EXPECTEDLY differ from the full-fixture reference (the
    # snapshot carries no trade history)...
    assert any("trades=MISMATCH" in e and "EXPECTED" in e
              for e in evidence)
    # ...but match an EXACT computed post-cut reference (not just "some
    # trades exist somewhere" -- every ticker's trade_count/cum_qty/
    # cum_turnover/last_price/last_qty/last_match_number for messages
    # after the cut, accounting for post-cut trades against pre-cut
    # resting orders the snapshot restored).
    assert any("post-cut trades vs exact expected delta" in e and
              "): OK" in e for e in evidence), evidence_text
    assert len(expected_trades) > 0, \
        "test is vacuous if nothing traded after the cut"


# --------------------------------------------------------------------------
# e. FH AND DB both killed mid-stream
# --------------------------------------------------------------------------

def test_kill_both_cold_bootstraps_to_identical_state(tmp_path, total_msgs,
                                                       baseline_dump):
    (ok, dump_dir), evidence = _run_scenario(
        tmp_path, run_e2e.run_kill_both, total_msgs)
    evidence_text = "\n".join(evidence)

    assert ok, evidence_text

    # Unlike glimpse_cold, replay-from-1 bootstrap reproduces the
    # baseline EXACTLY, trades included (no documented-difference
    # carve-out needed here).
    same, problems = run_e2e.compare_dump_dirs(dump_dir, baseline_dump)
    assert same, "final state diverged from baseline:\n" + \
        "\n".join(problems) + "\n\nrun evidence:\n" + evidence_text
