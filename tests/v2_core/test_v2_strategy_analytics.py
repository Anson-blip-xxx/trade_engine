from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import baseline, service

from services.v2_strategy_analytics import account_analytics, sizing_impact, statistics
from tests.support.v2_admission import database as database_fixture
from v2_core.strategy import StrategyScope

database = database_fixture
closed = closed_fixture


def test_real_settlement_report_keeps_costs_scope_and_evidence(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    assert service(database, income).run_once()["status"] == "CLEAR"
    with database() as c:
        c.execute("SET TRANSACTION READ ONLY")
        report = account_analytics(c, SCOPE, now_ms=70000)
        foreign = account_analytics(c, replace(SCOPE, account_id="other"), now_ms=70000)
        impact = sizing_impact(c, SCOPE, {"sizing.max_margin_fraction": "0.05"})
    assert report["summary"]["settled_trades"] == 1
    assert Decimal(report["summary"]["net_pnl"]) == Decimal("-10.106")
    assert Decimal(report["trades"][0]["fees_usdt"]) == Decimal("0.096")
    assert report["trades"][0]["id"] == episode
    assert Decimal(report["trades"][0]["held_minutes"]) == Decimal(7) / 60000
    assert report["trades"][0]["explanation_status"] == "RECORDED"
    assert report["trades"][0]["excursions"] is None  # Never invent historical marks.
    assert foreign["summary"]["settled_trades"] == 0 and foreign["trades"] == []
    assert impact["replayed"] == 1 and impact["pnl_comparison"] is None
    assert impact["rows"][0]["before"]["quantity"] == "1.25"
    assert (
        impact["rows"][0]["after"]["quantity"] == "1.25"
    )  # Stop risk, not 5%, binds here.


def test_unsettled_loss_is_unknown_not_a_zero_win(database, closed):
    with database() as c:
        report = account_analytics(c, SCOPE, now_ms=70000)
    assert report["summary"]["unsettled_or_unvalued"] == 1
    assert report["summary"]["win_rate_pct"] is None
    assert report["summary"]["mean_net_pnl"] is None
    assert report["trades"][0]["net_pnl"] is None


def test_equity_timeout_does_not_hide_settlements_or_abort_read_transaction(
    database, closed, monkeypatch
):
    import services.v2_equity_analytics as equity_module

    def timeout(conn, *_args, **_kwargs):
        conn.execute("SET LOCAL statement_timeout='1ms'")
        conn.execute("SELECT pg_sleep(0.02)")

    monkeypatch.setattr(equity_module, "equity_report", timeout)
    with database() as c:
        c.execute("SET TRANSACTION READ ONLY")
        report = account_analytics(c, SCOPE, now_ms=70000)
        assert c.execute("SELECT 1").fetchone() == (1,)
    assert report["equity"]["status"] == "QUERY_TIMEOUT"
    assert report["summary"]["unsettled_or_unvalued"] == 1


def test_profit_factor_zero_loss_and_maintenance_are_explicit():
    def trade(pnl, maintenance=False):
        return {"net_pnl": pnl, "maintenance": maintenance, "held_minutes": "10"}

    empty = statistics([])
    assert (
        empty["profit_factor_status"] == "NO_SAMPLE" and empty["mean_net_pnl"] is None
    )
    one = statistics([trade("5")])
    assert (
        one["profit_factor"] is None and one["profit_factor_status"] == "NO_LOSS_SAMPLE"
    )
    mixed = statistics(
        [trade("10"), trade("-5"), trade("0"), trade("1000", True), trade(None)]
    )
    assert mixed["settled_trades"] == 3 and mixed["breakeven"] == 1
    assert mixed["net_pnl"] == "5" and mixed["profit_factor"] == "2"
    assert mixed["maintenance_excluded"] == mixed["unsettled_or_unvalued"] == 1


@pytest.mark.parametrize("days", [0, 2, 365, True, "14"])
def test_window_is_bounded(database, days):
    with database() as c, pytest.raises(ValueError, match="WINDOW"):
        account_analytics(c, SCOPE, days=days)


def test_read_only_analysis_and_replay_create_no_business_records(database, closed):
    with database() as c:
        before = c.execute(
            "SELECT (SELECT count(*) FROM v2_trade_intents),(SELECT count(*) FROM v2_orders),(SELECT count(*) FROM v2_state_history)"
        ).fetchone()
        account_analytics(c, SCOPE, now_ms=70000)
        sizing_impact(c, SCOPE, {"sizing.max_margin_fraction": "0.10"})
        after = c.execute(
            "SELECT (SELECT count(*) FROM v2_trade_intents),(SELECT count(*) FROM v2_orders),(SELECT count(*) FROM v2_state_history)"
        ).fetchone()
        assert before == after


def test_receipt_sampling_is_explicit_bounded_and_account_scoped(database):
    owner = StrategyScope(**asdict(SCOPE), producer="s6").consumer
    foreign = StrategyScope(
        **asdict(replace(SCOPE, account_id="other")), producer="s6"
    ).consumer
    with database() as c:
        c.execute(
            """WITH inserted AS (
          INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot)
          SELECT gen_random_uuid(),'s3','SANDBOX','qa-bulk-'||n,'{}'::jsonb FROM generate_series(1,6001) n
          RETURNING signal_id)
        INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason)
          SELECT %s,signal_id,'IGNORED','QA_FILTER' FROM inserted""",
            (owner,),
        )
        c.execute(
            "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) SELECT %s,signal_id,'IGNORED','FOREIGN' FROM v2_inbound_signals",
            (foreign,),
        )
        report = account_analytics(c, SCOPE)
    assert report["receipt_sample_count"] == report["receipt_sample_limit"] == 5000
    assert report["receipt_sample_truncated"] is True
    assert report["receipts"] == [
        {
            "source": "s3",
            "consumer": owner,
            "outcome": "IGNORED",
            "reason": "QA_FILTER",
            "receipts": 5000,
            "distinct_signals": 5000,
        }
    ]
