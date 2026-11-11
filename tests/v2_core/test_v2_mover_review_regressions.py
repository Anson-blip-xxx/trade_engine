"""Review-boundary regressions: frozen cutoff, source and setup conditions.

Pure fixtures only. No database, venue I/O or execution-policy mutation.
"""

from copy import deepcopy

import pytest

from services.v2_daily_mover_report import (
    _gate_shadow,
    _shadow_structure_class,
    build_report,
)
from services.v2_mover_gate_shadow import evaluate_mover_gate_shadow
from tests.v2_core.test_v2_daily_mover_report import (
    DAY,
    NOW,
    SCOPE,
    inputs,
    trade,
)
from tests.v2_core.test_v2_mover_gate_shadow import frozen_sample, metric_sample, report
from tests.v2_core.test_v2_mover_reason_digest import report_with_directional


def _details():
    return deepcopy(
        report_with_directional()["rows"][0]["evidence"]["rejection_details"]
    )


def _build_at_source_cutoff(*, cutoff=None, trades=(), details=None):
    ranking, evidence, paths = inputs()
    evidence["as_of_ms"] = DAY["end_ms"] if cutoff is None else cutoff
    raw = evidence["symbols"]["UPUSDT"]
    raw["trades"] = list(trades)
    if details is not None:
        raw.update(
            signal_count=1,
            by_strategy={
                "s6": {
                    "classification_version": "directional-entry-events-v2",
                    "candidate_count": 1,
                    "stage_counts": {"MARKET_FILTER_REJECTED": 1},
                    "reason_counts": [
                        {
                            "stage": "MARKET_FILTER_REJECTED",
                            "reason": "TREND_VOLUME_UNCONFIRMED",
                            "count": 1,
                        }
                    ],
                    "rejection_details": details,
                }
            },
        )
    return build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)


@pytest.mark.parametrize(
    "field", ["decided_ms", "opened_ms", "closed_ms", "settled_ms"]
)
def test_later_report_clock_never_admits_trade_beyond_frozen_source(field):
    # The old implementation checked NOW and accepted this future-of-source row.
    assert DAY["end_ms"] + 1 < NOW
    with pytest.raises(ValueError, match="DAILY_REPORT_FUTURE_TRADE"):
        _build_at_source_cutoff(trades=[trade(**{field: DAY["end_ms"] + 1})])


def test_old_source_can_be_reformatted_without_losing_valid_settled_trade():
    result = _build_at_source_cutoff(trades=[trade(settled_ms=DAY["end_ms"])])
    assert (
        result["rows"][0]["evidence"]["trades"][0]["actual_settled_net_usdt"] == "2.9"
    )


@pytest.mark.parametrize("cutoff", [DAY["end_ms"] - 1, NOW + 1, True, None])
def test_source_cutoff_must_cover_completed_day_and_not_exceed_report(cutoff):
    ranking, evidence, paths = inputs()
    evidence["as_of_ms"] = cutoff
    with pytest.raises(ValueError, match="DAILY_REPORT_EVIDENCE_SCOPE_MISMATCH"):
        build_report(DAY, ranking, evidence, paths, as_of_ms=NOW, scope=SCOPE)


@pytest.mark.parametrize("field", ["observed_ms", "decided_ms", "completed_ms"])
def test_later_report_clock_drops_rejection_examples_beyond_source(field):
    details = _details()
    details[0]["reason_samples"][0][field] = DAY["end_ms"] + 1
    result = _build_at_source_cutoff(details=details)
    evidence = result["rows"][0]["evidence"]
    assert evidence["rejection_details"][0]["reason_samples"] == []
    assert evidence["reason_counts"][0]["count"] == 1
    assert result["gate_shadow"]["summary"]["sample_count"] == 0


@pytest.mark.parametrize(
    "container,field",
    [
        ("operands", "assembled_at_ms"),
        ("operands", "structure_last_closed_at_ms"),
        ("operands", "jev.checked_at_ms"),
        ("market_sources", "market_closed_at_ms"),
        ("market_sources", "s3_observed_at_ms"),
    ],
)
def test_frozen_metric_observation_cannot_cross_source_cutoff(container, field):
    details = _details()
    details[0]["reason_samples"][0][container][field] = DAY["end_ms"] + 1
    result = _build_at_source_cutoff(details=details)
    assert result["rows"][0]["evidence"]["rejection_details"][0]["reason_samples"] == []


def test_planned_expiry_after_cutoff_is_not_misclassified_as_future_observation():
    details = _details()
    details[0]["reason_samples"][0]["operands"]["jev.model_expires_at_ms"] = NOW + 1
    result = _build_at_source_cutoff(details=details)
    assert (
        len(result["rows"][0]["evidence"]["rejection_details"][0]["reason_samples"])
        == 1
    )


@pytest.mark.parametrize(
    "sources,mode,error,reason",
    [
        ({}, "UNKNOWN", False, "MISSING_FROZEN_MARKET_ENVIRONMENT"),
        ({"market_environment": "LIVE"}, "UNKNOWN", True, "INVALID_FROZEN_INPUT"),
        (
            {"market_environment": "SANDBOX", "s3_environment": "LIVE"},
            "UNKNOWN",
            True,
            "INVALID_FROZEN_INPUT",
        ),
        (
            {"market_environment": "SANDBOX"},
            "FROZEN_METRIC_ONLY",
            False,
            "SINGLE_GATE_COMPARISON_ONLY",
        ),
        (
            {"structure_environment": "SANDBOX"},
            "FROZEN_METRIC_ONLY",
            False,
            "SINGLE_GATE_COMPARISON_ONLY",
        ),
    ],
)
def test_report_shadow_uses_actual_market_environment(sources, mode, error, reason):
    details = _details()
    details[0]["reason_samples"][0]["market_sources"] = sources
    sample = report_with_directional(rejection_details=details)["gate_shadow"][
        "samples"
    ][0]
    assert sample["mode"] == mode
    assert sample["error"] is error
    assert sample["volume"]["reason"] == reason
    if mode == "UNKNOWN":
        assert sample["volume"]["gate_changed"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("market_host", "fapi.binance.com"),
        ("host", "fapi.binance.com"),
        ("market_source", "BINANCE_USDM_LIVE"),
        ("structure_source", "BINANCE_USDM_LIVE"),
        ("s3_source", "BINANCE_USDM_LIVE"),
    ],
)
def test_report_shadow_rejects_known_live_source_despite_sandbox_label(field, value):
    details = _details()
    details[0]["reason_samples"][0]["market_sources"] = {
        "market_environment": "SANDBOX",
        field: value,
    }
    shadow = report_with_directional(rejection_details=details)["gate_shadow"]
    sample = shadow["samples"][0]
    assert sample["error"] is True
    assert sample["volume"]["reason"] == "INVALID_FROZEN_INPUT"
    assert sample["volume"]["gate_changed"] is None
    assert shadow["summary"]["metric_only_count"] == 0
    assert value not in str(sample)


@pytest.mark.parametrize(
    "environment,field,value",
    [
        ("LIVE", "market_host", "demo-fapi.binance.com"),
        ("LIVE", "market_source", "BINANCE_USDM_TESTNET"),
        ("LIVE", "market_source", "BINANCE_USDM_SANDBOX"),
        ("LIVE", "structure_source", "BINANCE_USDM_SANDBOX"),
        ("SANDBOX", "market_host", "fapi.binance.com"),
    ],
)
def test_source_contradiction_is_rejected_in_both_environment_directions(
    environment, field, value
):
    frozen = metric_sample()
    frozen.update(
        environment=environment,
        require_market_environment=True,
        market_sources={"market_environment": environment, field: value},
    )
    shadow = evaluate_mover_gate_shadow([frozen], environment=environment)
    sample = shadow["samples"][0]
    assert sample["error"] is True
    assert sample["volume"]["status"] == "UNKNOWN"
    assert value not in str(sample)


@pytest.mark.parametrize(
    "environment,sources",
    [
        ("SANDBOX", {}),
        ("SANDBOX", {"market_source": "BINANCE_FUTURES"}),
        (
            "SANDBOX",
            {
                "market_source": "BINANCE_USDM_TESTNET",
                "market_host": "demo-fapi.binance.com",
            },
        ),
        ("SANDBOX", {"market_source": "BINANCE_USDM_SANDBOX"}),
        ("LIVE", {"market_source": "BINANCE_FUTURES"}),
        (
            "LIVE",
            {"market_source": "BINANCE_USDM_LIVE", "market_host": "fapi.binance.com"},
        ),
        ("SANDBOX", {"market_host": "unknown.private.example?token=never-echo"}),
        ("LIVE", {"market_source": "UNKNOWN_PROVIDER_PRIVATE"}),
    ],
)
def test_missing_generic_unknown_or_consistent_provenance_never_claims_authentication(
    environment, sources
):
    frozen = metric_sample()
    frozen.update(
        environment=environment,
        require_market_environment=True,
        market_sources={"market_environment": environment, **sources},
    )
    shadow = evaluate_mover_gate_shadow([frozen], environment=environment)
    sample = shadow["samples"][0]
    assert sample["mode"] == "FROZEN_METRIC_ONLY"
    assert sample["error"] is False
    assert shadow["market_source_provenance"] == "NOT_VERIFIED_BY_SHADOW"
    assert (
        shadow["market_source_notice"]
        == "ENVIRONMENT_LABEL_IS_NOT_MARKET_SOURCE_AUTHENTICATION"
    )
    assert "never-echo" not in str(shadow)
    assert "UNKNOWN_PROVIDER_PRIVATE" not in str(shadow)


def test_known_host_without_explicit_market_environment_does_not_invent_it():
    frozen = metric_sample()
    frozen.update(
        require_market_environment=True,
        market_sources={"market_host": "demo-fapi.binance.com"},
    )
    sample = report(frozen)["samples"][0]
    assert sample["volume"]["reason"] == "MISSING_FROZEN_MARKET_ENVIRONMENT"
    assert sample["mode"] == "UNKNOWN"


def test_full_replay_checks_known_source_host_not_only_projected_metric_mode():
    frozen = frozen_sample(regime="range")
    frozen["context"]["directional"]["market"]["input_evidence"]["source_host"] = (
        "fapi.binance.com"
    )
    sample = report(frozen)["samples"][0]
    assert sample["error"] is True
    assert sample["mode"] == "UNKNOWN"
    assert sample["volume"]["reason"] == "INVALID_FROZEN_INPUT"


def test_liquidity_source_does_not_replace_or_conflict_with_volume_bar_provenance():
    frozen = metric_sample()
    frozen.update(
        require_market_environment=True,
        market_sources={
            "market_environment": "SANDBOX",
            "market_host": "demo-fapi.binance.com",
            "liquidity_environment": "LIVE",
            "liquidity_host": "fapi.binance.com",
        },
    )
    assert report(frozen)["samples"][0]["mode"] == "FROZEN_METRIC_ONLY"


@pytest.mark.parametrize("regime", ["bull_trend", "range"])
@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_real_s0_regime_labels_replay_without_synthetic_neutral_default(regime, side):
    frozen = frozen_sample(
        side=side, buy_fraction="0.6" if side == "LONG" else "0.4", regime=regime
    )
    sample = report(frozen)["samples"][0]
    assert sample["mode"] == "FULL_FROZEN_REPLAY"
    assert sample["regime"]["observed_regime"] == regime


@pytest.mark.parametrize("regime", ["unregistered", "bear_trend", "BULL_TREND", ""])
def test_unregistered_regime_still_remains_unknown(regime):
    sample = report(frozen_sample(regime=regime))["samples"][0]
    assert sample["mode"] == "UNKNOWN"
    assert sample["volume"]["reason"] == "UNKNOWN_OBSERVED_REGIME"
    assert sample["volume"]["gate_changed"] is None


def _setup(direction, **changes):
    if direction == "BUY":
        values = {
            "breakout_long": False,
            "prior3_touched_ema20_long": True,
            "previous_close": "99",
            "previous_open": "100",
            "close": "102",
            "previous_high": "101",
        }
    else:
        values = {
            "breakout_short": False,
            "prior3_touched_ema20_short": True,
            "previous_close": "101",
            "previous_open": "100",
            "close": "98",
            "previous_low": "99",
        }
    return {"operands": {**values, **changes}}


@pytest.mark.parametrize("direction,suffix", [("BUY", "long"), ("SELL", "short")])
def test_setup_classification_is_directional_frozen_conditions_only(direction, suffix):
    assert (
        _shadow_structure_class(_setup(direction), direction)
        == "PULLBACK_RESUME_CONDITION"
    )
    assert (
        _shadow_structure_class(
            _setup(direction, **{f"breakout_{suffix}": True}), direction
        )
        == "BREAKOUT_CONDITION"
    )
    assert (
        _shadow_structure_class(
            _setup(direction, **{f"prior3_touched_ema20_{suffix}": False}), direction
        )
        == "NO_FRESH_SETUP"
    )
    assert (
        _shadow_structure_class(
            _setup(direction, **{f"breakout_{suffix}": 1}), direction
        )
        == "UNKNOWN"
    )
    assert (
        _shadow_structure_class(
            _setup(direction, **{f"prior3_touched_ema20_{suffix}": None}), direction
        )
        == "UNKNOWN"
    )
    assert (
        _shadow_structure_class(_setup(direction, previous_close=None), direction)
        == "UNKNOWN"
    )
    assert _shadow_structure_class({"operands": {}}, direction) == "UNKNOWN"


@pytest.mark.parametrize("direction,edge", [("BUY", "101"), ("SELL", "99")])
def test_pullback_resume_requires_strict_directional_break_of_previous_edge(
    direction, edge
):
    assert (
        _shadow_structure_class(_setup(direction, close=edge), direction)
        == "NO_FRESH_SETUP"
    )
    assert (
        _shadow_structure_class(_setup(direction, previous_close="100"), direction)
        == "PULLBACK_RESUME_CONDITION"
    )
    unfavorable_previous = "101" if direction == "BUY" else "99"
    assert (
        _shadow_structure_class(
            _setup(direction, previous_close=unfavorable_previous), direction
        )
        == "NO_FRESH_SETUP"
    )


def test_structure_cohorts_count_unique_bounded_examples_not_orders_or_profit():
    detail = _details()[0]
    base = detail["reason_samples"][0]
    buy = deepcopy(base)
    buy.update(
        evidence_ref="a" * 64,
        operands={**_setup("BUY")["operands"], "volume_ratio": "0.8"},
    )
    sell = deepcopy(base)
    sell.update(
        evidence_ref="b" * 64,
        event="TREND_DOWN",
        operands={
            **_setup("SELL", breakout_short=True)["operands"],
            "volume_ratio": "0.5",
        },
    )
    unknown = deepcopy(base)
    unknown.update(evidence_ref="c" * 64, market_sources={}, operands={})
    rows = [
        {
            "symbol": "UPUSDT",
            "direction": "BUY",
            "evidence": {
                "rejection_details": [
                    {**detail, "reason_samples": [buy, deepcopy(buy), unknown]}
                ]
            },
        },
        {
            "symbol": "DOWNUSDT",
            "direction": "SELL",
            "evidence": {"rejection_details": [{**detail, "reason_samples": [sell]}]},
        },
    ]
    before = deepcopy(rows)
    shadow = _gate_shadow(rows, environment="SANDBOX")
    assert rows == before
    assert shadow["summary"]["sample_count"] == 3
    cohorts = {
        (r["side"], r["structure_class"]): r for r in shadow["structure_cohorts"]
    }
    assert cohorts[("BUY", "PULLBACK_RESUME_CONDITION")]["volume_gate_changed"] == 1
    assert cohorts[("SELL", "BREAKOUT_CONDITION")]["volume_gate_changed"] == 0
    assert cohorts[("BUY", "UNKNOWN")]["unknown_volume"] == 1
    assert sum(r["sample_count"] for r in cohorts.values()) == 3
    assert sum(r["full_replay"] for r in cohorts.values()) == 0
    assert shadow["execution_authorized"] is False
    assert "NOT_ADMISSION_OR_REALIZED_PROFIT" in shadow["structure_cohort_basis"]
