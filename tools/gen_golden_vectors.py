#!/usr/bin/env python3
"""Generate cross-language golden byte vectors from the Phase-1 prototype.

Dev-only tool (JNX_PLAN2.md Phase F1 step 1) -- runs on the dev
interpreter (3.14), NOT subject to the 3.6 gate. It imports the proven
Python prototype codec (`jnxfeed.itch`, `jnxfeed.soup`) to build wire
bytes with the prototype ENCODER, re-verifies every vector with the
prototype DECODER (round-trip), and writes flat JSON that the future
C++ test suite (`cpp/test/test_itch.cpp`, `test_soup.cpp`) reads with a
minimal hand-rolled JSON parser -- so the JSON shape here is
deliberately flat: no nested objects/arrays inside "fields".

Usage:
    python3 tools/gen_golden_vectors.py            # write itch.json + soup.json
    python3 tools/gen_golden_vectors.py --check     # regenerate to temp, diff, exit non-zero on mismatch
    python3 tools/gen_golden_vectors.py --verify     # re-decode the committed files, assert fields match

Determinism: vector order, field order, and every value are a pure
function of this script's source (no timestamps, no randomness, no
dict-iteration-order dependence -- output keys are sorted and the
vector list order is fixed by the source below), so running twice
yields byte-identical output files.
"""
import argparse
import json
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from jnxfeed.itch import codec as itch_codec  # noqa: E402
from jnxfeed.itch import messages as itch_messages  # noqa: E402
from jnxfeed.itch import schema as itch_schema  # noqa: E402
from jnxfeed.soup import packets as soup_packets  # noqa: E402
from jnxfeed import types as jnx_types  # noqa: E402

VECTORS_DIR = os.path.join(REPO_ROOT, "cpp", "test", "vectors")
ITCH_JSON_PATH = os.path.join(VECTORS_DIR, "itch.json")
SOUP_JSON_PATH = os.path.join(VECTORS_DIR, "soup.json")

NO_PRICE = jnx_types.NO_PRICE


# --- ITCH vectors ------------------------------------------------------

def _itch_vector(name, msg):
    """Encode `msg` with the prototype encoder, round-trip it through the
    prototype decoder, and return this vector's JSON-ready dict."""
    msg_type = itch_codec._CLASS_TO_TYPE[type(msg)]
    encoded = itch_codec.encode(msg)

    expected_len = itch_schema.total_length(msg_type)
    if len(encoded) != expected_len:
        raise AssertionError(
            "{}: encoded length {} != schema total_length {} for type {}".format(
                name, len(encoded), expected_len, msg_type
            )
        )

    decoded = itch_codec.decode(encoded)
    if decoded != msg:
        raise AssertionError(
            "{}: round-trip mismatch\n  sent:    {!r}\n  decoded: {!r}".format(
                name, msg, decoded
            )
        )

    fields = dict(decoded._asdict())
    return {
        "name": name,
        "type": msg_type,
        "hex": encoded.hex(),
        "fields": fields,
    }


def build_itch_vectors():
    M = itch_messages
    vectors = []

    # T -- Timestamp - Seconds [len 5]
    vectors.append(_itch_vector("t_basic", M.TimestampSeconds(seconds=34200)))
    vectors.append(_itch_vector("t_zero_seconds", M.TimestampSeconds(seconds=0)))
    vectors.append(_itch_vector("t_max_seconds", M.TimestampSeconds(seconds=0xFFFFFFFF)))

    # S -- System Event [len 10]
    vectors.append(_itch_vector(
        "s_start_of_messages", M.SystemEvent(ns=1000, group="DAY", event="O")
    ))
    vectors.append(_itch_vector(
        "s_blank_group_system_wide", M.SystemEvent(ns=2000, group="", event="Q")
    ))
    vectors.append(_itch_vector(
        "s_end_of_messages", M.SystemEvent(ns=3000, group="NGHT", event="C")
    ))

    # L -- Price Tick Size [len 17]
    vectors.append(_itch_vector(
        "l_basic", M.PriceTickSize(ns=100, tick_table_id=1, tick_size=1, price_start=1000)
    ))
    vectors.append(_itch_vector(
        "l_zero_tick_table", M.PriceTickSize(ns=200, tick_table_id=0, tick_size=5, price_start=0)
    ))
    vectors.append(_itch_vector(
        "l_large_price_start",
        M.PriceTickSize(ns=300, tick_table_id=7, tick_size=100, price_start=0x7FFFFFFE),
    ))

    # R -- Orderbook Directory [len 45]
    vectors.append(_itch_vector(
        "r_daytime_market",
        M.OrderbookDirectory(
            ns=400, orderbook_id="8306", isin="JP3902400005", group="DAY",
            round_lot=100, tick_table_id=1, price_decimals=1,
            upper_limit=200000, lower_limit=100000,
        ),
    ))
    vectors.append(_itch_vector(
        "r_night_market",
        M.OrderbookDirectory(
            ns=500, orderbook_id="7203", isin="JP3633400001", group="NGHT",
            round_lot=100, tick_table_id=2, price_decimals=1,
            upper_limit=999999, lower_limit=1,
        ),
    ))
    vectors.append(_itch_vector(
        "r_isin_blank_after_strip",
        M.OrderbookDirectory(
            ns=600, orderbook_id="0001", isin="", group="DAYX",
            round_lot=1, tick_table_id=0, price_decimals=1,
            upper_limit=NO_PRICE, lower_limit=0,
        ),
    ))

    # H -- Trading State [len 14]
    vectors.append(_itch_vector(
        "h_trading", M.TradingState(ns=700, orderbook_id="8306", group="DAY", state="T")
    ))
    vectors.append(_itch_vector(
        "h_suspended", M.TradingState(ns=800, orderbook_id="7203", group="DAY", state="V")
    ))
    vectors.append(_itch_vector(
        "h_unknown_state", M.TradingState(ns=900, orderbook_id="0001", group="DAYU", state="?")
    ))

    # Y -- Short Selling Price Restriction [len 14]
    vectors.append(_itch_vector(
        "y_unrestricted", M.ShortSellRestriction(ns=1000, orderbook_id="8306", group="DAY", state="0")
    ))
    vectors.append(_itch_vector(
        "y_restricted", M.ShortSellRestriction(ns=1100, orderbook_id="7203", group="DAY", state="1")
    ))
    vectors.append(_itch_vector(
        "y_unknown_state", M.ShortSellRestriction(ns=1200, orderbook_id="0001", group="DAYU", state="?")
    ))

    # A -- Order Added [len 30]
    vectors.append(_itch_vector(
        "a_buy_basic",
        M.OrderAdded(ns=1300, order_number=123456789, side="B", qty=100,
                     orderbook_id="8306", group="DAY", price=15000),
    ))
    vectors.append(_itch_vector(
        "a_sell_max_qty",
        M.OrderAdded(ns=1400, order_number=987654321, side="S", qty=0xFFFFFFFF,
                     orderbook_id="7203", group="DAY", price=25000),
    ))
    vectors.append(_itch_vector(
        "a_ref_price_no_price",
        M.OrderAdded(ns=1500, order_number=0, side="B", qty=0,
                     orderbook_id="8306", group="DAY", price=NO_PRICE),
    ))
    vectors.append(_itch_vector(
        "a_ref_price_real_value",
        M.OrderAdded(ns=1600, order_number=0, side="B", qty=0,
                     orderbook_id="8306", group="DAY", price=99999),
    ))

    # F -- Order Added w/ Attributes [len 35]
    vectors.append(_itch_vector(
        "f_dlp_order",
        M.OrderAddedWithAttributes(
            ns=1700, order_number=42, side="B", qty=500,
            orderbook_id="8306", group="DAY", price=15000,
            attribution="ABCD", order_type="Q",
        ),
    ))
    vectors.append(_itch_vector(
        "f_blank_attribution",
        M.OrderAddedWithAttributes(
            ns=1800, order_number=43, side="S", qty=600,
            orderbook_id="7203", group="DAY", price=16000,
            attribution="", order_type="Q",
        ),
    ))
    vectors.append(_itch_vector(
        "f_regular_order_blank_type",
        M.OrderAddedWithAttributes(
            ns=1900, order_number=44, side="B", qty=700,
            orderbook_id="0001", group="DAYU", price=17000,
            attribution="", order_type="",
        ),
    ))

    # E -- Order Executed [len 25]
    vectors.append(_itch_vector(
        "e_basic", M.OrderExecuted(ns=2000, order_number=42, executed_qty=100, match_number=1)
    ))
    vectors.append(_itch_vector(
        "e_large_match_number",
        M.OrderExecuted(ns=2100, order_number=43, executed_qty=1, match_number=0xFFFFFFFFFFFFFFFF),
    ))
    vectors.append(_itch_vector(
        "e_zero_qty_edge",
        M.OrderExecuted(ns=2200, order_number=44, executed_qty=0, match_number=2),
    ))

    # D -- Order Deleted [len 13]
    vectors.append(_itch_vector("d_basic", M.OrderDeleted(ns=2300, order_number=42)))
    vectors.append(_itch_vector("d_small_order_number", M.OrderDeleted(ns=2400, order_number=1)))
    vectors.append(_itch_vector(
        "d_max_order_number", M.OrderDeleted(ns=2500, order_number=0xFFFFFFFFFFFFFFFF)
    ))

    # U -- Order Replaced [len 29]
    vectors.append(_itch_vector(
        "u_basic",
        M.OrderReplaced(ns=2600, orig_order_number=42, new_order_number=4200, qty=200, price=15500),
    ))
    vectors.append(_itch_vector(
        "u_max_qty",
        M.OrderReplaced(ns=2700, orig_order_number=43, new_order_number=4300,
                         qty=0xFFFFFFFF, price=16500),
    ))
    vectors.append(_itch_vector(
        "u_min_price",
        M.OrderReplaced(ns=2800, orig_order_number=44, new_order_number=4400, qty=1, price=1),
    ))

    # G -- End of Snapshot (GLIMPSE only) [len 9]
    vectors.append(_itch_vector("g_zero", M.EndOfSnapshot(sequence_number=0)))
    vectors.append(_itch_vector("g_basic", M.EndOfSnapshot(sequence_number=12562)))
    vectors.append(_itch_vector(
        "g_large_binary_seq", M.EndOfSnapshot(sequence_number=0xFFFFFFFFFFFFFFFF)
    ))

    return vectors


# --- SoupBinTCP vectors -------------------------------------------------

def _soup_vector(name, pkt):
    """Encode `pkt` with the prototype encoder, round-trip it through the
    prototype decoder, and return this vector's JSON-ready dict.

    Packets carrying raw bytes (message/payload) get a hex string field
    instead of raw bytes in "fields" (JSON has no bytes type): `U`/`+`
    become "message_hex"/"payload_hex" from their own field name, and
    `S` (Sequenced Data, wrapping one ITCH message) is specifically
    named "payload_hex" per the spec so C++ test code has one
    unambiguous key for "the inner ITCH message bytes".
    """
    encoded = soup_packets.encode(pkt)
    frame = encoded[2:]  # strip the 2-byte length prefix decode_frame() doesn't want
    decoded = soup_packets.decode_frame(frame)
    if decoded != pkt:
        raise AssertionError(
            "{}: round-trip mismatch\n  sent:    {!r}\n  decoded: {!r}".format(name, pkt, decoded)
        )

    fields = dict(decoded._asdict())
    for key in ("message", "payload"):
        if key in fields and isinstance(fields[key], (bytes, bytearray)):
            fields["{}_hex".format(key)] = bytes(fields[key]).hex()
            del fields[key]

    type_char = encoded[2:3].decode("ascii")
    if type_char == "S" and "message_hex" in fields:
        fields["payload_hex"] = fields.pop("message_hex")

    return {
        "name": name,
        "type": type_char,
        "hex": encoded.hex(),
        "fields": fields,
    }


def build_soup_vectors():
    P = soup_packets
    vectors = []

    # L -- Login Request (client). username/password RIGHT-padded;
    # requested_session/requested_sequence LEFT-padded (JNX_PLAN.md 3.4).
    vectors.append(_soup_vector(
        "login_request_minimal",
        P.LoginRequest(username="", password="", requested_session="", requested_sequence=0),
    ))
    vectors.append(_soup_vector(
        "login_request_typical",
        P.LoginRequest(username="usr", password="pass", requested_session="1697659284",
                        requested_sequence=12562),
    ))
    vectors.append(_soup_vector(
        "login_request_exact_width_fill",
        P.LoginRequest(username="USER12", password="PASSWORD12",
                        requested_session="SESSION123", requested_sequence=18446744073709551615),
    ))

    # A -- Login Accepted (server). session:10 LEFT-padded, sequence:20 LEFT-padded.
    vectors.append(_soup_vector(
        "login_accepted_minimal", P.LoginAccepted(session="1", sequence=1)
    ))
    vectors.append(_soup_vector(
        "login_accepted_typical", P.LoginAccepted(session="1697659284", sequence=12562)
    ))
    vectors.append(_soup_vector(
        "login_accepted_exact_width_fill",
        P.LoginAccepted(session="ABCDEFGHIJ", sequence=18446744073709551615),
    ))

    # J -- Login Rejected (server), both reject codes.
    vectors.append(_soup_vector(
        "login_rejected_not_authorized",
        P.LoginRejected(reject_code=P.REJECT_NOT_AUTHORIZED),
    ))
    vectors.append(_soup_vector(
        "login_rejected_session_unavailable",
        P.LoginRejected(reject_code=P.REJECT_SESSION_UNAVAILABLE),
    ))

    # S -- Sequenced Data (server): one ITCH message per packet.
    inner_t = itch_codec.encode(itch_messages.TimestampSeconds(seconds=34200))
    vectors.append(_soup_vector(
        "sequenced_data_timestamp", P.SequencedData(message=inner_t),
    ))
    inner_a = itch_codec.encode(itch_messages.OrderAdded(
        ns=1300, order_number=123456789, side="B", qty=100,
        orderbook_id="8306", group="DAY", price=15000,
    ))
    vectors.append(_soup_vector(
        "sequenced_data_order_added", P.SequencedData(message=inner_a),
    ))

    # H -- Server Heartbeat (no payload).
    vectors.append(_soup_vector("server_heartbeat", P.ServerHeartbeat()))

    # Z -- End of Session (no payload).
    vectors.append(_soup_vector("end_of_session", P.EndOfSession()))

    # + -- Debug (server, free-form; ignored by clients).
    debug_payload = b"debug: replay caught up to seq 234751"
    vectors.append(_soup_vector("debug_packet", P.DebugPacket(payload=debug_payload)))

    # U -- Unsequenced Data (client; unused for market data).
    unseq_payload = b"unsequenced-test-payload"
    vectors.append(_soup_vector("unsequenced_data", P.UnsequencedData(message=unseq_payload)))

    # R -- Client Heartbeat (no payload).
    vectors.append(_soup_vector("client_heartbeat", P.ClientHeartbeat()))

    # O -- Logout Request (client, no payload).
    vectors.append(_soup_vector("logout_request", P.LogoutRequest()))

    return vectors


# --- I/O -----------------------------------------------------------------

def _dumps(vectors):
    return json.dumps({"vectors": vectors}, indent=2, sort_keys=True) + "\n"


def _write(path, vectors):
    with open(path, "w", encoding="ascii") as f:
        f.write(_dumps(vectors))


def _generate_all():
    itch_vectors = build_itch_vectors()
    soup_vectors = build_soup_vectors()
    return itch_vectors, soup_vectors


def cmd_generate():
    itch_vectors, soup_vectors = _generate_all()
    os.makedirs(VECTORS_DIR, exist_ok=True)
    _write(ITCH_JSON_PATH, itch_vectors)
    _write(SOUP_JSON_PATH, soup_vectors)
    print("wrote {} ITCH vectors -> {}".format(len(itch_vectors), ITCH_JSON_PATH))
    print("wrote {} Soup vectors -> {}".format(len(soup_vectors), SOUP_JSON_PATH))
    return 0


def cmd_check():
    itch_vectors, soup_vectors = _generate_all()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_itch = os.path.join(tmpdir, "itch.json")
        tmp_soup = os.path.join(tmpdir, "soup.json")
        _write(tmp_itch, itch_vectors)
        _write(tmp_soup, soup_vectors)

        ok = True
        for tmp_path, committed_path in ((tmp_itch, ITCH_JSON_PATH), (tmp_soup, SOUP_JSON_PATH)):
            if not os.path.exists(committed_path):
                print("MISMATCH: {} does not exist (run without --check to generate)".format(
                    committed_path
                ))
                ok = False
                continue
            with open(tmp_path, "r", encoding="ascii") as f:
                fresh = f.read()
            with open(committed_path, "r", encoding="ascii") as f:
                committed = f.read()
            if fresh != committed:
                print("MISMATCH: {} differs from freshly generated output".format(committed_path))
                ok = False
            else:
                print("OK: {} matches freshly generated output".format(committed_path))
        return 0 if ok else 1


def cmd_verify():
    """Re-decode every vector in the committed JSON files and assert the
    decoded fields equal the recorded "fields", independent of generation."""
    ok = True

    with open(ITCH_JSON_PATH, "r", encoding="ascii") as f:
        itch_data = json.load(f)
    for v in itch_data["vectors"]:
        raw = bytes.fromhex(v["hex"])
        try:
            decoded = itch_codec.decode(raw)
        except Exception as exc:  # noqa: BLE001 -- report, don't crash the sweep
            print("VERIFY FAIL: itch/{}: decode raised {!r}".format(v["name"], exc))
            ok = False
            continue
        actual = dict(decoded._asdict())
        if actual != v["fields"]:
            print("VERIFY FAIL: itch/{}: fields mismatch\n  recorded: {}\n  decoded:  {}".format(
                v["name"], v["fields"], actual
            ))
            ok = False
        if len(raw) != itch_schema.total_length(v["type"]):
            print("VERIFY FAIL: itch/{}: hex length {} != schema length {} for type {}".format(
                v["name"], len(raw), itch_schema.total_length(v["type"]), v["type"]
            ))
            ok = False

    with open(SOUP_JSON_PATH, "r", encoding="ascii") as f:
        soup_data = json.load(f)
    for v in soup_data["vectors"]:
        raw = bytes.fromhex(v["hex"])
        frame = raw[2:]
        try:
            decoded = soup_packets.decode_frame(frame)
        except Exception as exc:  # noqa: BLE001
            print("VERIFY FAIL: soup/{}: decode raised {!r}".format(v["name"], exc))
            ok = False
            continue
        actual = dict(decoded._asdict())
        for key in ("message", "payload"):
            if key in actual and isinstance(actual[key], (bytes, bytearray)):
                actual["{}_hex".format(key)] = bytes(actual[key]).hex()
                del actual[key]
        if v["type"] == "S" and "message_hex" in actual:
            actual["payload_hex"] = actual.pop("message_hex")
        if actual != v["fields"]:
            print("VERIFY FAIL: soup/{}: fields mismatch\n  recorded: {}\n  decoded:  {}".format(
                v["name"], v["fields"], actual
            ))
            ok = False

    if ok:
        print("VERIFY OK: {} itch vectors + {} soup vectors all re-decode correctly".format(
            len(itch_data["vectors"]), len(soup_data["vectors"])
        ))
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                         help="regenerate to a temp dir and diff against committed files")
    parser.add_argument("--verify", action="store_true",
                         help="re-decode the committed vector files and assert fields match")
    args = parser.parse_args(argv)

    if args.check:
        return cmd_check()
    if args.verify:
        return cmd_verify()
    return cmd_generate()


if __name__ == "__main__":
    sys.exit(main())
