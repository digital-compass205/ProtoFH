"""Tests for the T7.1 CLI views (static / tail / book / stats)."""
import io
import os

from jnxfeed import itchfile
from jnxfeed.cli import views
from jnxfeed.itch import codec
from jnxfeed.itch import messages as m
from jnxfeed.sim.exchange import ExchangeSimulator

from soup_stub import StubSoupServer  # noqa: F401  (import path check only)


def fixture_messages():
    """Synthetic session: directory + states + orders + trades on 8306,
    a second book 9984, and a timestamp."""
    return [
        codec.encode(m.TimestampSeconds(seconds=34200)),
        codec.encode(m.OrderbookDirectory(
            ns=1, orderbook_id="8306", isin="JP3902900004", group="DAY",
            round_lot=100, tick_table_id=1, price_decimals=1,
            upper_limit=20000, lower_limit=10000)),
        codec.encode(m.TradingState(ns=2, orderbook_id="8306", group="DAY",
                                    state="T")),
        codec.encode(m.ShortSellRestriction(ns=3, orderbook_id="8306",
                                            group="DAY", state="1")),
        codec.encode(m.OrderAdded(ns=4, order_number=0, side="B", qty=0,
                                  orderbook_id="8306", group="DAY",
                                  price=15005)),
        codec.encode(m.OrderAdded(ns=5, order_number=1, side="B", qty=100,
                                  orderbook_id="8306", group="DAY",
                                  price=15000)),
        codec.encode(m.OrderAdded(ns=6, order_number=2, side="S", qty=80,
                                  orderbook_id="8306", group="DAY",
                                  price=15010)),
        codec.encode(m.OrderAdded(ns=7, order_number=3, side="B", qty=50,
                                  orderbook_id="9984", group="DAY",
                                  price=200)),
        codec.encode(m.OrderExecuted(ns=8, order_number=1, executed_qty=40,
                                     match_number=900)),
    ]


def write_fixture(tmp_path):
    path = str(tmp_path / "view_fixture.itch")
    with itchfile.ItchFileWriter(path) as w:
        for raw in fixture_messages():
            w.write(raw)
    return path


def run_view(main_fn, argv):
    out = io.StringIO()
    code = main_fn(argv, out=out)
    return code, out.getvalue()


# --- static -------------------------------------------------------------------

def test_static_table_golden(tmp_path):
    path = write_fixture(tmp_path)
    code, text = run_view(views.main_static, ["--itch-file", path])
    assert code == 0
    lines = text.splitlines()
    assert lines[0].split() == ["SICC", "ISIN", "Group", "Lot", "TickTbl",
                                "PriceDec", "Lower", "Upper", "State",
                                "SSRestr", "RefPrice"]
    # 8306 fully announced by R; states/ref set by H/Y/ref-A.
    row = [l for l in lines if l.startswith("8306")][0].split()
    assert row == ["8306", "JP3902900004", "DAY", "100", "1", "1",
                   "1000.0", "2000.0", "T", "1", "1500.5"]
    # 9984 exists only as a book (A order): NOT in refdata, so no row.
    assert not [l for l in lines if l.startswith("9984")]
    assert "1 instrument(s)" in text


def test_static_csv_golden(tmp_path):
    path = write_fixture(tmp_path)
    code, text = run_view(views.main_static, ["--itch-file", path, "--csv"])
    assert code == 0
    lines = [l for l in text.splitlines() if l]
    assert lines[0] == ("SICC,ISIN,Group,Lot,TickTbl,PriceDec,Lower,Upper,"
                        "State,SSRestr,RefPrice")
    assert lines[1] == "8306,JP3902900004,DAY,100,1,1,1000.0,2000.0,T,1,1500.5"
    assert len(lines) == 2


def test_static_master_stub_notice(tmp_path):
    path = write_fixture(tmp_path)
    code, text = run_view(views.main_static,
                          ["--itch-file", path, "--master", "whatever.csv"])
    assert code == 0
    assert "not implemented" in text


# --- tail ---------------------------------------------------------------------

def test_tail_limit_golden(tmp_path):
    path = write_fixture(tmp_path)
    code, text = run_view(views.main_tail,
                          ["--itch-file", path, "--limit", "3"])
    assert code == 0
    lines = text.splitlines()
    assert len(lines) == 3
    assert lines[0].split()[0] == "1"           # seq column
    assert "T TimestampSeconds seconds=34200" in lines[0]
    assert "R OrderbookDirectory" in lines[1]
    assert "price=1500.5" in lines[1] or "upper_limit=2000.0" in lines[1]
    assert "H TradingState" in lines[2]


def test_tail_type_and_book_filters(tmp_path):
    path = write_fixture(tmp_path)
    code, text = run_view(views.main_tail,
                          ["--itch-file", path, "--types", "A"])
    assert code == 0
    lines = text.splitlines()
    assert len(lines) == 4  # ref-price A + three real orders
    assert all("OrderAdded" in l for l in lines)

    code, text = run_view(views.main_tail,
                          ["--itch-file", path, "--types", "A",
                           "--book", "9984"])
    assert code == 0
    lines = text.splitlines()
    assert len(lines) == 1
    assert "orderbook_id=9984" in lines[0]


def test_tail_requires_a_source():
    code, text = run_view(views.main_tail, [])
    assert code == 2
    assert "choose exactly one source" in text


# --- book ---------------------------------------------------------------------

def test_book_file_mode_prints_final_state(tmp_path):
    path = write_fixture(tmp_path)
    code, text = run_view(views.main_book,
                          ["8306", "--itch-file", path, "--depth", "3"])
    assert code == 0
    assert "book 8306  state=T" in text
    # After the 40-lot execution the bid shows 60 @ 1500.0, ask 80 @ 1501.0.
    assert "60" in text and "1500.0" in text
    assert "1501.0" in text and "80" in text
    assert "spread=1.0" in text
    assert "bid_qty_total=60  ask_qty_total=80" in text
    assert "traded: 1 fills, volume=40" in text
    assert "match=900" in text
    assert "09:30:00" in text  # 34200s = 09:30, trade timestamp


def test_book_unknown_sicc_is_empty_not_error(tmp_path):
    path = write_fixture(tmp_path)
    code, text = run_view(views.main_book, ["0000", "--itch-file", path])
    assert code == 0
    assert "book 0000" in text
    assert "traded: nothing yet" in text


# --- stats ---------------------------------------------------------------------

def test_stats_file_mode_summary(tmp_path):
    path = write_fixture(tmp_path)
    code, text = run_view(views.main_stats, ["--itch-file", path])
    assert code == 0
    assert "messages=9" in text
    assert "A=4" in text and "E=1" in text and "T=1" in text
    assert "instruments=1  live_orders=3  books=2" in text
    assert "orphans: E=0 D=0 U=0  collisions=0" in text
    assert "trades=1  volume=40" in text
    assert "msgs/s" in text


# --- live plumbing (against the in-process simulator) ----------------------------

def test_tail_live_mode_against_simulator():
    with ExchangeSimulator(messages=fixture_messages()) as sim:
        code, text = run_view(views.main_tail, [
            "--host", "127.0.0.1", "--port", str(sim.itch_port),
            "--user", "TEST", "--pass", "SECRET", "--limit", "4",
        ])
    assert code == 0
    lines = text.splitlines()
    assert len(lines) == 4
    assert "TimestampSeconds" in lines[0]
    assert "OrderbookDirectory" in lines[1]


def test_stats_live_mode_against_simulator():
    with ExchangeSimulator(messages=fixture_messages()) as sim:
        code, text = run_view(views.main_stats, [
            "--host", "127.0.0.1", "--port", str(sim.itch_port),
            "--user", "TEST", "--pass", "SECRET", "--interval", "0.1",
        ])
    assert code == 0
    # Final block after Z: session id + full totals.
    assert "session=SIM0000001" in text
    assert "state=ENDED" in text
    assert "messages=9" in text
    assert "trades=1  volume=40" in text


def test_live_login_reject_reports_failure():
    with ExchangeSimulator(messages=fixture_messages()) as sim:
        code, text = run_view(views.main_tail, [
            "--host", "127.0.0.1", "--port", str(sim.itch_port),
            "--user", "TEST", "--pass", "WRONG",
        ])
    assert code == 3
    assert "live session failed" in text
    assert "login rejected" in text
