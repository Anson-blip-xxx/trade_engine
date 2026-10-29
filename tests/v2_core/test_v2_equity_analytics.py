from dataclasses import asdict, replace
from decimal import Decimal
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture

from services.v2_equity_analytics import equity_report
from v2_core.account_risk import AccountScope
from v2_core.state import BusinessState, StateKey

database = database_fixture
SCOPE = AccountScope("BINANCE", "equity-qa", "SANDBOX", "FUTURES")


def snapshot(
    connect,
    at,
    *,
    scope=SCOPE,
    wallet="1000",
    equity="1000",
    overrides=None,
    deleted=False,
):
    store = BusinessState(connect)
    key = StateKey(**asdict(scope), namespace="capital-snapshot-v1", key="latest")
    old = store.read(key)
    payload = {
        "account_scope": asdict(scope),
        "started_at_ms": at,
        "valid_until_ms": at + 100,
        "observation_id": str(uuid4()),
        "account": {
            "totalWalletBalance": wallet,
            "totalMarginBalance": equity,
            "totalInitialMargin": "100",
            "availableBalance": "800",
        },
        # The modeled budget is deliberately not the venue equity.
        "capital_health": {"base": "50", "peak": "80", "factor": "0.5"},
    }
    payload.update(overrides or {})
    assert (
        store.change(
            key,
            expected_version=old.version if old else 0,
            request_key=str(uuid4()),
            payload={} if deleted else payload,
            deleted=deleted,
            reason="VERIFIED_CAPITAL_SNAPSHOT",
        ).code
        == "APPLIED"
    )


def read(connect, *, scope=SCOPE, start_ms=0, now_ms=400, **kwargs):
    with connect() as c:
        c.execute("SET TRANSACTION READ ONLY")
        return equity_report(c, scope, start_ms=start_ms, now_ms=now_ms, **kwargs)


def income(connect, at, value, kind="TRANSFER", currency="USDT", scope=SCOPE):
    with connect() as c:
        c.execute(
            "INSERT INTO v2_exchange_income(income_id,exchange,account_id,environment,product,income_type,source_id,symbol,amount,currency,occurred_at_ms,evidence) VALUES (%s,%s,%s,%s,%s,%s,%s,'',%s,%s,%s,'{}')",
            (
                str(uuid4()),
                *asdict(scope).values(),
                kind,
                str(uuid4()),
                value,
                currency,
                at,
            ),
        )


def test_empty_and_single_sample_never_invent_returns(database):
    empty = read(database)
    assert empty["status"] == "NO_SAMPLES" and empty["latest"] is None
    snapshot(database, 300, equity="900")
    report = read(database, now_ms=350)
    assert report["fresh"] and report["latest"]["equity"] == "900"
    assert report["latest"]["unrealized"] == "-100"
    assert report["raw_equity_change"] is None
    assert report["raw_sampled_peak_decline_pct"] is None
    assert report["strategy_return_pct"] is report["strategy_drawdown_pct"] is None
    assert report["latest"]["at"].endswith("+08:00")


def test_deposit_and_realized_costs_are_not_strategy_profit(database):
    snapshot(database, 100)
    snapshot(database, 300, wallet="1950", equity="1900")
    income(database, 150, "1000")
    income(database, 200, "-45", "REALIZED_PNL")
    income(database, 250, "-5", "COMMISSION")
    income(
        database, 100, "999"
    )  # First point's balance already includes this boundary.
    income(database, 301, "999")  # Outside the sampled interval.
    report = read(database)
    assert report["raw_equity_change"] == "900"
    assert report["cash"]["events"] == 3
    assert Decimal(report["cash"]["observed_usdt_cash"]) == 950
    assert Decimal(report["cash"]["wallet_reconciliation_residual"]) == 0
    assert report["cash"]["coverage_status"] == "NOT_PROVEN"
    assert report["strategy_return_pct"] is None


def test_withdrawal_raw_decline_not_labeled_strategy_drawdown(database):
    snapshot(database, 100, wallet="2000", equity="2000")
    snapshot(database, 200, wallet="1000", equity="1000")
    income(database, 150, "-1000")
    report = read(database)
    assert Decimal(report["raw_sampled_peak_decline_pct"]) == 50
    assert report["strategy_drawdown_pct"] is None
    assert not report["fresh"]


def test_account_environment_scope_and_payload_scope_are_enforced(database):
    other = replace(SCOPE, account_id="other")
    live = replace(SCOPE, environment="LIVE")
    snapshot(database, 100)
    snapshot(database, 200, equity="900")
    snapshot(database, 300, scope=other, equity="999999")
    snapshot(database, 300, scope=live, equity="777777")
    income(database, 150, "99999", scope=other)
    income(database, 150, "88888", scope=live)
    snapshot(database, 300, overrides={"account_scope": asdict(other)}, equity="666666")
    report = read(database)
    assert report["samples"] == 2 and report["cash"]["events"] == 0
    assert report["latest"]["equity"] == "900"
    assert report["excluded"]["SCOPE_MISMATCH"] == 1
    assert not report["latest_record_valid"]


@pytest.mark.parametrize(
    "overrides,deleted,reason",
    [
        ({"account": {}}, False, "INVALID_SNAPSHOT"),
        ({"started_at_ms": 600, "valid_until_ms": 700}, False, "FUTURE_OBSERVATION"),
        ({}, True, "DELETED_OR_UNVERIFIED"),
        ({"valid_until_ms": 10}, False, "INVALID_SNAPSHOT"),
        ({"started_at_ms": True}, False, "INVALID_SNAPSHOT"),
    ],
)
def test_invalid_latest_never_makes_old_snapshot_fresh(
    database, overrides, deleted, reason
):
    snapshot(database, 100, overrides={"valid_until_ms": 900})
    snapshot(database, 200, overrides=overrides, deleted=deleted)
    report = read(database)
    assert report["samples"] == 1 and not report["fresh"]
    assert report["excluded"][reason] == 1


def test_duplicate_timestamp_dedup_and_conflict_exclusion(database):
    snapshot(database, 100, wallet="1000.0")
    snapshot(database, 100, wallet="1000.00")
    report = read(database)
    assert report["samples"] == 1 and report["excluded"]["DUPLICATE_TIMESTAMP"] == 1
    snapshot(database, 200, equity="900")
    snapshot(database, 200, equity="800")
    report = read(database)
    assert report["samples"] == 1 and report["excluded"]["CONFLICTING_TIMESTAMP"] == 2
    assert not report["latest_record_valid"]


def test_time_window_clock_regression_and_scan_cap_are_explicit(database):
    for at in (100, 300, 200, 400):
        snapshot(database, at)
    report = read(database, start_ms=150, now_ms=500)
    assert report["clock_regression"] and report["samples"] == 3
    assert report["max_gap_ms"] == 100
    bounded = read(database, sample_limit=2, now_ms=500)
    assert bounded["scan_truncated"] and bounded["scanned_records"] == 2
    assert bounded["samples"] == 2
    assert read(database, start_ms=500, now_ms=600)["status"] == "NO_SAMPLES"


@pytest.mark.parametrize("currency,expected", [("USDT", True), ("BNB", False)])
def test_cash_truncation_and_no_implicit_currency_conversion(
    database, currency, expected
):
    snapshot(database, 100)
    snapshot(database, 300)
    income(database, 150, "1", currency=currency)
    report = read(database)
    assert (report["cash"]["wallet_reconciliation_residual"] is not None) == expected
    income(database, 160, "1")
    income(database, 170, "1")
    report = read(database, sample_limit=2)
    assert report["cash"]["truncated"] and report["cash"]["events"] == 2
    assert report["cash"]["wallet_reconciliation_residual"] is None


def test_negative_equity_and_chart_reduction_keep_peak_trough_and_endpoints(database):
    values = ["1000"] * 30
    values[7], values[12], values[29] = "2000", "-100", "1500"
    for at, value in enumerate(values, 1):
        snapshot(database, at * 10, equity=value)
    report = read(database, chart_limit=8)
    assert len(report["chart"]) <= 8
    assert report["chart"][0]["at_ms"] == 10 and report["chart"][-1]["at_ms"] == 300
    assert {"2000", "-100"} <= {p["equity"] for p in report["chart"]}
    assert Decimal(report["raw_sampled_peak_decline_pct"]) == 105


@pytest.mark.parametrize(
    "arguments",
    [
        {"now_ms": True},
        {"start_ms": -1},
        {"sample_limit": 1},
        {"sample_limit": 10001},
        {"chart_limit": 7},
    ],
)
def test_invalid_bounds_rejected_before_query(database, arguments):
    with pytest.raises(ValueError, match="BOUNDS"):
        read(database, **arguments)


def test_read_report_never_creates_states_income_or_orders(database):
    snapshot(database, 100)
    snapshot(database, 200)
    with database() as c:
        query = "SELECT (SELECT count(*) FROM v2_state_history),(SELECT count(*) FROM v2_exchange_income),(SELECT count(*) FROM v2_orders)"
        before = c.execute(query).fetchone()
        c.execute("SET TRANSACTION READ ONLY")
        equity_report(c, SCOPE, start_ms=0, now_ms=400)
        assert c.execute(query).fetchone() == before
