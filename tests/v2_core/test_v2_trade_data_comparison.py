"""Offline reports never turn cohort selection into hypothetical returns."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scripts.v2_trade_data_comparison import (
    DAY,
    band,
    build_report,
    load,
    load_tv_funnel,
)
from v2_core.directional import directional_events

NOW = 30 * DAY


def trade(identity="one", **changes):
    return {
        "id": identity,
        "opened_ms": NOW - DAY,
        "closed_ms": NOW - DAY + 60000,
        "decided_ms": 0,
        "net_pnl": "-0.2",
        "maintenance": False,
        "held_minutes": "1",
        "fees_usdt": "0.3",
        "funding_usdt": "0",
        "other_cash_usdt": "0",
        "gross_usdt": "0.1",
        "cash_residual_usdt": "0",
        "source": "s3",
        "side": "BUY",
        "entry_mode": "RIGHT_MOMENTUM",
        "liquidation": False,
        **changes,
    }


def test_costs_not_subtracted_twice_and_zero_pnl_separate():
    report = build_report(
        [trade(), trade("even", net_pnl="0", gross_usdt="0.3")], as_of_ms=NOW
    )
    stats = report["windows_by_actual_open_time"][2]
    assert Decimal(stats["net_pnl"]) == Decimal("-0.2")
    assert stats["wins"] == 0 and stats["losses"] == 1 and stats["breakeven"] == 1
    assert stats["cost_flipped_winners"] == 1
    assert (
        not report["execution_authorized"] and not report["automatic_parameter_updates"]
    )


def test_cohort_uses_open_time_not_decision_or_settlement():
    report = build_report(
        [trade(opened_ms=NOW - 2 * DAY, decided_ms=NOW, closed_ms=NOW)], as_of_ms=NOW
    )
    assert report["windows_by_actual_open_time"][0]["actual_opened"] == 0
    assert report["windows_by_actual_open_time"][1]["settled_trades"] == 1


def test_open_trade_not_zero_return_and_maintenance_visible():
    report = build_report(
        [
            trade(net_pnl=None, closed_ms=None, held_minutes=None),
            trade("maintenance", maintenance=True, net_pnl="999"),
        ],
        as_of_ms=NOW,
    )
    stats = report["windows_by_actual_open_time"][2]
    assert stats["actual_opened"] == 2 and stats["settled_trades"] == 0
    assert stats["unsettled_or_unvalued"] == stats["maintenance_excluded"] == 1


def test_liquidation_retained_in_baseline_and_sensitive_subgroup_only():
    report = build_report(
        [trade(net_pnl="-60", liquidation=True), trade("normal", net_pnl="-3")],
        as_of_ms=NOW,
    )
    a, b, c = report["execution_attribution_14d"]
    assert tuple(Decimal(v["net_pnl"]) for v in (a, b, c)) == (
        Decimal(-63),
        Decimal(-60),
        Decimal(-3),
    )
    assert report["external_data_comparison_status"].startswith("NOT_AVAILABLE")


def test_weekly_slices_disjoint_at_boundary():
    rows = [
        trade("older", opened_ms=NOW - 7 * DAY - 1),
        trade("boundary", opened_ms=NOW - 7 * DAY),
        trade("oldest", opened_ms=NOW - 14 * DAY),
        trade("now", opened_ms=NOW, closed_ms=NOW),
    ]
    report = build_report(rows, as_of_ms=NOW)
    assert [r["summary"]["actual_opened"] for r in report["weekly_stability"]] == [2, 2]


def test_drawdown_is_closed_pnl_not_equity():
    rows = [
        trade("a", net_pnl="5", closed_ms=1),
        trade("b", net_pnl="-4", closed_ms=2),
        trade("c", net_pnl="1", closed_ms=3),
        trade("d", net_pnl="-3", closed_ms=4),
    ]
    stats = build_report(rows, as_of_ms=NOW)["windows_by_actual_open_time"][2]
    assert Decimal(stats["closed_trade_cumulative_drawdown_usdt"]) == 6
    assert not stats["drawdown_is_account_equity_drawdown"]


def test_missing_costs_not_replaced_with_zero():
    stats = build_report([trade(fees_usdt=None)], as_of_ms=NOW)[
        "windows_by_actual_open_time"
    ][2]
    assert stats["fees_usdt"] is None and stats["fees_usdt_missing"] == 1


def test_groups_do_not_depend_on_profits():
    rows = [trade(), trade("two", source="tv_bridge", side="SELL")]
    other = deepcopy(rows)
    other[0]["net_pnl"] = "9999"
    first = build_report(rows, as_of_ms=NOW)["source_side_14d"]
    second = build_report(other, as_of_ms=NOW)["source_side_14d"]
    assert [(r["source"], r["side"], r["settled_trades"]) for r in first] == [
        (r["source"], r["side"], r["settled_trades"]) for r in second
    ]


def test_exact_decimals_preserved():
    stats = build_report(
        [
            trade(net_pnl="0.123456789123456789"),
            trade("b", net_pnl="0.000000000000000001"),
        ],
        as_of_ms=NOW,
    )["windows_by_actual_open_time"][2]
    assert Decimal(stats["net_pnl"]) == Decimal("0.123456789123456790")


def test_duplicate_and_future_refused():
    with pytest.raises(ValueError, match="DUPLICATE"):
        build_report([trade(), trade()], as_of_ms=NOW)
    with pytest.raises(ValueError, match="FUTURE"):
        build_report([trade(opened_ms=NOW + 1)], as_of_ms=NOW)


def test_feature_band_boundaries_and_missing():
    thresholds = ((60, "LOW"), (80, "MID"))
    assert [band(v, thresholds) for v in (None, 59, 60, 79, 80)] == [
        "UNKNOWN",
        "LOW",
        "MID",
        "MID",
        "HIGH",
    ]
    with pytest.raises(ValueError, match="NONFINITE"):
        band("NaN", thresholds)


def test_readwrite_database_rejected_before_query():
    class ReadWrite:
        def execute(self, query):
            assert query == "SHOW transaction_read_only"
            return self

        def fetchone(self):
            return ("off",)

    with pytest.raises(ValueError, match="READ_ONLY_REQUIRED"):
        load(ReadWrite(), as_of_ms=NOW)
    with pytest.raises(ValueError, match="READ_ONLY_REQUIRED"):
        load_tv_funnel(ReadWrite(), {}, as_of_ms=NOW)


class ReadFixture:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        self.answer = next(self.answers)
        return self

    def fetchone(self):
        return self.answer

    def fetchall(self):
        return self.answer


def test_tv_funnel_counts_distinct_signals_not_consumer_sum():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = ReadFixture(
        [
            ("on",),
            [("a", at, "TREND_UP"), ("b", at, "TREND_DOWN")],
            [
                ("a", "EXPIRED", "EXPIRED_BEFORE_SCHEDULING", at),
                ("b", "IGNORED", "score_below_minimum", at),
            ],
            [("b", "IGNORED", "score_below_minimum", at)],
            [
                ("a", "IGNORED", "overextended", at),
                ("b", "IGNORED", "score_below_minimum", at),
            ],
            [
                ("a", "IGNORED", "overextended", at),
                ("b", "IGNORED", "score_below_minimum", at),
            ],
        ]
    )
    scope = {
        "exchange": "X",
        "account_id": "fixture",
        "environment": "SANDBOX",
        "product": "FUTURES",
    }
    r = load_tv_funnel(conn, scope, as_of_ms=NOW)
    assert (
        r["inbound_distinct_signals"]
        == r["decision_distinct_signals_any_consumer"]
        == 2
    )
    assert sum(c["decision_consumer_signal_count"] for c in r["consumers"]) == 3
    assert r["direction_applicable"]["inbound_distinct_signals"] == 2
    assert r["direction_applicable"]["decision_distinct_signals"] == 1
    assert r["direction_applicable"]["expired_distinct_signals"] == 1
    assert r["opposite_direction"]["decision_distinct_signals"] == 2
    assert "snapshot->>'signal'" in conn.queries[1][0]
    assert r["latest_inbound_at"].endswith("+08:00")
    assert all(
        "fixture" in params[0] for sql, params in conn.queries if "consumer=%s" in sql
    )


def test_tv_truncation_refuses_complete_claim():
    conn = ReadFixture([("on",), [("a", None, "TREND_UP"), ("b", None, "TREND_DOWN")]])
    with pytest.raises(ValueError, match="REFUSE_TRUNCATED_TV"):
        load_tv_funnel(conn, {"environment": "SANDBOX"}, as_of_ms=NOW, max_signals=1)


def tv_fixture(signals, *, receipts=((), ()), decisions=((), ())):
    return ReadFixture(
        [("on",), signals, receipts[0], decisions[0], receipts[1], decisions[1]]
    )


def tv_scope():
    return {
        "exchange": "BINANCE",
        "account_id": "fixture",
        "environment": "SANDBOX",
        "product": "FUTURES",
    }


def test_tv_direction_cohorts_separate_opposite_and_unsupported():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = tv_fixture(
        [
            ("up", at, "PULSE_UP"),
            ("down", at, "PANIC_SELL"),
            ("other", at, "BOOKKEEPING"),
        ],
        receipts=(
            [
                ("up", "IGNORED", "overextended", at),
                ("down", "EXPIRED", "signal expired before scheduling", at),
                ("other", "EXPIRED", "signal expired before scheduling", at),
            ],
            [
                ("up", "EXPIRED", "signal expired before scheduling", at),
                ("down", "EXPIRED", "signal expired before evaluation", at),
                ("other", "EXPIRED", "signal expired before scheduling", at),
            ],
        ),
        decisions=(
            [("up", "IGNORED", "overextended", at)],
            [("down", "EXPIRED", "signal expired before evaluation", at)],
        ),
    )
    report = load_tv_funnel(conn, tv_scope(), as_of_ms=NOW)
    relevant = report["direction_applicable"]
    opposite = report["opposite_direction"]
    unsupported = report["unsupported_direction"]
    assert (
        relevant["inbound_distinct_signals"]
        == relevant["decision_distinct_signals"]
        == 2
    )
    assert relevant["expired_receipt_distinct_signals"] == 1
    assert relevant["expired_decision_distinct_signals"] == 1
    assert relevant["expired_distinct_signals"] == 1  # Same expiry, not two signals.
    assert opposite["expired_distinct_signals"] == 2
    assert opposite["decision_distinct_signals"] == 0
    assert (
        unsupported["inbound_distinct_signals"]
        == unsupported["expired_distinct_signals"]
        == 1
    )
    assert (
        unsupported["receipt_consumer_signal_count"] == 2
    )  # Two consumers, one signal.
    for consumer in report["consumers"]:
        assert (
            sum(
                consumer[k]["inbound_distinct_signals"]
                for k in (
                    "direction_applicable",
                    "opposite_direction",
                    "unsupported_direction",
                )
            )
            == consumer["inbound_distinct_signals"]
        )
        assert consumer["direction_applicable"]["inbound_distinct_signals"] == 1


def test_tv_classification_matches_shared_registry_including_quant_events():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    long_events = set(directional_events("S6"))
    short_events = set(directional_events("S8"))
    signals = [(event, at, event) for event in sorted(long_events | short_events)]
    signals += [("missing", at, None), ("unknown", at, "FUTURE_UNKNOWN_EVENT")]
    report = load_tv_funnel(tv_fixture(signals), tv_scope(), as_of_ms=NOW)
    for consumer in report["consumers"]:
        own, other = (
            (long_events, short_events)
            if consumer["producer"] == "s6"
            else (short_events, long_events)
        )
        assert consumer["direction_applicable"]["inbound_distinct_signals"] == len(own)
        assert consumer["opposite_direction"]["inbound_distinct_signals"] == len(
            other - own
        )
        assert consumer["unsupported_direction"]["inbound_distinct_signals"] == 2
        assert consumer["direction_applicable"]["unreceipted_distinct_signals"] == len(
            own
        )


def test_tv_empty_cohort_has_explicit_zero_denominators():
    report = load_tv_funnel(tv_fixture([]), tv_scope(), as_of_ms=NOW)
    assert report["inbound_distinct_signals"] == 0
    assert report["latest_inbound_at"] is None
    for key in ("direction_applicable", "opposite_direction", "unsupported_direction"):
        values = report[key]
        assert (
            values["inbound_distinct_signals"]
            == values["expired_distinct_signals"]
            == 0
        )
        assert (
            values["receipt_outcome_reasons"] == values["decision_action_reasons"] == []
        )
        assert values["latest_decision_at"] is None


def test_tv_duplicate_rows_cannot_inflate_denominators_or_expiries():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    signal = ("a", at, "TREND_UP")
    receipt = ("a", "EXPIRED", "signal expired before evaluation", at)
    decision = ("a", "EXPIRED", "signal expired before evaluation", at)
    conn = tv_fixture(
        [signal, signal],
        receipts=([receipt, receipt], [receipt, receipt]),
        decisions=([decision, decision], [decision, decision]),
    )
    report = load_tv_funnel(conn, tv_scope(), as_of_ms=NOW)
    assert report["inbound_distinct_signals"] == 1
    assert report["decision_distinct_signals_any_consumer"] == 1
    assert sum(r["decision_consumer_signal_count"] for r in report["consumers"]) == 2
    for key in ("direction_applicable", "opposite_direction"):
        assert report[key]["inbound_distinct_signals"] == 1
        assert report[key]["expired_distinct_signals"] == 1
        assert report[key]["receipt_consumer_signal_count"] == 1
        assert report[key]["receipt_outcome_reasons"][0]["consumer_signal_count"] == 1


@pytest.mark.parametrize("conflict", ("SIGNAL", "RECEIPT", "DECISION"))
def test_tv_conflicting_identity_refused_instead_of_selecting_result(conflict):
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    signals = [("a", at, "TREND_UP")]
    records = [("a", "IGNORED", "overextended", at), ("a", "EXPIRED", "expired", at)]
    if conflict == "SIGNAL":
        signals.append(("a", at, "TREND_DOWN"))
    conn = tv_fixture(
        signals,
        receipts=(records if conflict == "RECEIPT" else [], []),
        decisions=(records if conflict == "DECISION" else [], []),
    )
    with pytest.raises(ValueError, match="CONFLICTING_TV_" + conflict):
        load_tv_funnel(conn, tv_scope(), as_of_ms=NOW)


def test_tv_outside_cohort_receipt_refused():
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = tv_fixture([], receipts=([("outside", "EXPIRED", "expired", at)], []))
    with pytest.raises(ValueError, match="TV_RECORD_OUTSIDE_INBOUND_COHORT"):
        load_tv_funnel(conn, tv_scope(), as_of_ms=NOW)


def test_missing_initial_stop_not_inferred_from_return():
    stats = build_report([trade()], as_of_ms=NOW)["windows_by_actual_open_time"][2]
    assert stats["initial_risk_units"]["r_sample_count"] == 0
    assert stats["initial_risk_units"]["r_missing_count"] == 1
    assert stats["initial_risk_units"]["historical_mean_net_r"] is None


def test_entry_mode_side_groups_preserve_all_actual_trades():
    rows = [
        trade(),
        trade("two", side="SELL"),
        trade("three", entry_mode="S6B_TREND_TAKEOVER"),
    ]
    report = build_report(rows, as_of_ms=NOW)
    groups = report["entry_mode_side_14d"]
    assert len(groups) == 3
    assert sum(g["settled_trades"] for g in groups) == 3


def test_load_liquidation_uses_order_root_evidence_and_bounded_fills(monkeypatch):
    row = trade(score=90, raw_strength=99, opening_notional="2", closing_notional="2.1")
    monkeypatch.setattr(
        "scripts.v2_trade_data_comparison.trade_review",
        lambda *args, **kwargs: {"truncated": False, "trades": [row]},
    )
    conn = ReadFixture(
        [
            ("on",),
            [("BINANCE", "fixture", "SANDBOX", "FUTURES", "alias")],
            (False,),
            [("one", {"peers": []}, "range", "0.001", "2", True, "evidence")],
            [],
        ]
    )
    rows, _ = load(conn, as_of_ms=NOW)
    assert rows[0]["liquidation"] and rows[0]["exit_reason"] == "EXCHANGE_LIQUIDATION"
    assert Decimal(rows[0]["cash_residual_usdt"]) == 0
    sql, params = conn.queries[3]
    assert "o.request_evidence->>'kind'='LIQUIDATION'" in sql
    assert "o.request_evidence->>'origin'='BINANCE_EXTERNAL_CLOSE'" in sql
    assert "o.leg='CLOSE'" in sql and "f.occurred_at_ms<=%s" in sql
    assert "f.created_at<=to_timestamp(%s/1000.0)" in sql
    assert params[:2] == (NOW, NOW)


def test_load_refuses_decision_window_boundary_gap():
    conn = ReadFixture(
        [("on",), [("BINANCE", "fixture", "SANDBOX", "FUTURES", "alias")], (True,)]
    )
    with pytest.raises(ValueError, match="REFUSE_INTENT_OPEN_TIME_BOUNDARY_GAP"):
        load(conn, as_of_ms=NOW)
