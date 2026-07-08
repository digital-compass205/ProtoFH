"""T3.2 golden tests: decode the official Japannext samples end-to-end.

Two layers:

- The committed sliced fixture (tests/fixtures/sample_udp_head.itch) is
  validated on every run, with no external dependencies.
- The full official captures are validated when /workspace/jnx-specs is
  present (skipped otherwise, same convention as the T3.1 pcap tests).
  Expected numbers are pinned twice on purpose: hard-coded from
  JNX_PLAN.md's pre-verified ground truth AND cross-checked against the
  committed golden_manifest.json, so a silent regeneration of the
  manifest cannot loosen the test.

Note on GLIMPSE: the official GLIMPSE sample capture is TRUNCATED — it
stops mid-snapshot (1 dangling byte of a split length prefix, client RST)
and therefore contains no `G` End of Snapshot message. `G` decoding is
covered by the codec unit vectors and, later, the T6.1 simulator tests.
"""
import json
import os

import pytest

from jnxfeed import itchfile
from jnxfeed.itch import codec
from jnxfeed.cli import fixtures

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures")
SPECS_DIR = fixtures.DEFAULT_SPECS_DIR

needs_specs = pytest.mark.skipif(
    not os.path.isdir(SPECS_DIR),
    reason="official sample captures not available at {}".format(SPECS_DIR),
)

# Ground truth from JNX_PLAN.md ("Verified ground truth" + T3.2).
UDP_SESSION = "1697659284"
UDP_FIRST_SEQ = 12562
UDP_LAST_SEQ = 234750
UDP_MESSAGE_COUNT = 222189
UDP_TYPE_COUNTS = {
    "A": 128366, "E": 67902, "D": 10772, "U": 9287,
    "T": 5843, "Y": 16, "S": 2, "H": 1,
}


def load_golden():
    with open(os.path.join(FIXTURES_DIR, "golden_manifest.json")) as f:
        return json.load(f)


def test_committed_slice_fixture_decodes_and_matches_manifest():
    with open(os.path.join(FIXTURES_DIR, "sample_udp_head.manifest.json")) as f:
        manifest = json.load(f)

    type_counts = {}
    count = 0
    for message in itchfile.read_file(os.path.join(FIXTURES_DIR, "sample_udp_head.itch")):
        codec.decode(message)  # zero decode errors
        key = chr(message[0])
        type_counts[key] = type_counts.get(key, 0) + 1
        count += 1

    assert count == manifest["message_count"]
    assert type_counts == manifest["type_counts"]
    assert manifest["session"] == UDP_SESSION
    assert manifest["first_seq"] == UDP_FIRST_SEQ
    # The slice is contiguous from the start of the capture.
    assert manifest["last_seq"] == UDP_FIRST_SEQ + count - 1


@needs_specs
def test_udp_sample_golden():
    # scan_udp_sample decodes every message and enforces single-session
    # + contiguous sequence numbers internally (raises SampleError).
    manifest, _ = fixtures.scan_udp_sample(
        os.path.join(SPECS_DIR, fixtures.UDP_SAMPLE_NAME)
    )
    assert manifest["session"] == UDP_SESSION
    assert manifest["first_seq"] == UDP_FIRST_SEQ
    assert manifest["last_seq"] == UDP_LAST_SEQ
    assert manifest["message_count"] == UDP_MESSAGE_COUNT
    assert dict(manifest["type_counts"]) == UDP_TYPE_COUNTS
    # Contiguity arithmetic must close exactly.
    assert UDP_FIRST_SEQ + UDP_MESSAGE_COUNT - 1 == UDP_LAST_SEQ
    # And the committed golden manifest must agree with the recomputation.
    assert json.loads(json.dumps(manifest)) == load_golden()["udp"]


@needs_specs
def test_tcp_sample_golden():
    manifest = fixtures.scan_soup_sample(
        os.path.join(SPECS_DIR, fixtures.TCP_SAMPLE_NAME),
        fixtures.ITCH_TCP_PORT,
    )
    # A Soup session: Login Accepted first (enforced by scan_soup_sample),
    # then sequenced ITCH; every carried message decoded.
    assert manifest["session"] == "1697486488"
    assert manifest["login_sequence"] == 3168301
    assert manifest["message_count"] == 2632
    assert manifest["trailing_bytes"] == 0
    assert set(manifest["type_counts"]) == {"A", "D", "E", "T", "U"}
    assert json.loads(json.dumps(manifest)) == load_golden()["tcp"]


@needs_specs
def test_glimpse_sample_golden():
    manifest = fixtures.scan_soup_sample(
        os.path.join(SPECS_DIR, fixtures.GLIMPSE_SAMPLE_NAME),
        fixtures.GLIMPSE_TCP_PORT,
    )
    # Snapshot semantics: GLIMPSE logs in against the blank current
    # session; the stream is directory spin + trading-state spin + tick
    # sizes + open orders.
    assert manifest["session"] == ""
    assert manifest["login_sequence"] == 1
    assert manifest["message_count"] == 8688
    assert dict(manifest["type_counts"]) == {
        "A": 295, "H": 4181, "L": 13, "R": 4182, "S": 1, "T": 1, "Y": 15,
    }
    # Documented truncation: capture stops mid-frame, so no G arrives.
    assert manifest["trailing_bytes"] == 1
    assert "end_of_snapshot_sequence" not in manifest
    assert json.loads(json.dumps(manifest)) == load_golden()["glimpse"]
