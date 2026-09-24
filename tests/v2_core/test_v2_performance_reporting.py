from decimal import Decimal

from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_settlement import baseline, service
from test_v2_intent_admission import database as database_fixture

from services.v2_performance_reporting import performance, signal_funnel

database = database_fixture
closed = closed_fixture


def test_empty_reports_are_explicit_and_do_not_invent_equity(database):
    with database() as conn:
        report = performance(conn)
        assert report["summary"]["trade_intents"] == 0
        assert report["summary"]["win_rate"] is None
        assert report["daily"]["mark_to_market"] is None
        assert signal_funnel(conn) == []


def test_cash_and_trade_totals_match_but_use_event_not_settlement_date(
    database, closed
):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    service(database, income).run_once()
    with database() as conn:
        report = performance(conn)
    assert Decimal(report["summary"]["realized_pnl"]) == Decimal("-10.106")
    assert report["summary"]["settled_trades"] == 1
    assert report["summary"]["open_positions"] == 0
    assert report["daily"]["cash"][0]["day"] == "1970-01-01"
    assert report["daily"]["closed_trades"][0]["day"] == "1970-01-01"
    assert Decimal(report["daily"]["cash"][0]["net_pnl"]) == Decimal("-10.106")


def test_utc8_midnight_not_utc_midnight(database):
    with database() as conn:
        rows = conn.execute("""SELECT (to_timestamp(t/1000.0) AT TIME ZONE 'Asia/Shanghai')::date::text
            FROM unnest(ARRAY[1790265599999::bigint,1790265600000::bigint]) t""").fetchall()
    assert rows == [("2026-09-24",), ("2026-09-25",)]
