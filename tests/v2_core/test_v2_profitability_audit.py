from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

import pytest

from services.v2_profitability_audit import (
    HOUR,
    MINUTE,
    VERSION,
    AuditPolicy,
    BudgetArchive,
    enrich_audit,
    public_report,
    runner_input,
    unavailable,
)


def case():
    return {
        "trade": {
            "id": "private-episode",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "opened_ms": 99 * HOUR + MINUTE,
            "closed_ms": 100 * HOUR + 30 * MINUTE,
            "source": "s3",
            "entry_mode": "RIGHT_MOMENTUM",
            "config_digest": "a" * 64,
            "strategy_version": "fixture",
            "net_pnl": "22",
            "opening_quantity": "2",
            "closing_quantity": "2",
            "opening_notional": "200",
            "closing_notional": "223",
            "sizing": {"stop_price": "80"},
        },
        "fills": [
            {
                "identity": "open",
                "t": 99 * HOUR + MINUTE,
                "leg": "OPEN",
                "quantity": "2",
                "price": "100",
            },
            {
                "identity": "partial",
                "t": 99 * HOUR + 40 * MINUTE,
                "leg": "CLOSE",
                "quantity": ".5",
                "price": "110",
            },
            {
                "identity": "flat",
                "t": 100 * HOUR + 30 * MINUTE,
                "leg": "CLOSE",
                "quantity": "1.5",
                "price": "112",
            },
        ],
    }


def read(_symbol, start, end):
    rows = []
    for t in range(start, end, MINUTE):
        p = Decimal(100) + Decimal((t - 100 * HOUR) // MINUTE) * Decimal(".005")
        rows.append(
            {
                "t": t,
                "o": str(p),
                "h": str(p + Decimal(".01")),
                "l": str(p - Decimal(".01")),
                "c": str(p),
                "v": "1",
                "tbv": ".5",
            }
        )
    return list(reversed(rows))


def inputs():
    policy = AuditPolicy(max_trades=1, trend_cases=1)
    return {
        "version": VERSION,
        "policy": asdict(policy),
        "cohorts": {"execution_authorized": False},
        "paths": {
            "kind": "OFFLINE_FACTUAL_TRADE_PATH_REVIEW",
            "account_scope": {
                "environment": "SANDBOX",
                "account_id": "private-account",
            },
            "as_of_ms": 124 * HOUR,
            "cases": [case()],
            "selected_count": 1,
        },
    }


def test_full_flow_partial_fills_archive_features_paths_and_residual_model():
    before = inputs()
    frozen = deepcopy(before)
    result = enrich_audit(before, read_bars=read)
    assert before == frozen
    assert result["trade_paths"]["status_counts"] == {"AVAILABLE_LOWER_BOUND": 1}
    row = result["cases"][0]
    assert row["indicators"]["status"] == "COMPARED"
    assert row["post_flat"]["status"] == "OBSERVED"
    assert row["post_flat"]["basis"] == "FIRST_FULLY_AFTER_FLAT_MINUTE_CLOSE"
    assert row["trend"]["status"] == "SHADOW"
    assert row["trend"]["residual_quantity"] == "1.5"
    assert row["trend"]["timeliness"] == "NOT_PROVEN"
    assert row["trend"]["modeled_net_delta_with_reported_funding"] is None
    assert result["execution_authorized"] is result["profitability_valid"] is False
    assert "private-account" not in str(result) and "private-episode" not in str(result)


def test_boundary_flat_is_not_resurrected_as_trend_inventory():
    c = case()
    c["fills"][-1]["t"] = 100 * HOUR
    c["trade"]["closed_ms"] = 100 * HOUR
    with pytest.raises(ValueError, match="NOT_HELD"):
        runner_input(c, read, as_of_ms=124 * HOUR, policy=AuditPolicy())


def test_boundary_partial_is_subtracted_before_activation():
    c = case()
    c["fills"][1]["t"] = 100 * HOUR
    payload = runner_input(
        c, BudgetArchive(read, AuditPolicy()), as_of_ms=124 * HOUR, policy=AuditPolicy()
    )
    assert len(payload["reduction_fills"]) == 1


def test_bounded_horizon_is_not_misrepresented_as_stale_archive():
    c = inputs()
    c["paths"]["as_of_ms"] = 200 * HOUR
    result = enrich_audit(c, read_bars=read)
    model = result["cases"][0]["trend"]
    assert model["simulation_as_of_ms"] == 124 * HOUR
    assert model["audit_as_of_ms"] == 200 * HOUR
    assert model["observation_window_truncated"] is True
    assert all(
        x["status"] != "EXIT_REQUIRED_DATA_STALE" for x in model["arms"].values()
    )


def test_archive_failure_is_not_a_trade_halt_or_empty_success():
    def broken(*_):
        raise RuntimeError("password=DO_NOT_EXPORT")

    result = enrich_audit(inputs(), read_bars=broken)
    assert result["trade_paths"]["status_counts"] == {
        "ARCHIVE_UNAVAILABLE_OR_INVALID": 1
    }
    assert result["cases"][0]["indicators"]["status"] == "UNAVAILABLE"
    assert "DO_NOT_EXPORT" not in str(result)
    assert result["execution_authorized"] is False


@pytest.mark.parametrize(
    "field,value",
    [("opening_quantity", "3"), ("closing_notional", "999"), ("closed_ms", 101 * HOUR)],
)
def test_inconsistent_actual_fills_cannot_produce_valid_derived_research(field, value):
    data = inputs()
    data["paths"]["cases"][0]["trade"][field] = value
    result = enrich_audit(data, read_bars=read)
    assert result["trade_paths"]["status_counts"] == {
        "INVALID_OR_INCOMPLETE_FILL_EVIDENCE": 1
    }
    item = result["cases"][0]
    assert all(
        item[name]["reason"] == "PROFIT_AUDIT_INVALID_FILL_EVIDENCE"
        for name in ("indicators", "post_flat", "trend")
    )
    assert result["archive_calls"] == 0


def test_direct_runner_loader_rejects_inconsistent_cash_and_fill_evidence():
    data = case()
    data["trade"]["opening_quantity"] = "3"
    with pytest.raises(ValueError, match="FILL_TOTAL"):
        runner_input(data, read, as_of_ms=124 * HOUR, policy=AuditPolicy())


def test_total_read_budget_counts_failures_and_stops_before_next_io():
    calls = []

    def broken(*args):
        calls.append(args)
        raise RuntimeError("unavailable")

    bounded = BudgetArchive(broken, AuditPolicy(max_archive_minutes=1440))
    with pytest.raises(RuntimeError):
        bounded("BTCUSDT", 0, 1440 * MINUTE)
    with pytest.raises(ValueError, match="BUDGET"):
        bounded("BTCUSDT", 0, MINUTE)
    assert len(calls) == 1


def test_deadline_checked_before_each_page():
    now, calls = [0], []

    def archive(*args):
        calls.append(args)
        now[0] = 61
        return []

    bounded = BudgetArchive(archive, AuditPolicy(), monotonic=lambda: now[0])
    with pytest.raises(ValueError, match="DEADLINE"):
        bounded("BTCUSDT", 0, 1440 * MINUTE)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        lambda rows: rows + rows,
        lambda rows: [{**rows[0], "t": -1}],
        lambda rows: "not bars",
    ],
)
def test_archive_rejects_duplicate_out_of_scope_or_malformed_page(bad):
    bounded = BudgetArchive(
        lambda *_: bad(read("BTCUSDT", 100 * HOUR, 100 * HOUR + MINUTE)), AuditPolicy()
    )
    with pytest.raises(ValueError):
        bounded("BTCUSDT", 100 * HOUR, 100 * HOUR + MINUTE)


def test_unknown_reason_cannot_expose_dsn_or_secret():
    assert (
        unavailable(ValueError("PROFIT_AUDIT_DEADLINE"))["reason"]
        == "PROFIT_AUDIT_DEADLINE"
    )
    assert unavailable(ValueError("dsn=secret"))["reason"] == "ValueError"
    assert public_report(
        {"account_scope": {"account_id": "a"}, "items": [{"trade_id": "t", "pnl": "1"}]}
    ) == {"items": [{"pnl": "1"}]}


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_trades", True),
        ("trend_cases", 7),
        ("max_archive_seconds", 1000),
        ("max_archive_minutes", 0),
    ],
)
def test_audit_configuration_bounds(field, value):
    with pytest.raises(ValueError):
        AuditPolicy(**{field: value})
