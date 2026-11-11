import time
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE as DB_SCOPE
from test_v2_directional_settlement import baseline, service

from services.v2_profit_capture_review import (
    build_profit_capture_review,
    strict_trade_review,
)
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope

SCOPE = AccountScope("BINANCE", "qa-account", "SANDBOX", "FUTURES")
database = database_fixture
closed = closed_fixture


def row(identity="a", **changes):
    return {
        "id": identity,
        "side": "BUY",
        "entry_mode": "MOMENTUM",
        "strategy_version": "v1",
        "config_digest": "config-a",
        "decided_ms": 1000,
        "closed_ms": 2000,
        "settled_ms": 2100,
        "settlement_state": "SETTLED",
        "maintenance": False,
        "net_pnl": "9",
        "opening_quantity": "2",
        "closing_quantity": "2",
        "opening_notional": "200",
        "closing_notional": "210",
        "fees_usdt": "1",
        **changes,
    }


def review(rows):
    return {
        "account_scope": asdict(SCOPE),
        "settlement_label_contract": "CURRENT_ACCOUNTING_REVISION_COMPLETE_USDT_SETTLEMENT",
        "trades": rows,
        "as_of_ms": 10000,
        "window_start_ms": 0,
        "truncated": False,
    }


def build(rows):
    return build_profit_capture_review(review(rows), holdout_start_ms=5000)


def test_fee_aware_partial_exit_vwap_is_actual_gross_not_double_deducted():
    # Two partial reductions: 1@120, 1@90; aggregate close=210, net=9.
    result = build([row(), row("b", side="SELL", net_pnl="-11")])
    train = result["periods"]["TRAIN"]
    assert Decimal(train["net_pnl_usdt"]) == -2
    assert Decimal(train["gross_pnl_usdt"]) == 0
    assert Decimal(train["net_minus_gross_usdt"]) == -2
    assert Decimal(train["observed_fees_usdt"]) == 2
    assert train["sample_status"] == "INSUFFICIENT_SAMPLE"
    assert len(result["groups"]) == 2
    assert result["hypothetical_pnl"] is None
    assert not result["profit_improvement_proven"]


def test_holdout_split_purges_unknown_and_late_settlement_labels():
    result = build(
        [
            row(),
            row("b", settled_ms=5500),
            row("c", closed_ms=5500, settled_ms=6000),
            row("d", settlement_state="SETTLEMENT_REVISION_CHANGED", net_pnl=None),
            row("e", decided_ms=6000, closed_ms=7000, settled_ms=7100),
            row(
                "f",
                decided_ms=6000,
                closed_ms=None,
                settled_ms=None,
                settlement_state="OPEN_OR_PARTIAL_EXIT",
                net_pnl=None,
            ),
        ]
    )
    assert result["periods"]["TRAIN"]["settled"] == 1
    assert result["periods"]["HOLDOUT"]["settled"] == 1
    assert result["periods"]["HOLDOUT"]["unvalued"] == 1
    assert result["excluded"]["EMBARGOED_UNAVAILABLE_AT_CUTOFF"] == 3


def test_future_label_and_maintenance_cannot_become_profit():
    result = build([row(settled_ms=11000), row("b", maintenance=True, net_pnl="99999")])
    assert result["periods"]["TRAIN"]["net_pnl_usdt"] is None
    assert result["excluded"] == {"INVALID_LABEL_TIMING": 1, "MAINTENANCE": 1}


def test_zero_denominators_missing_fees_and_mismatched_quantities_are_unknown():
    stats = build([row(net_pnl="0", fees_usdt=None, closing_quantity="1")])["periods"][
        "TRAIN"
    ]
    assert stats["profit_factor"] is stats["payoff_ratio"] is None
    assert stats["gross_pnl_usdt"] is stats["net_minus_gross_usdt"] is None
    assert stats["observed_fees_usdt"] is None
    assert stats["breakeven"] == 1
    assert build([])["periods"]["TRAIN"]["win_rate_pct"] is None


def test_config_versions_are_not_pooled_and_labels_do_not_choose_split():
    inputs = [
        row(),
        row("b", config_digest="new", decided_ms=6000, closed_ms=7000, settled_ms=7100),
    ]
    first = build(inputs)
    changed = deepcopy(inputs)
    changed[0]["net_pnl"] = "-999"
    second = build(changed)
    assert [
        (g["period"], g["config_digest"], g["observed"]) for g in first["groups"]
    ] == [(g["period"], g["config_digest"], g["observed"]) for g in second["groups"]]


@pytest.mark.parametrize(
    "mutation", ["foreign_environment", "unvalidated", "duplicate", "future_cutoff"]
)
def test_invalid_report_contract_rejected(mutation):
    source = review([row()])
    cutoff = 5000
    if mutation == "foreign_environment":
        source["account_scope"]["environment"] = "PRODUCTION"
    elif mutation == "unvalidated":
        source["settlement_label_contract"] = "LEGACY"
    elif mutation == "duplicate":
        source["trades"].append(row())
    else:
        cutoff = 11000
    with pytest.raises(ValueError):
        build_profit_capture_review(source, holdout_start_ms=cutoff)


def test_strict_scope_rejected_before_database_access():
    with pytest.raises(ValueError, match="SCOPE"):
        strict_trade_review(None, replace(SCOPE, environment="LIVE"), now_ms=10000)


def test_strict_label_errors_are_not_silently_zero(monkeypatch):
    import services.v2_profit_capture_review as module

    base = review([row()])
    monkeypatch.setattr(module, "trade_review", lambda *a, **k: base)

    def unavailable(*args, **kwargs):
        raise RuntimeError("safe test failure")

    monkeypatch.setattr(module, "outcomes", unavailable)
    with pytest.raises(RuntimeError):
        strict_trade_review(None, SCOPE, now_ms=10000)


def test_strict_read_is_scoped_and_rejects_stale_accounting_revision(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    assert service(database, income).run_once()["status"] == "CLEAR"

    def read(scope=DB_SCOPE):
        with database() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            return strict_trade_review(
                conn, scope, now_ms=time.time_ns() // 1000000 + 1
            )

    first = read()
    assert first["summary"]["settled_trades"] == 1
    assert first["trades"][0]["settlement_state"] == "SETTLED"
    assert read(replace(DB_SCOPE, account_id="other"))["trades"] == []
    runtime.data.ledger.adjustment(
        episode_id=episode,
        source_id="qa-late-funding",
        amount_text="-0.02",
        currency="USDT",
        kind="FUNDING",
        occurred_at_ms=70001,
        evidence={"source": "qa"},
    )
    changed = read()
    assert changed["summary"]["settled_trades"] == 0
    assert changed["trades"][0]["net_pnl"] is None
    assert changed["trades"][0]["settlement_state"] == "SETTLEMENT_REVISION_CHANGED"


def test_strict_read_rejects_write_transactions(database):
    with database() as conn, pytest.raises(ValueError, match="READ_ONLY"):
        strict_trade_review(conn, DB_SCOPE, now_ms=time.time_ns() // 1000000)


def test_strict_read_batches_labels_and_never_keeps_stale_pnl(monkeypatch):
    import services.v2_profit_capture_review as module
    from services.v2_trade_review import build_review

    records = [
        {
            **row(str(i)),
            "symbol": "BTCUSDT",
            "source": "s3",
            "opened_ms": 1001,
            "close_known_ms": 2001,
        }
        for i in range(123)
    ]
    base = build_review(records, start_ms=0, now_ms=10000)
    base.update(
        window_days=14,
        sample_limit=500,
        context_lookback_hours=24,
        loaded_records=123,
        account_scope=asdict(SCOPE),
    )
    batches = []
    monkeypatch.setattr(module, "trade_review", lambda *a, **k: base)

    def labels(conn, scope, ids, *, now):
        batches.append(list(ids))
        return {
            i: {
                "state": "SETTLEMENT_REVISION_CHANGED",
                "net_pnl": None,
                "closed_ms": 2000,
                "settled_ms": 2100,
            }
            for i in ids
        }

    monkeypatch.setattr(module, "outcomes", labels)
    result = strict_trade_review(None, SCOPE, now_ms=10000)
    assert list(map(len, batches)) == [100, 23]
    assert result["label_query_batches"] == 2
    assert result["summary"]["settled_trades"] == 0
    assert all(r["net_pnl"] is None for r in result["trades"])
