import hashlib
import json
from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from services.v2_daily_mover_execution_review import MAX_TRADES, build_execution_review

MINUTE = 60_000
START = 1792857600000
END = START + 1440 * MINUTE
SCOPE = {
    "exchange": "BINANCE",
    "account_id": "private-account-do-not-project",
    "environment": "SANDBOX",
    "product": "FUTURES",
}


def digest(value, *, ensure_ascii=False):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=ensure_ascii, separators=(",", ":")
        ).encode()
    ).hexdigest()


def trade(identity="private-episode", *, net="2.9", side="BUY", hold=60, **changes):
    gross = Decimal(net) + Decimal("0.1")
    return {
        "id": identity,
        "symbol": "BTCUSDT",
        "side": side,
        "opened_ms": START + MINUTE,
        "decided_ms": START,
        "closed_ms": START + (hold + 1) * MINUTE,
        "settled_ms": START + (hold + 2) * MINUTE,
        "opening_quantity": "1",
        "closing_quantity": "1",
        "remaining_quantity": "0",
        "opening_notional": "100",
        "closing_notional": str(Decimal(100) + gross * (1 if side == "BUY" else -1)),
        "gross_pnl_usdt": str(gross),
        "net_pnl": net,
        "fees_usdt": "0.1",
        "net_pnl_available": True,
        "fills_valid": True,
        "settlement_state": "SETTLED",
        "settlement_revision": 1,
        "status": "FILLED_SETTLED",
        "source": "s3",
        "exit_reason": "PEAK_DRAWDOWN",
        **changes,
    }


def projection(row):
    return {
        key: deepcopy(value)
        for key, value in {**row, "actual_settled_net_usdt": row.get("net_pnl")}.items()
        if key
        in {
            "side",
            "opened_ms",
            "closed_ms",
            "settled_ms",
            "opening_quantity",
            "closing_quantity",
            "actual_settled_net_usdt",
            "status",
            "settlement_state",
        }
    }


def report(*trades):
    rows = list(trades or [trade()])
    return {
        "account_environment": "SANDBOX",
        "account_alias": "private-alias-never-project",
        "scope_key": digest(SCOPE, ensure_ascii=True),
        "market_source": "BINANCE_USDM_LIVE",
        "as_of_ms": END + 6 * 60 * MINUTE,
        "window": {"start_ms": START, "end_ms": END, "timezone": "Asia/Shanghai"},
        "account_evidence": {
            "account_scope": deepcopy(SCOPE),
            "as_of_ms": END,
            "window_start_ms": START,
            "window_end_ms": END,
            "symbols": {"BTCUSDT": {"trades": deepcopy(rows)}},
        },
        "rows": [
            {
                "symbol": "BTCUSDT",
                "evidence": {"trades": [projection(row) for row in rows]},
            }
        ],
    }


def path_case(row, *, ret="2", favorable="3", adverse="-1"):
    first = ((row["closed_ms"] + MINUTE - 1) // MINUTE) * MINUTE
    anchor, end = first + MINUTE, first + 60 * MINUTE
    return {
        "episode_ref": row["id"],
        "symbol": row["symbol"],
        "side": row["side"],
        "source": "BINANCE_USDM_SANDBOX",
        "actual_net_pnl": row["net_pnl"],
        "actual_net_basis": "CURRENT_COMPLETE_USDT_SETTLEMENT_COSTS_ALREADY_INCLUDED",
        "post_flat": {
            "status": "OBSERVED",
            "basis": "FIRST_FULLY_AFTER_FLAT_MINUTE_CLOSE",
            "flat_ms": row["closed_ms"],
            "requested_horizon_minutes": 60,
            "anchor_ms": anchor,
            "anchor_delay_ms": anchor - row["closed_ms"],
            "observed_end_ms": end,
            "observed_duration_ms": end - anchor,
            "horizon_delay_ms": end - (row["closed_ms"] + 60 * MINUTE),
            "return_pct": ret,
            "favorable_excursion_pct": favorable,
            "adverse_excursion_pct": adverse,
            "anchor_price": "100",
            "end_price": str(
                Decimal(100) + Decimal(ret) * (1 if row["side"] == "BUY" else -1)
            ),
            "market_return_not_trade_pnl": True,
            "counterfactual_execution_proven": False,
            "costs_included": False,
            "path_digest": "a" * 64,
            "anchor_provenance": {
                "source": "CALLER_CLOSED_1M_ARCHIVE",
                "bar": {
                    "t": first,
                    "o": "100",
                    "h": "101",
                    "l": "99",
                    "c": "100",
                    "v": "1",
                },
            },
        },
    }


def with_paths(value, *cases, **changes):
    value["actual_same_environment_paths"] = {
        "source": "BINANCE_USDM_SANDBOX",
        "as_of_ms": END,
        "cases": list(cases),
        **changes,
    }
    return value


def raw(value):
    return value["account_evidence"]["symbols"]["BTCUSDT"]["trades"]


def test_six_settlements_match_factual_summary_without_inventing_funding():
    trades = [
        trade("jct", net="5.18", side="SELL", hold=52),
        trade(
            "apr", net="-0.52", side="SELL", hold=90, exit_reason="LOW_YIELD_STAGNATION"
        ),
        trade("haedal-a", net="4.76", hold=23),
        trade("huma", net="4.83", side="SELL", hold=142),
        trade("haedal-b", net="-2.82", hold=17, exit_reason="EARLY_LOSS_MOMENTUM_WEAK"),
        trade("hot", net="-2.77", hold=11, exit_reason=None),
    ]
    value = report(*trades)
    before = deepcopy(value)
    result = build_execution_review(value)
    assert value == before
    assert result["status"] == "AVAILABLE"
    assert result["totals"]["settled_count"] == 6
    # These are rounded fixture amounts, not the full-precision live settlement.
    assert Decimal(result["totals"]["net_usdt"]) == Decimal("8.66")
    assert result["totals"]["wins"] == result["totals"]["losses"] == 3
    assert result["totals"]["held_minutes_median"] == "37.500000"
    assert Decimal(result["by_side"]["SELL"]["net_usdt"]) == Decimal("9.49")
    assert result["primary_sources"] == {"s3": 6}
    assert result["funding_usdt"] is None
    assert result["by_exit_reason"]["UNKNOWN_RECORDED_REASON"]["settled_count"] == 1
    assert Decimal(result["totals"]["gross_usdt"]) - Decimal(
        result["totals"]["recorded_fees_usdt"]
    ) == Decimal(result["totals"]["net_usdt"])


def test_input_order_alias_and_extra_secret_fields_do_not_change_output_or_leak():
    value = report(trade("a"), trade("b", side="SELL"))
    expected = build_execution_review(value)
    raw(value).reverse()
    value["account_alias"] = "another-alias"
    raw(value)[0]["secret"] = "private-secret"
    raw(value)[0]["orders"] = [{"account_id": "private-child-id"}]
    actual = build_execution_review(value)
    assert actual == expected
    text = json.dumps(actual)
    assert all(
        secret not in text
        for secret in (
            SCOPE["account_id"],
            "another-alias",
            "private-secret",
            "private-child-id",
        )
    )
    assert all(len(row["trade_ref"]) == 64 for row in actual["trades"])
    assert actual["content_digest"] == digest(
        {key: item for key, item in actual.items() if key != "content_digest"}
    )
    assert actual["research_only"] is True
    assert (
        actual["execution_authorized"] is actual["automatic_parameter_updates"] is False
    )


def test_review_binds_account_environment_scope_and_both_cutoffs():
    value = report()
    result = build_execution_review(value)
    assert result["account_environment"] == "SANDBOX"
    assert result["scope_key"] == value["scope_key"]
    assert result["as_of_ms"] == value["as_of_ms"]
    assert result["evidence_as_of_ms"] == END
    assert result["promotion_authorized"] is False
    changed = deepcopy(value)
    changed["account_evidence"]["account_scope"]["account_id"] = "another-account"
    assert (
        build_execution_review(changed)["reason"] == "ACCOUNT_SCOPE_OR_CUTOFF_MISMATCH"
    )
    changed["scope_key"] = digest(
        changed["account_evidence"]["account_scope"], ensure_ascii=True
    )
    newer = build_execution_review(changed)
    assert newer["content_digest"] != result["content_digest"]
    assert newer["trades"][0]["trade_ref"] != result["trades"][0]["trade_ref"]


def test_non_ascii_account_scope_matches_existing_canonical_digest():
    from v2_core.evidence import canonical

    value = report()
    value["account_evidence"]["account_scope"]["account_id"] = "账户一"
    value["scope_key"] = hashlib.sha256(
        canonical(value["account_evidence"]["account_scope"]).encode()
    ).hexdigest()
    result = build_execution_review(value)
    assert result["status"] == "AVAILABLE"
    assert "账户一" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("bad", [END - 1, END + 7 * 60 * MINUTE, None, True])
def test_evidence_asof_must_cover_day_and_not_exceed_report(bad):
    value = report()
    value["account_evidence"]["as_of_ms"] = bad
    assert build_execution_review(value)["reason"] == "ACCOUNT_SCOPE_OR_CUTOFF_MISMATCH"


def test_identical_trade_under_two_alias_keys_is_counted_once():
    value = report()
    value["account_evidence"]["symbols"]["alias"] = deepcopy(
        value["account_evidence"]["symbols"]["BTCUSDT"]
    )
    result = build_execution_review(value)
    assert result["totals"]["settled_count"] == 1
    assert result["coverage"]["duplicate_copies_removed"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"net_pnl": "999"},
        {"settlement_revision": 2},
        {"source": "tv_bridge"},
        {"closed_ms": START + 62 * MINUTE},
        {"fees_usdt": "0.2"},
        {"fills_valid": False},
    ],
)
def test_conflicting_duplicate_invalidates_all_copies(changes):
    value = report()
    second = {**deepcopy(raw(value)[0]), **changes}
    raw(value).append(second)
    value["rows"][0]["evidence"]["trades"].append(projection(second))
    result = build_execution_review(value)
    assert result["totals"] is None
    assert result["coverage"]["conflicting_episodes"] == 1
    assert result["coverage"]["unknown_reasons"] == {"CONFLICTING_EPISODE_EVIDENCE": 1}


@pytest.mark.parametrize(
    "changes",
    [
        {"net_pnl_available": False},
        {"settlement_state": "REVISION_CHANGED"},
        {"status": "FILLED_UNSETTLED"},
        {"fills_valid": None},
        {"opening_quantity": "0"},
        {"closing_quantity": "0.5"},
        {"remaining_quantity": "0.1"},
        {"maintenance": True},
        {"exit_origin": "USER_APPROVED_MAINTENANCE"},
        {"side": "LONG"},
        {"environment": "LIVE"},
        {"account_id": "another-account"},
    ],
)
def test_incomplete_or_cross_scope_trade_never_becomes_settled_pnl(changes):
    value = report(trade(**changes))
    result = build_execution_review(value)
    assert result["status"] == "UNKNOWN"
    assert result["totals"] is None
    assert result["trades"] == []


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "NaN",
        "-Infinity",
        float("nan"),
        1.0,
        True,
        "1e99999999",
        "0e99999999",
        "1e19",
        "1e-100",
        "9" * 101,
        {},
        [],
    ],
)
def test_bad_net_is_unknown_not_zero_or_float(bad):
    value = report()
    raw(value)[0]["net_pnl"] = bad
    result = build_execution_review(value)
    assert result["totals"] is None
    assert result["trades"] == []


@pytest.mark.parametrize(
    "field", ["opened_ms", "closed_ms", "settled_ms", "decided_ms"]
)
@pytest.mark.parametrize("bad", [True, "123", END + 1, -1, None])
def test_bad_lifecycle_timestamps_are_unknown(field, bad):
    value = report()
    raw(value)[0][field] = bad
    result = build_execution_review(value)
    if field == "decided_ms" and bad is None:
        assert result["totals"]["settled_count"] == 1
    else:
        assert result["totals"] is None


def test_old_account_snapshot_does_not_borrow_new_report_cutoff():
    row = trade(settled_ms=END + MINUTE)
    value = report(row)
    assert value["as_of_ms"] > row["settled_ms"]
    assert build_execution_review(value)["totals"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"account_environment": "LIVE"},
        {"scope_key": "wrong"},
        {"account_evidence": None},
        {"as_of_ms": END - 1},
        {"rows": []},
    ],
)
def test_invalid_scope_or_projection_fails_closed(changes):
    value = {**report(), **changes}
    result = build_execution_review(value)
    assert result["totals"] is None


def test_invalid_missing_projection_or_identity_is_not_reconstructed_from_price():
    value = report()
    value["rows"][0]["evidence"]["trades"][0]["actual_settled_net_usdt"] = "9999"
    assert build_execution_review(value)["totals"] is None


def test_projected_trade_with_empty_raw_evidence_is_unknown_not_zero():
    value = report()
    raw(value).clear()
    result = build_execution_review(value)
    assert result["totals"] is None
    assert result["status"] == "UNKNOWN"
    value = report()
    raw(value)[0].pop("id")
    assert build_execution_review(value)["totals"] is None


@pytest.mark.parametrize("field", ["fees_usdt", "gross_pnl_usdt"])
def test_bad_optional_cost_fields_leave_verified_net_available_but_unknown_subtotal(
    field,
):
    value = report()
    raw(value)[0][field] = "NaN"
    result = build_execution_review(value)
    assert result["totals"]["net_usdt"] == "2.9"
    key = "recorded_fees_usdt" if field == "fees_usdt" else "gross_usdt"
    assert result["totals"][key] is None
    assert result["funding_usdt"] is None


def test_gross_must_agree_with_fill_notionals_not_net_plus_fees():
    value = report()
    raw(value)[0]["closing_notional"] = "1"
    result = build_execution_review(value)
    assert result["totals"]["net_usdt"] == "2.9"
    assert result["totals"]["gross_usdt"] is None


def test_breakeven_rebates_unicode_symbols_and_decimal_context_are_supported():
    value = report(
        trade(net="0", symbol="牛来USDT", fees_usdt="-0.1", source="tv_bridge")
    )
    value["rows"][0]["symbol"] = "牛来USDT"
    result = build_execution_review(value)
    with localcontext() as context:
        context.prec = 3
        assert build_execution_review(value) == result
    assert result["totals"]["breakeven"] == 1
    assert result["primary_sources"] == {"tv_bridge": 1}
    assert result["trades"][0]["symbol"] == "牛来USDT"


def test_shared_symbol_boundary_accepts_extension_cjk_without_normalizing():
    value = report(trade(symbol="𠀀USDT"))
    value["rows"][0]["symbol"] = "𠀀USDT"
    assert build_execution_review(value)["trades"][0]["symbol"] == "𠀀USDT"


def test_500_references_supported_but_501_never_silently_truncated():
    rows = [trade(str(index)) for index in range(MAX_TRADES)]
    assert build_execution_review(report(*rows))["totals"]["settled_count"] == 500
    result = build_execution_review(report(*rows, trade("overflow")))
    assert result["reason"] == "TRADE_BOUND"
    assert result["totals"] is None


def test_unknown_trade_does_not_erase_other_verified_trade():
    value = report(trade("good"), trade("bad", fills_valid=False))
    result = build_execution_review(value)
    assert result["status"] == "PARTIAL"
    assert result["totals"]["settled_count"] == 1
    assert result["totals"]["net_usdt"] == "2.9"


def test_favorable_and_adverse_exit_evidence_are_both_reported_without_counterfactual_pnl():
    a, b = trade("a"), trade("b", side="SELL")
    value = with_paths(report(a, b), path_case(a), path_case(b, ret="-2", adverse="-3"))
    result = build_execution_review(value)["post_flat_evidence"]
    assert result["observed_count"] == 2
    assert result["favorable_end_count"] == result["adverse_end_count"] == 1
    assert result["market_return_not_trade_pnl"] is True
    assert (
        result["counterfactual_execution_proven"]
        is result["anchor_is_actual_exit_price"]
        is False
    )
    assert all(
        case["anchor_basis"] == "FIRST_FULLY_AFTER_FLAT_MINUTE_CLOSE_NOT_EXIT_PRICE"
        for case in result["cases"]
    )
    assert "pnl" not in json.dumps(result["cases"]).lower()


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "BINANCE_USDM_LIVE"},
        {"as_of_ms": END + 7 * 60 * MINUTE},
        {"cases": [path_case(trade())] * 7},
    ],
)
def test_cross_environment_future_or_unbounded_paths_are_unknown(changes):
    value = with_paths(report(), path_case(trade()), **changes)
    assert build_execution_review(value)["post_flat_evidence"]["observed_count"] == 0
    assert build_execution_review(value)["totals"]["settled_count"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "INCOMPLETE_OR_INVALID_MINUTE_PATH"},
        {"status": "HORIZON_NOT_YET_OBSERVABLE"},
        {"basis": "EXIT_PRICE"},
        {"flat_ms": START},
        {"observed_end_ms": END + 1},
        {"observed_duration_ms": 1},
        {"anchor_ms": START},
        {"anchor_delay_ms": 0},
        {"horizon_delay_ms": 1},
        {"requested_horizon_minutes": True},
        {"path_digest": "private-episode"},
        {"return_pct": "NaN"},
        {"return_pct": "1e50"},
        {"anchor_price": "0"},
        {"end_price": "50"},
        {"favorable_excursion_pct": "-1"},
        {"adverse_excursion_pct": "1"},
        {"market_return_not_trade_pnl": False},
        {"costs_included": True},
        {"counterfactual_execution_proven": True},
    ],
)
def test_partial_or_inconsistent_paths_never_make_exit_claims(changes):
    case = path_case(trade())
    case["post_flat"].update(changes)
    result = build_execution_review(with_paths(report(), case))
    assert result["post_flat_evidence"]["observed_count"] == 0
    assert result["totals"]["settled_count"] == 1


def test_immature_post_flat_cannot_borrow_later_report_or_path_asof():
    row = trade(hold=1430)
    value = with_paths(report(row), path_case(row), as_of_ms=END + 120 * MINUTE)
    result = build_execution_review(value)
    assert result["totals"]["settled_count"] == 1
    assert result["post_flat_evidence"]["observed_count"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"episode_ref": "another"},
        {"side": "SELL"},
        {"symbol": "ETHUSDT"},
        {"source": "BINANCE_USDM_LIVE"},
        {"actual_net_pnl": "100"},
        {"actual_net_basis": "UNVERIFIED"},
    ],
)
def test_path_must_match_identified_trade_and_verified_net(changes):
    case = {**path_case(trade()), **changes}
    result = build_execution_review(with_paths(report(), case))
    assert result["post_flat_evidence"]["observed_count"] == 0


def test_duplicate_path_case_is_not_double_counted():
    case = path_case(trade())
    result = build_execution_review(with_paths(report(), case, deepcopy(case)))
    assert result["post_flat_evidence"]["observed_count"] == 0


@pytest.mark.parametrize(
    "proof",
    [
        None,
        {},
        {"source": "LIVE"},
        {"source": "CALLER_CLOSED_1M_ARCHIVE", "bar": {"t": START}},
        {
            "source": "CALLER_CLOSED_1M_ARCHIVE",
            "bar": {
                "t": START + 61 * MINUTE,
                "o": "100",
                "h": "101",
                "l": "99",
                "c": "101",
                "v": "1",
            },
        },
    ],
)
def test_missing_or_conflicting_anchor_proof_is_unknown(proof):
    case = path_case(trade())
    case["post_flat"]["anchor_provenance"] = proof
    result = build_execution_review(with_paths(report(), case))
    assert result["post_flat_evidence"]["observed_count"] == 0


def test_real_report_and_post_flat_builders_agree_with_review_schema():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from services.v2_daily_mover_report import build_report
    from services.v2_trade_path_review import post_flat_path
    from v2_core.account_risk import AccountScope
    from v2_core.quant_indicators import Bar

    value = report()
    raw_evidence = value["account_evidence"]
    sample = {
        "symbol": "BTCUSDT",
        "return_pct": "10",
        "open": "100",
        "close": "110",
        "high": "112",
        "low": "99",
        "base_volume": "1000",
        "engine_symbol_supported": True,
    }
    ranking = {
        "start_ms": START,
        "end_ms": END,
        "market_source": "BINANCE_USDM_LIVE",
        "status": "PARTIAL_COVERAGE",
        "coverage": {"discovered": 1, "eligible": 1},
        "winners": [sample],
        "losers": [],
        "eligible_summaries": [sample],
    }
    day = (
        datetime.fromtimestamp(START / 1000, ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )
    actual = build_report(
        day,
        ranking,
        raw_evidence,
        {},
        as_of_ms=value["as_of_ms"],
        scope=AccountScope(**SCOPE),
    )
    actual["account_evidence"] = raw_evidence
    row = raw(value)[0]
    case = path_case(row)
    closed = row["closed_ms"]
    bars = [
        Bar(t=t, o="100", h="110", l="90", c="100", v="1")
        for t in range(closed, closed + 60 * MINUTE, MINUTE)
    ]
    case["post_flat"] = post_flat_path(bars, side="BUY", closed_ms=closed, as_of_ms=END)
    result = build_execution_review(with_paths(actual, case))
    assert result["status"] == "AVAILABLE"
    assert result["totals"]["net_usdt"] == "2.9"
    assert result["post_flat_evidence"]["observed_count"] == 1
    assert result["post_flat_evidence"]["unchanged_end_count"] == 1


def test_module_has_no_io_or_trading_imports():
    import ast
    import inspect

    from services import v2_daily_mover_execution_review as module

    tree = ast.parse(inspect.getsource(module))
    imports = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imports |= {
        name.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
    }
    assert imports <= {
        "hashlib",
        "json",
        "re",
        "collections",
        "decimal",
        "statistics",
        "v2_core",
    }
    assert {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module.startswith("v2_core")
    } == {"v2_core.symbols"}
