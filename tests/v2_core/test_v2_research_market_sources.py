"""Research source checks use only frozen fixtures, never public/private I/O."""

from copy import deepcopy

import pytest

from services.v2_daily_mover_coverage import build_coverage_review
from services.v2_mover_gate_shadow import evaluate_mover_gate_shadow
from tests.v2_core.test_v2_daily_mover_coverage import (
    load as catalog,
)
from tests.v2_core.test_v2_daily_mover_coverage import (
    report as coverage_report,
)
from tests.v2_core.test_v2_daily_mover_replay import fixture, load, rehash
from tests.v2_core.test_v2_mover_gate_shadow import frozen_sample
from v2_core.producer import market_envelope
from v2_core.research_market_sources import (
    frozen_source_review,
    market_comparison_contract,
)
from v2_core.signal_fusion import FusionPolicy, build_fusion


def sample():
    result = frozen_sample()
    result["context"]["freshness_policy"] = {
        "s3": {"scope": "SYMBOL", "max_age_ms": 60000}
    }
    result["context"]["valid_until_ms"] = 86460001
    return result


def shadow(value):
    return evaluate_mover_gate_shadow([value], environment="SANDBOX")


@pytest.mark.parametrize(
    "source,environment,expected",
    [
        ("BINANCE_USDM_LIVE", "SANDBOX", False),
        ("BINANCE_USDM_TESTNET", "SANDBOX", True),
        ("BINANCE_USDM_LIVE", "LIVE", True),
        ("BINANCE_FUTURES", "SANDBOX", None),
    ],
)
def test_environment_contract_never_grants_profit_or_execution(
    source, environment, expected
):
    result = market_comparison_contract(
        research_source=source, execution_environment=environment
    )
    assert result["environment_match"] is expected
    for key in (
        "cross_environment_pnl_permitted",
        "executable_profit_proven",
        "execution_authorized",
        "catalog_absence_is_strategy_rejection",
        "historical_listing_proven_by_current_catalog",
    ):
        assert result[key] is False


@pytest.mark.parametrize("value", [None, [], {}, "PRIVATE_UNKNOWN_SOURCE"])
def test_invalid_source_labels_remain_unknown_without_exporting_input(value):
    result = market_comparison_contract(
        research_source=value, execution_environment=value
    )
    assert (
        result["research_environment"] == result["execution_environment"] == "UNKNOWN"
    )
    assert result["environment_match"] is None
    assert frozen_source_review({}, environment=value)["replay_blocked"] is True


def test_shadow_exposes_bound_source_and_legacy_freshness_unknown_without_mutation():
    current = sample()
    original = deepcopy(current)
    result = shadow(current)
    source = result["samples"][0]["market_provenance"]
    assert source["source_snapshot_digest_checked"] is True
    assert source["recorded_freshness_confirmed"] is True
    assert source["source_host"] is None
    assert source["source_authenticity"] == "NOT_INDEPENDENTLY_VERIFIED"
    assert result["summary"]["replayed_count"] == 1
    assert current == original
    legacy = shadow(frozen_sample())["samples"][0]
    assert legacy["mode"] == "FULL_FROZEN_REPLAY"
    assert legacy["market_provenance"]["freshness"] == "UNKNOWN"
    assert legacy["market_provenance"]["recorded_freshness_confirmed"] is False


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("expired_context", "FROZEN_CONTEXT_EXPIRED"),
        ("stale_source", "FROZEN_SOURCE_EXPIRED_OR_FUTURE"),
        ("future_source", "FROZEN_SOURCE_TIME_INVALID"),
        ("digest", "FROZEN_SOURCE_DIGEST_MISMATCH"),
    ],
)
def test_explicit_bad_sources_cannot_become_candidate_confirmation(defect, reason):
    value = sample()
    context = value["context"]
    if defect == "expired_context":
        context["valid_until_ms"] = context["assembled_at"]
    elif defect == "stale_source":
        context["freshness_policy"]["s3"]["max_age_ms"] = 0
    elif defect == "future_source":
        context["directional"]["market"]["input_evidence"]["closed_at"] += 2
    else:
        context["sources"]["s3"]["snapshot_id"] = "f" * 64
    result = shadow(value)
    row = result["samples"][0]
    assert row["volume"]["reason"] == reason
    assert row["market_provenance"]["replay_blocked"] is True
    assert row["market_provenance"]["recorded_freshness_confirmed"] is False
    assert result["summary"]["replayed_count"] == 0
    assert result["summary"]["volume_gate_changed_count"] == 0


def test_expired_auxiliary_source_is_not_hidden_by_fresh_s3():
    value = sample()
    value["context"]["freshness_policy"]["s0"] = {"scope": "GLOBAL", "max_age_ms": 1}
    value["context"]["sources"]["s0"] = market_envelope(
        "s0", "SANDBOX", "*", 86400000 - 1, {}
    )
    row = shadow(value)["samples"][0]
    assert row["volume"]["reason"] == "FROZEN_SOURCE_EXPIRED_OR_FUTURE"


def test_incomplete_source_freshness_rules_do_not_confirm_all_sources():
    value = sample()
    value["context"]["sources"]["s0"] = market_envelope(
        "s0", "SANDBOX", "*", 86400000, {}
    )
    result = frozen_source_review(value, environment="SANDBOX")
    assert result["freshness"] == "UNKNOWN"
    assert result["recorded_freshness_confirmed"] is False


def test_known_live_market_host_cannot_be_relabelled_as_sandbox():
    value = sample()
    value["context"]["directional"]["market"]["input_evidence"]["source_host"] = (
        "fapi.binance.com"
    )
    result = frozen_source_review(value, environment="SANDBOX")
    assert result["reason"] == "FROZEN_SOURCE_ENVIRONMENT_MISMATCH"
    assert result["replay_blocked"] is True


def test_tv_confirmation_reuses_proof_freshness_and_never_claims_chart_environment():
    value = sample()
    now = value["context"]["assembled_at"]
    peer = {
        "signal_id": "public-tv-peer",
        "source": "tv_bridge",
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "signal": "TREND_DOWN",
        "observed_at": now - 1,
        "expires_at_ms": now + 1000,
    }
    value["context"]["signal_fusion"] = build_fusion(
        value["signal"],
        source="s3",
        peers=[peer],
        policy=FusionPolicy(enabled=True),
        policy_version=1,
        now_ms=now,
    )
    current = frozen_source_review(value, environment="SANDBOX")
    assert current["tv_confirmation"] == "CONFIRMS"
    assert current["tv_market_environment"] == "UNKNOWN"
    value["context"]["assembled_at"] = now + 1000
    expired = frozen_source_review(value, environment="SANDBOX")
    assert expired["tv_confirmation"] == "UNKNOWN"
    assert expired["replay_blocked"] is False


def test_replay_persists_comparison_contract_and_precise_source_failure():
    report, row = fixture()
    report["market_source"] = "BINANCE_USDM_LIVE"
    result = load(report, [row])
    assert result["market_comparison"]["environment_match"] is False
    assert (
        result["samples"][0]["market_provenance"]["source_snapshot_digest_checked"]
        is True
    )
    row[11]["features"]["context"]["valid_until_ms"] = 86400001
    rehash(report, row)
    result = load(report, [row])
    assert result["unknown_reasons"] == {"FROZEN_CONTEXT_EXPIRED": 1}
    assert result["summary"]["new_market_candidate_count"] == 0


def test_coverage_keeps_current_listing_separate_from_strategy_rejection():
    snapshot = catalog()
    snapshot["rows"][1].update(
        present=False, status="ABSENT_FROM_CURRENT_CATALOG", supported=None
    )
    result = build_coverage_review(coverage_report(), snapshot)
    assert (
        result["market_comparison"]["comparison_scope"]
        == "CROSS_ENVIRONMENT_RESEARCH_ONLY"
    )
    assert result["rows"][1]["current_listing"] == "ABSENT_FROM_CURRENT_CATALOG"
    assert result["rows"][1]["historical_evidence"] == "NO_RETAINED_SIGNAL_UNKNOWN"
