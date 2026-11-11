"""Actual pure market evaluation with frozen data; no DB, network or orders."""

import json
from copy import deepcopy
from decimal import Decimal

import pytest

import services.v2_mover_gate_shadow as shadow
from services.v2_trend_entry import evaluate_trend_market
from tests.support.v2_research_market import research_market_from_minutes
from v2_core.evidence import canonical, digest
from v2_core.producer import market_envelope
from v2_core.runtime_policy import resolve


def frozen_sample(*, side="SHORT", volume="8", buy_fraction="0.4", regime="neutral"):
    sign = 1 if side == "LONG" else -1
    candles = []
    for minute in range(1440):
        opened = Decimal(100) + sign * Decimal("0.03") * minute
        closed = opened + sign * Decimal("0.03")
        amount = Decimal(volume) if minute >= 1425 else Decimal(10)
        candles.append(
            {
                "t": minute * 60000,
                "o": str(opened),
                "c": str(closed),
                "h": str(max(opened, closed) + Decimal("0.01")),
                "l": str(min(opened, closed) - Decimal("0.01")),
                "v": str(amount),
                "tbv": str(amount * Decimal(buy_fraction)),
            }
        )
    closed_at = 86400000
    market = research_market_from_minutes(
        candles,
        symbol="BTCUSDT",
        environment="SANDBOX",
        closed_at_ms=closed_at,
    )
    return {
        "evidence_ref": "a" * 64,
        "signal": {
            "symbol": "BTCUSDT",
            "signal": "TREND_UP" if side == "LONG" else "TREND_DOWN",
            "observed_at": closed_at,
            "expires_at_ms": closed_at + 90000,
            "features": {"strength": 20},
        },
        "context": {
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "assembled_at": closed_at + 1,
            "sources": {
                "s3": market_envelope("s3", "SANDBOX", "BTCUSDT", closed_at, market)
            },
            "directional": {
                "market": market,
                "price": market["15m"]["close"],
                "regime": regime,
            },
        },
        "config": {
            "strategy": "S6" if side == "LONG" else "S8",
            "source": "s3",
            "policy": resolve({"entry.trend_structure_enabled": True}),
        },
    }


def metric_sample(ratio="0.8"):
    return {
        "environment": "SANDBOX",
        "config": {"policy": {"trend.min_volume_ratio": "1"}},
        "frozen_metrics": {"volume_ratio": ratio},
    }


def legacy_frozen_sample(**kwargs):
    sample = frozen_sample(**kwargs)
    for key in (
        "trend.breakout_body_guard_enabled",
        "trend.entry_quality_shadow_enabled",
    ):
        sample["config"]["policy"].pop(key)
    return sample


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_registered_legacy_schema_replays_with_explicit_hash_preserving_adapter(side):
    sample = legacy_frozen_sample(
        side=side, buy_fraction="0.6" if side == "LONG" else "0.4"
    )
    original = deepcopy(sample)
    sample["config_digest"] = digest(canonical(sample["config"]))
    row = report(sample)["samples"][0]
    provenance = row["policy_compatibility"]
    assert row["mode"] == "FULL_FROZEN_REPLAY"
    assert row["volume"]["baseline_market_reason"] == "TREND_VOLUME_UNCONFIRMED"
    assert row["volume"]["candidate_market_reason"] == "CANDIDATE"
    assert provenance["mode"] == "EXPLICIT_LEGACY_SCHEMA_ADAPTER"
    assert provenance["migration"] == "PRE_ENTRY_QUALITY_20261107_TO_DISABLED_V1"
    assert provenance["source_schema_digest"] == (
        "63e742ad4be439950afa8fc84faf5ca735cc66672cd4879e668c93dfbb2b8ef0"
    )
    assert provenance["explicit_legacy_values"] == {
        "trend.breakout_body_guard_enabled": False,
        "trend.entry_quality_shadow_enabled": False,
    }
    assert provenance["source_config_digest"] == row["config_digest"]
    assert provenance["evaluation_config_digest"] != row["config_digest"]
    assert provenance["original_evidence_modified"] is False
    assert provenance["historical_code_identity"] == "NOT_PROVEN"
    assert row["input_digest"] == digest(canonical(sample))
    assert sample["config"] == original["config"]
    assert sample["signal"] == original["signal"]
    assert sample["context"] == original["context"]


def test_legacy_adapter_does_not_read_changed_current_defaults(monkeypatch):
    sample = legacy_frozen_sample()
    for key in shadow._LEGACY_DISABLED_FEATURES:
        monkeypatch.setitem(shadow.SCHEMA, key, (True, None, None))
    calls = []

    def observed(signal, context, config):
        calls.append(deepcopy(config))
        return evaluate_trend_market(signal, context, config)

    monkeypatch.setattr(shadow, "evaluate_trend_market", observed)
    assert report(sample)["samples"][0]["mode"] == "FULL_FROZEN_REPLAY"
    assert len(calls) == 2
    assert all(
        config["policy"][key] is False
        for config in calls
        for key in shadow._LEGACY_DISABLED_FEATURES
    )


@pytest.mark.parametrize(
    "defect", ["one_new_key", "additional_missing", "unknown_key", "future_schema"]
)
def test_legacy_adapter_rejects_any_unregistered_schema(defect, monkeypatch):
    sample = legacy_frozen_sample()
    if defect == "one_new_key":
        sample["config"]["policy"]["trend.breakout_body_guard_enabled"] = False
    elif defect == "additional_missing":
        sample["config"]["policy"].pop("trend.max_atr_pct")
    elif defect == "unknown_key":
        sample["config"]["policy"]["unknown.feature"] = False
    else:
        monkeypatch.setitem(shadow.SCHEMA, "future.risk.setting", (True, None, None))
    monkeypatch.setattr(
        shadow, "evaluate_trend_market", lambda *args: pytest.fail("no replay")
    )
    row = report(sample)["samples"][0]
    assert row["mode"] == "UNKNOWN"
    assert row["policy_compatibility"] is None
    assert row["volume"]["candidate_market_reason"] is None


def test_complete_current_policy_keeps_enabled_guard_and_original_hash():
    sample = frozen_sample()
    sample["config"]["policy"].update(
        dict.fromkeys(shadow._LEGACY_DISABLED_FEATURES, True)
    )
    row = report(sample)["samples"][0]
    compatibility = row["policy_compatibility"]
    assert compatibility["mode"] == "EXACT_FROZEN_POLICY"
    assert compatibility["explicit_legacy_values"] == {}
    assert (
        compatibility["source_config_digest"]
        == compatibility["evaluation_config_digest"]
    )
    assert all(
        sample["config"]["policy"][key] is True
        for key in shadow._LEGACY_DISABLED_FEATURES
    )


@pytest.mark.parametrize(
    "regime", ["bull_trend", "range", "weak_bull", "weak_bear", "risk-off"]
)
def test_current_s0_regimes_are_valid_frozen_replay_inputs(regime):
    sample = frozen_sample(regime=regime)
    original = deepcopy(sample)
    row = report(sample)["samples"][0]
    assert row["mode"] == "FULL_FROZEN_REPLAY"
    assert row["regime"]["observed_regime"] == regime
    assert sample == original


def report(sample):
    return shadow.evaluate_mover_gate_shadow([sample], environment="SANDBOX")


def test_real_evaluator_replays_identical_inputs_changing_only_frozen_volume_policy(
    monkeypatch,
):
    sample = frozen_sample()
    original = deepcopy(sample)
    calls = []

    def observed(signal, context, config):
        calls.append(deepcopy((signal, context, config)))
        return evaluate_trend_market(signal, context, config)

    monkeypatch.setattr(shadow, "evaluate_trend_market", observed)
    result = report(sample)
    row = result["samples"][0]
    assert row["mode"] == "FULL_FROZEN_REPLAY"
    assert row["volume"] == {
        "status": "COMPARED",
        "reason": "SINGLE_GATE_COMPARISON_ONLY",
        "baseline_gate": "FAIL",
        "candidate_gate": "PASS",
        "gate_changed": True,
        "volume_ratio": "0.8",
        "baseline_market_reason": "TREND_VOLUME_UNCONFIRMED",
        "candidate_market_reason": "CANDIDATE",
        "other_gate_status": "NO_RECORDED_MARKET_FAILURE",
    }
    assert len(calls) == 2
    assert calls[0][0:2] == calls[1][0:2] == (sample["signal"], sample["context"])
    expected = deepcopy(calls[0][2])
    expected["policy"]["trend.min_volume_ratio"] = "0.75"
    assert calls[1][2] == expected
    assert sample == original
    assert result["summary"]["volume_gate_changed_count"] == 1
    assert result["summary"]["replayed_count"] == 1
    assert result["summary"]["degraded_count"] == 0
    assert result["execution_authorized"] is False
    assert result["not_trade_prediction"] is True
    assert result["automatic_parameter_selection"] is False
    assert result["out_of_sample_validation"] == "NOT_COMPLETED"
    assert result["market_data_scope"] == "SANDBOX_NOT_LIVE"
    assert result["market_source_provenance"] == "NOT_VERIFIED_BY_SHADOW"
    assert "context" not in row and "config" not in row and "signal" not in row


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("flow", "TREND_FLOW_UNCONFIRMED"),
        ("regime", "regime_conflict"),
        ("late_price", "TREND_ENTRY_MOVED"),
    ],
)
def test_actual_other_gate_failure_survives_volume_relaxation(defect, reason):
    sample = frozen_sample(
        side="LONG",
        buy_fraction="0.0664" if defect == "flow" else "0.6",
        regime="risk-off" if defect == "regime" else "neutral",
    )
    if defect == "late_price":
        values = sample["context"]["directional"]["market"]["trend_structure"]["values"]
        sample["context"]["directional"]["price"] = str(
            Decimal(values["close"]) + Decimal(values["atr"]) * 3
        )
    original = deepcopy(sample)
    result = report(sample)
    row = result["samples"][0]
    assert row["volume"]["gate_changed"] is True
    assert row["volume"]["candidate_market_reason"] == reason
    assert row["volume"]["other_gate_status"] == "STILL_FAILS"
    assert result["summary"]["other_gate_still_failed_count"] == 1
    assert row["regime"]["status"] == "UNKNOWN"
    assert row["regime"]["reason"] == "REQUIRES_RELATIVE_STRENGTH_AND_LIVE_VALIDATION"
    assert sample == original


@pytest.mark.parametrize(
    "ratio,baseline,candidate,changed",
    [
        ("0.725", "FAIL", "FAIL", False),
        ("0.642", "FAIL", "FAIL", False),
        ("0.000862", "FAIL", "FAIL", False),
        ("0", "FAIL", "FAIL", False),
        ("0.75", "FAIL", "PASS", True),
        ("1", "PASS", "PASS", False),
    ],
)
def test_metric_projection_compares_only_the_registered_inequality(
    ratio, baseline, candidate, changed, monkeypatch
):
    monkeypatch.setattr(
        shadow,
        "evaluate_trend_market",
        lambda *args: pytest.fail("metric projections cannot replay"),
    )
    result = report(metric_sample(ratio))
    row = result["samples"][0]
    assert row["mode"] == "FROZEN_METRIC_ONLY"
    assert row["volume"]["baseline_gate"] == baseline
    assert row["volume"]["candidate_gate"] == candidate
    assert row["volume"]["gate_changed"] is changed
    assert row["volume"]["other_gate_status"] == "UNKNOWN"
    assert row["volume"]["candidate_market_reason"] is None
    assert (
        result["summary"]["metric_only_count"]
        == result["summary"]["degraded_count"]
        == 1
    )


@pytest.mark.parametrize(
    "change", ["threshold", "config", "environment", "ratio", "unsupported_baseline"]
)
def test_missing_evidence_never_uses_defaults(change, monkeypatch):
    sample = metric_sample()
    if change == "threshold":
        sample["config"]["policy"].clear()
    elif change == "config":
        sample.pop("config")
    elif change == "environment":
        sample.pop("environment")
    elif change == "ratio":
        sample["frozen_metrics"]["volume_ratio"] = None
    else:
        sample["config"]["policy"]["trend.min_volume_ratio"] = "0.5"
    monkeypatch.setattr(
        shadow,
        "evaluate_trend_market",
        lambda *args: pytest.fail("incomplete evidence cannot replay"),
    )
    result = report(sample)
    assert result["summary"]["unknown_count"] == 1
    assert result["summary"]["error_count"] == 0
    assert result["samples"][0]["volume"]["gate_changed"] is None


def test_partial_policy_cannot_replay_even_with_valid_context(monkeypatch):
    sample = frozen_sample()
    sample["config"]["policy"].pop("trend.max_atr_pct")
    sample["frozen_metrics"] = {"volume_ratio": "0.8"}
    monkeypatch.setattr(
        shadow,
        "evaluate_trend_market",
        lambda *args: pytest.fail("historical defaults must be explicit"),
    )
    row = report(sample)["samples"][0]
    assert row["mode"] == "FROZEN_METRIC_ONLY"
    assert row["replay_unavailable_reason"] == "INCOMPLETE_FROZEN_POLICY"


@pytest.mark.parametrize(
    "defect",
    [
        "scope",
        "unbound",
        "stale",
        "missing_regime",
        "symbol",
        "config_digest",
        "metric_mismatch",
        "unknown_policy",
    ],
)
def test_invalid_or_unverifiable_context_cannot_claim_comparable_replay(defect):
    sample = frozen_sample()
    if defect == "scope":
        sample["context"]["environment"] = "LIVE"
    elif defect == "unbound":
        sample["context"]["sources"]["s3"]["symbol"] = "ETHUSDT"
    elif defect == "stale":
        sample["context"]["assembled_at"] += 960000
        sample["signal"]["expires_at_ms"] += 960000
    elif defect == "missing_regime":
        sample["context"]["directional"].pop("regime")
    elif defect == "symbol":
        sample["signal"]["symbol"] = "ETHUSDT"
    elif defect == "config_digest":
        sample["config_digest"] = "b" * 64
    elif defect == "metric_mismatch":
        sample["frozen_metrics"] = {"volume_ratio": "0.99"}
    else:
        sample["config"]["policy"]["unexpected.policy"] = "1"
    result = report(sample)
    assert result["summary"]["unknown_count"] == 1
    assert result["summary"]["replayed_count"] == 0
    assert result["samples"][0]["volume"]["gate_changed"] is None


def test_zero_volume_preserves_unknown_flow_instead_of_inventing_an_observation():
    row = report(frozen_sample(volume="0"))["samples"][0]
    assert row["mode"] == "FULL_FROZEN_REPLAY"
    assert row["volume"]["volume_ratio"] == "0"
    assert row["volume"]["candidate_market_reason"] == "TREND_FLOW_UNCONFIRMED"
    assert row["volume"]["other_gate_status"] == "UNKNOWN"


def test_malformed_rows_are_isolated_and_report_contains_no_sensitive_exception_text():
    secret = metric_sample()
    secret["password"] = "DO_NOT_EXPOSE"
    records = [None, secret, metric_sample("NaN"), metric_sample(), {}]
    result = shadow.evaluate_mover_gate_shadow(records, environment="SANDBOX")
    assert result["summary"] == {
        "sample_count": 5,
        "replayed_count": 0,
        "metric_only_count": 1,
        "volume_gate_changed_count": 1,
        "other_gate_still_failed_count": 0,
        "unknown_count": 4,
        "degraded_count": 5,
        "error_count": 3,
        "regime_unknown_count": 5,
    }
    assert "DO_NOT_EXPOSE" not in json.dumps(result)


@pytest.mark.parametrize("field", ["market", "sources"])
def test_malformed_nested_context_does_not_block_later_rows(field):
    bad = frozen_sample()
    if field == "market":
        bad["context"]["directional"]["market"] = []
    else:
        bad["context"]["sources"] = []
    result = shadow.evaluate_mover_gate_shadow(
        [bad, metric_sample()], environment="SANDBOX"
    )
    assert result["summary"]["error_count"] == 1
    assert result["summary"]["metric_only_count"] == 1
    assert result["samples"][1]["volume"]["gate_changed"] is True


def test_hashes_and_report_are_deterministic_and_plan_is_fixed():
    sample = metric_sample()
    result = report(sample)
    assert result == report(deepcopy(sample))
    assert result["version"] == "mover-gate-shadow-v1"
    assert result["volume_plan"] == {
        "baseline_min_volume_ratio": "1",
        "candidate_min_volume_ratio": "0.75",
    }
    assert result["samples"][0]["input_digest"] == digest(canonical(sample))
    assert result["samples"][0]["config_digest"] == digest(canonical(sample["config"]))
    changed = metric_sample("0.9")
    assert (
        report(changed)["samples"][0]["input_digest"]
        != result["samples"][0]["input_digest"]
    )


def test_scope_and_sample_count_are_bounded():
    assert (
        shadow.evaluate_mover_gate_shadow([], environment="LIVE")["market_data_scope"]
        == "LIVE_OBSERVATIONS_ONLY"
    )
    with pytest.raises(ValueError, match="EXPLICIT_SHADOW_ENVIRONMENT"):
        shadow.evaluate_mover_gate_shadow([], environment="TEST")
    with pytest.raises(ValueError, match="BOUNDED_SHADOW_SAMPLES"):
        shadow.evaluate_mover_gate_shadow([{}] * 101, environment="SANDBOX")
    with pytest.raises(ValueError, match="BOUNDED_SHADOW_SAMPLES"):
        shadow.evaluate_mover_gate_shadow(iter([]), environment="SANDBOX")
