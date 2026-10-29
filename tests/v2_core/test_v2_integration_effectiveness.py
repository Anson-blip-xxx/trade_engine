"""Fixed-horizon integration research cannot invent trades or use future inputs."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from scripts.v2_integration_effectiveness import timestamp
from services.v2_integration_effectiveness import (
    EffectivenessPolicy,
    build_report,
    candidate_from_record,
    fixed_horizon_markout,
    jev_classification,
    load_records,
    load_tv_funnel,
    tv_classification,
)
from v2_core.account_risk import AccountScope

POLICY = EffectivenessPolicy((1, 2), "5", "2", minimum_matched_pairs=2)


def record(identity="a", **changes):
    return {
        "decision_id": identity,
        "evidence_ref": "e-" + identity,
        "signal_id": "s-" + identity,
        "symbol": "BTCUSDT",
        "decided_at_ms": 65000,
        "source": "s3",
        "action": "OPEN",
        "side": "BUY",
        "strategy": "S6",
        "config_digest": "config",
        "strategy_version": "v1",
        "regime": "BULL",
        "evaluation": {
            "admission_authorized": True,
            "market_plan": {"side": "LONG", "reason": "CANDIDATE"},
            "jev_entry_gate": {"status": "PASS", "reason": "JEV_SECOND_FILTER_PASSED"},
        },
        "actual_filled": False,
        "actual_settled_net_usdt": None,
        **changes,
    }


def veto(identity="b", **changes):
    r = record(identity, action="IGNORED", side=None, **changes)
    r["evaluation"] = {
        "original_admission": {
            "admission_authorized": True,
            "market_plan": {"side": "LONG"},
        },
        "jev_entry_gate": {"status": "VETO", "reason": "JEV_CHASE_RISK_HIGH"},
    }
    return r


def bars(start=120000, count=2):
    return [
        {"t": start + i * 60000, "o": "100", "h": "105", "l": "98", "c": "102"}
        for i in range(count)
    ]


def markout(r=None, data=None, **kwargs):
    return fixed_horizon_markout(
        candidate_from_record(r or record()),
        data if data is not None else bars(),
        horizon_minutes=kwargs.pop("horizon_minutes", 2),
        as_of_ms=kwargs.pop("as_of_ms", 300000),
        policy=POLICY,
        **kwargs,
    )


def report(records, read_bars=None, **kwargs):
    return build_report(
        records,
        read_bars=read_bars
        or (lambda target, start, end: bars(start, (end - start) // 60000)),
        start_ms=1,
        as_of_ms=kwargs.pop("as_of_ms", 500000),
        policy=POLICY,
        **kwargs,
    )


def test_next_minute_entry_never_uses_bar_already_open_at_decision():
    value = markout(
        data=[{"t": 60000, "o": "1", "h": "900", "l": "1", "c": "900"}, *bars()]
    )
    assert value["entry_at_ms"] == 120000
    assert value["gross_return_bps"] == "200.00000000"
    assert value["reason"] == "MARKOUT_NOT_STRATEGY_PNL"


def test_exact_minute_decision_still_uses_next_minute_not_same_open():
    value = markout(record(decided_at_ms=60000))
    assert value["entry_at_ms"] == 120000


def test_long_short_costs_and_excursions_are_symmetric_but_costs_adverse():
    long = markout()
    short = markout(record(side="SELL", strategy="S8"))
    assert Decimal(long["net_return_bps"]) < Decimal(long["gross_return_bps"])
    assert Decimal(short["net_return_bps"]) < Decimal(short["gross_return_bps"])
    assert long["gross_mfe_bps"] == "500.00000000"
    assert long["gross_mae_bps"] == "-200.00000000"
    assert short["gross_mfe_bps"] == "200.00000000"
    assert short["gross_mae_bps"] == "-500.00000000"
    expected = (
        (Decimal(102) * Decimal("0.9998")) / (Decimal(100) * Decimal("1.0002")) - 1
    ) * 10000
    expected -= Decimal(5) * (
        1 + (Decimal(102) * Decimal("0.9998")) / (Decimal(100) * Decimal("1.0002"))
    )
    assert abs(Decimal(long["net_return_bps"]) - expected) < Decimal("0.00000001")


def test_unclosed_horizon_is_censored_not_zero():
    value = markout(as_of_ms=239999)
    assert value["status"] == "CENSORED"
    assert value["net_return_bps"] is None


def test_missing_middle_bar_is_unknown_even_with_endpoints():
    value = markout(data=[bars()[0], {**bars()[1], "t": 240000}])
    assert value["reason"] == "BAR_HISTORY_INCOMPLETE"
    assert value["net_return_bps"] is None


def test_identical_duplicates_are_deduped_conflicts_unknown():
    assert markout(data=[*bars(), bars()[0]])["status"] == "COMPLETE"
    value = markout(data=[*bars(), {**bars()[0], "c": "103"}])
    assert value["reason"] == "BAR_DATA_INVALID_OR_CONFLICTING"


@pytest.mark.parametrize(
    "change", [{"h": "99"}, {"l": "103"}, {"o": "0"}, {"h": "NaN"}, {"c": True}]
)
def test_invalid_ohlc_cannot_be_a_markout(change):
    value = markout(data=[{**bars()[0], **change}, bars()[1]])
    assert value["status"] == "UNKNOWN"


def test_future_bars_cannot_change_past_horizon_or_decision_labels():
    before = report([record(), veto()])
    future = {"t": 9999990000, "o": "1", "h": "999999", "l": "1", "c": "999999"}
    after = report(
        [record(), veto()],
        lambda target, start, end: [*bars(start, (end - start) // 60000), future],
    )
    assert before == after


@pytest.mark.parametrize(
    "gate,expected",
    [
        ({"status": "VETO", "reason": "PREEXISTING_REJECTION"}, "UNCLASSIFIED"),
        (
            {"status": "SKIP", "reason": "JEV_RESULT_UNAVAILABLE"},
            "UNAVAILABLE_OR_SKIPPED",
        ),
        ({"status": "PASS", "reason": "JEV_GATE_DISABLED"}, "DISABLED"),
        ({"status": "FALLBACK", "reason": "JEV_BASELINE_FALLBACK"}, "FALLBACK"),
        ({"status": "VETO", "reason": "JEV_TREND_UNSUPPORTED"}, "VETO"),
        ({"status": "PASS", "reason": "FORGED"}, "UNCLASSIFIED"),
        ({}, "NOT_REVIEWED"),
    ],
)
def test_non_model_failures_never_count_as_model_veto(gate, expected):
    assert jev_classification(gate) == expected


def test_duplicate_decisions_do_not_double_count_denominators():
    result = report([record(), deepcopy(record()), veto()])
    assert result["decision_count"] == 2
    assert result["excluded_records"] == {"DUPLICATE_IDENTICAL_DECISION": 1}
    assert result["jev_counts"] == {"PASS": 1, "VETO": 1}
    altered = record()
    altered["evaluation"]["jev_entry_gate"]["hypotheses"] = {"trend_supported": "0.9"}
    with pytest.raises(ValueError, match="DUPLICATE_DECISION_CONFLICT"):
        report([record(), altered])


def test_missing_censored_and_invalid_candidates_not_in_complete_denominator():
    def archive(target, start, end):
        return [] if target == "MISSINGUSDT" else bars(start, (end - start) // 60000)

    skipped = record(
        "skip",
        evaluation={
            "jev_entry_gate": {"status": "SKIP", "reason": "JEV_RESULT_UNAVAILABLE"}
        },
    )
    result = report(
        [
            record(),
            record("missing", symbol="MISSINGUSDT"),
            record("recent", decided_at_ms=490000),
            skipped,
        ],
        archive,
    )
    group = next(g for g in result["groups"] if g["jev"] == "PASS")
    assert group["horizons"]["2"]["candidates"] == 3
    assert group["horizons"]["2"]["complete"] == 1
    assert group["horizons"]["2"]["positive_markout_fraction"] == "1.00000000"
    assert group["horizons"]["2"]["trade_win_rate"] is None


def test_match_is_same_direction_regime_version_day_and_non_reused():
    result = report(
        [
            record(),
            record("a2"),
            veto(),
            veto("wrong-regime", regime="BEAR"),
            veto("wrong-config", config_digest="other"),
        ]
    )
    match = result["matched_jev_comparisons"][0]
    assert match["matched_pairs"] == 1
    assert match["status"] == "INSUFFICIENT_MATCHED_SAMPLE"
    assert match["causal_improvement_proven"] is False
    assert match["unmatched_eligible"] == {"PASS": 1, "VETO": 2}


def test_unknown_regime_is_not_assumed_comparable():
    result = report([record(regime=None), veto(regime=None)])
    assert all(m["matched_pairs"] == 0 for m in result["matched_jev_comparisons"])


def test_actual_pnl_is_separate_from_markout_no_implicit_hypothetical_fill():
    result = report(
        [record(actual_filled=True, actual_settled_net_usdt="-12.5"), veto()]
    )
    assert result["actual_filled_count"] == result["actual_settled_count"] == 1
    assert result["actual_settled_net_usdt"] == "-12.5"
    assert (
        result["execution_authorized"] is result["automatic_parameter_updates"] is False
    )
    assert all(g["horizons"]["1"]["strategy_pnl"] is None for g in result["groups"])
    assert report([veto()])["actual_settled_net_usdt"] is None


def test_archive_failure_excluded_not_fabricated_loss_or_success():
    def fail(*args):
        raise OSError("sensitive connection details must not escape")

    result = report([record()], fail)
    assert result["rows"][0]["markouts"]["2"]["reason"] == "ARCHIVE_READ_UNAVAILABLE"
    assert "sensitive" not in str(result)


def test_tv_legacy_association_is_not_effective_fusion():
    assert tv_classification("s3", {"peers": []}) == "LEGACY_ASSOCIATION_ONLY"
    assert tv_classification("s3", {}) == "NO_FUSION_EVIDENCE"
    assert tv_classification("tv_bridge", {}) == "TV_DIRECT"


def test_tv_confirmation_does_not_claim_extra_admission_when_already_candidate():
    row = record()
    proof = {
        "version": "directional-fusion-v1",
        "status": "CONFIRMS",
        "veto": False,
        "peers": [{"source": "tv_bridge", "relationship": "CONFIRMS"}],
        "baseline_plan": {"reason": "CANDIDATE"},
        "fused_plan": {"reason": "CANDIDATE"},
    }
    row["evaluation"]["signal_fusion"] = proof
    item = candidate_from_record(row)
    assert item["tv"] == "TV_PEER_CONFIRMS"
    assert item["tv_decision_effect"] == "NO_ADMISSION_FLIP"
    proof["baseline_plan"]["reason"] = "score_below_minimum"
    assert (
        candidate_from_record(row)["tv_decision_effect"] == "SOFT_SCORE_ADMISSION_FLIP"
    )
    proof["status"], proof["peers"] = "ABSENT", []
    assert candidate_from_record(row)["tv"] == "S3_WITHOUT_TV_PEER"


def test_future_decisions_excluded_before_archive_calls():
    seen = []
    result = report([record(decided_at_ms=999999)], lambda *args: seen.append(args))
    assert not seen
    assert result["decision_count"] == 0
    assert result["excluded_records"] == {"OUTSIDE_AS_OF_WINDOW": 1}


def test_policy_bounds_and_timezone_explicit():
    for changes in (
        {"horizons_minutes": (0,)},
        {"horizons_minutes": (2, 1)},
        {"horizons_minutes": (1, 1)},
        {"fee_per_side_bps": "-1"},
        {"slippage_per_side_bps": "NaN"},
        {"minimum_matched_pairs": True},
    ):
        with pytest.raises(ValueError):
            replace(POLICY, **changes)
    with pytest.raises(ValueError, match="EXPLICIT_TIMEZONE_REQUIRED"):
        timestamp("2026-10-23T10:00:00")
    assert timestamp("2026-10-23T10:00:00+08:00") == timestamp(
        "2026-10-23T02:00:00+00:00"
    )


def test_loader_requires_read_only_before_any_business_query():
    class Connection:
        def execute(self, sql):
            assert sql == "SHOW transaction_read_only"
            return self

        def fetchone(self):
            return ("off",)

    with pytest.raises(ValueError, match="READ_ONLY_REQUIRED"):
        load_records(
            Connection(),
            AccountScope("BINANCE", "qa", "SANDBOX", "FUTURES"),
            start_ms=1,
            as_of_ms=1000,
        )


def test_loaders_use_the_real_strategy_consumer_encoding():
    class Connection:
        def execute(self, sql, params=None):
            self.sql, self.params = sql, params
            if params:
                encoded = '["BINANCE","qa","SANDBOX","FUTURES","s6"]'
                assert any(isinstance(p, list) and encoded in p for p in params)
            return self

        def fetchone(self):
            return ("on",) if self.sql.startswith("SHOW") else (0, 0, 0, 0, 0)

        def fetchall(self):
            return []

    conn, scope = Connection(), AccountScope("BINANCE", "qa", "SANDBOX", "FUTURES")
    assert load_records(conn, scope, start_ms=1, as_of_ms=1000) == ([], False)
    assert (
        load_tv_funnel(conn, scope, start_ms=1, as_of_ms=1000)[
            "environment_received_signals"
        ]
        == 0
    )
