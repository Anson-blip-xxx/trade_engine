"""Corrected features through all existing version consumers and admission."""

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_v2_directional import inputs
from test_v2_directional_replay import SCOPE, scenario, worker
from test_v2_indicator_contract_v3 import varied_batch
from test_v2_indicator_v3_flow import archived_stage
from test_v2_indicator_v4_flow import archived_reference
from test_v2_s3_candles import batch

from services.v2_directional_replay import MARKET_ONLY_REJECTIONS, market_plan
from services.v2_market_hub_entry import configured_feature_version
from services.v2_risk_reference import ArchivedRiskReference
from services.v2_s0_regime import build_s0_state
from services.v2_s3_candles import CORRECTED_VERSION, PRICE_VERSION, build_frame
from services.v2_s3_source import frame_digest
from services.v2_shared_market import IncrementalCandles
from services.v2_shared_risk_reference import SharedRiskReference
from services.v2_strategy_correctness_review import compare_frame
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import canonical
from v2_core.producer import market_envelope

database = database_fixture


def test_daemon_selection_is_explicit_and_legacy_default_is_unchanged():
    assert configured_feature_version({}) == PRICE_VERSION
    assert configured_feature_version({"V2_MARKET_FEATURE_VERSION": CORRECTED_VERSION}) == CORRECTED_VERSION


@pytest.mark.parametrize("version", ["", "s3-closed-1m-v1", "s3-closed-1m-v3", "s3-closed-1m-v4", "s3-closed-1m-v7", None])
def test_daemon_rejects_unknown_or_unqualified_feature_versions(version):
    with pytest.raises(ValueError, match="MARKET_DAEMON_FEATURE_VERSION_UNSUPPORTED"):
        configured_feature_version({"V2_MARKET_FEATURE_VERSION": version})


def test_corrected_incremental_producer_and_attested_shared_reference_agree():
    source = batch()
    at = source["closed_at"]
    archive = SimpleNamespace(read=lambda *_: source["candles"]["BTCUSDT"])
    cache = SimpleNamespace(hget=lambda *_: None)
    transport = SimpleNamespace(environment="SANDBOX")
    producer = IncrementalCandles(
        transport, cache, archive, feature_version=CORRECTED_VERSION
    )
    frame = producer.prepare("BTCUSDT", at)["frame"]
    assert frame["frame_id"] == f"s3-shared-v5:BTCUSDT:{at}"
    assert IncrementalCandles(transport, cache, archive).feature_version == PRICE_VERSION
    head = [at, frame["frame_id"], frame_digest(frame)]
    provider = SharedRiskReference(
        lambda: pytest.fail("read-only head injected"), scope=SCOPE,
        archive=archive, clock_ms=lambda: at + 1,
    )
    provider._head = lambda _: tuple(head)
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}
    assert provider(request).price == "101"
    head[2] = frame_digest(build_frame(source, environment="SANDBOX", feature_version=PRICE_VERSION))
    with pytest.raises(ValueError, match="REFERENCE_INPUT_MISMATCH"):
        provider(request)


def test_archived_corrected_reference_requires_exact_version_receipt():
    provider, head = archived_reference(CORRECTED_VERSION)
    assert isinstance(provider, ArchivedRiskReference)
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}
    assert provider(request).price == "101"
    head[0] = head[0].replace(CORRECTED_VERSION, PRICE_VERSION)
    with pytest.raises(ValueError, match="REFERENCE_INPUT_MISMATCH"):
        provider(request)


def test_corrected_s0_reconstruction_and_mixed_basket_rejection():
    stage, _, published = archived_stage(CORRECTED_VERSION)
    assert stage.run_once()["status"] == "PROJECTED"
    assert len(published) == 1
    frame = build_frame(batch(), environment="SANDBOX", feature_version=CORRECTED_VERSION)
    windows = frame["windows"]
    windows["ETHUSDT"] = deepcopy(windows["BTCUSDT"])
    kwargs = {"universe": list(windows), "s3_frame_id": "test", "s3_input_digest": "a" * 64}
    assert build_s0_state(windows, **kwargs)["evidence"]["closed_at"] == frame["observed_at"]
    windows["ETHUSDT"]["input_evidence"]["window_version"] = PRICE_VERSION
    with pytest.raises(ValueError):
        build_s0_state(windows, **kwargs)


def market_scenario():
    facts = inputs()
    frame = build_frame(batch(), environment="SANDBOX", feature_version=CORRECTED_VERSION)
    facts["market"]["input_evidence"] = frame["windows"]["BTCUSDT"]["input_evidence"]
    signal = {
        "symbol": "BTCUSDT", "signal": "TREND_UP", "observed_at": frame["observed_at"],
        "features": facts["event"],
    }
    context = {
        "symbol": "BTCUSDT", "environment": "SANDBOX",
        "assembled_at": frame["observed_at"] + 2, "market_only": True,
        "sources": {
            "s3": market_envelope("s3", "SANDBOX", "BTCUSDT", frame["observed_at"], facts["market"])
        },
        "directional": {
            "market": facts["market"], "price": facts["price"],
            "regime": facts["regime"], "short_ratio": None,
        },
    }
    config = {"source": "s3", "strategy": "S6", "policy": {"entry.confirmation_version": 2}}
    return signal, context, config


def test_market_screen_forwards_original_typed_indicator_contract():
    signal, context, config = market_scenario()
    before = deepcopy(context)
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "CANDIDATE"
    assert proof["confirmation_version"] == 2
    assert proof["indicator_evidence"] == context["directional"]["market"]["input_evidence"]
    assert type(proof["indicator_evidence"]["bar_count"]) is int
    assert proof["reward_estimate_kind"] == "REALIZED_MOVE_PROXY_NOT_FORECAST"
    assert proof["indicator_evidence_bound"] is True
    assert context == before


@pytest.mark.parametrize("defect", ["future", "negative", "boolean", "minute", "environment", "source", "interval", "source_time", "source_symbol", "different_features"])
def test_corrected_contract_is_bound_to_time_environment_and_source(defect):
    signal, context, config = market_scenario()
    evidence = context["directional"]["market"]["input_evidence"]
    source = context["sources"]["s3"]
    if defect == "future":
        evidence["closed_at"] += 60000
    elif defect == "negative":
        evidence["closed_at"] = -60000
    elif defect == "boolean":
        evidence["closed_at"] = True
    elif defect == "minute":
        evidence["closed_at"] += 1
    elif defect == "environment":
        evidence["environment"] = "LIVE"
    elif defect == "source":
        evidence["source"] = "OTHER"
    elif defect == "interval":
        evidence["interval"] = "1h"
    elif defect == "source_time":
        source["observed_at"] -= 60000
    elif defect == "source_symbol":
        source["symbol"] = "ETHUSDT"
    else:
        source["features"]["15m"]["close"] = "1"
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "indicator_evidence_unconfirmed"
    assert proof["indicator_evidence_bound"] is False


def test_missing_contract_is_a_terminal_market_rejection_not_global_retry():
    signal, context, config = market_scenario()
    del context["directional"]["market"]["input_evidence"]
    plan, _ = market_plan(signal, context, config)
    assert plan.reason == "indicator_evidence_unconfirmed"
    assert plan.reason in MARKET_ONLY_REJECTIONS
    assert "takeover_flow_unconfirmed" in MARKET_ONLY_REJECTIONS


def test_old_policy_does_not_add_new_evidence_or_reject_old_frames():
    signal, context, config = market_scenario()
    del context["directional"]["market"]["input_evidence"]
    config["policy"] = {}
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "CANDIDATE"
    assert "confirmation_version" not in proof
    assert "indicator_evidence" not in proof


def test_same_input_review_does_not_invent_counterfactual_pnl_or_mutate_input():
    source = varied_batch()
    before = deepcopy(source)
    result = compare_frame(source, regime="neutral")
    assert source == before
    assert result["execution_authorized"] is False
    assert result["profitability_validated"] is False
    assert result["alternate_orders_or_pnl"] is None
    assert result["historical_availability_at_entry"] == "NOT_VERIFIED"
    assert result["full_account_or_jev_admission"] == "NOT_RUN"
    assert len(result["input_digest"]) == 64
    assert result["features"][PRICE_VERSION]["15m"]["ema20"] == "101"
    assert result["features"][CORRECTED_VERSION]["15m"]["ema20"] != "101"
    assert len(result["probes"]) <= 64


def test_same_input_review_tags_event_probes_not_emitted_by_other_version():
    source = batch()
    # A last-minute surge with sufficient prior observations: old PUMP takes
    # the vacuous EMA fallback, corrected features apply a real distance.
    source["candles"]["BTCUSDT"][0].update(o="101", c="120", h="121", l="100", v="100", tbv="65")
    result = compare_frame(source, regime="neutral")
    pumps = [p for p in result["probes"] if p["signal"] == "PUMP_UP"]
    assert pumps
    for probe in pumps:
        assert probe["event_origin"] in (PRICE_VERSION, CORRECTED_VERSION)
        new = probe["variants"]["INDICATORS_AND_CONFIRMATION"]
        assert new["plan"]["reason"] == "takeover_not_ready"
        assert new["confirmation_version"] == 2


def test_incompatible_signal_is_consumed_and_does_not_block_following_signal(database):
    from services.v2_directional_replay import replay_decision

    replay, _, signal_id, context = scenario(database)
    replay.decide = lambda signal, ctx, cfg: replay_decision(
        signal, ctx, {**cfg, "policy": {"entry.confirmation_version": 2}}
    )
    context["market_only"] = True
    result = replay.consume(signal_id, context=context)
    assert result["status"] == "IGNORED"
    assert result["reason"] == "indicator_evidence_unconfirmed"
    saved = replay.decision(signal_id)
    proof = saved["snapshot"]["features"]["evaluation"]["source_scoring"]
    assert proof["confirmation_version"] == 2
    assert proof["indicator_evidence"] is None
    # Another strategy receipt progresses despite the rejected first one.
    following, _, next_id, next_context = scenario(database, "S8")
    assert following.consume(next_id, context=next_context)["reason"] == "DIRECTIONAL_REPLAY_ONLY"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
    restarted, _ = worker(database, now=4)
    restarted.decide = lambda *_: pytest.fail("retry must reuse frozen rejection")
    assert restarted.consume(signal_id, context={})["reason"] == "indicator_evidence_unconfirmed"
    assert canonical(restarted.decision(signal_id)) == canonical(saved)
