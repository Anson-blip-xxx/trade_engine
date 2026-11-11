"""Partial TV deployment must not narrow the universe or gate local v6 entries.

Only synthetic market inputs and the isolated QA database are used. No live
signal, model request, exchange order or runtime policy mutation is allowed.
"""

import json
from copy import deepcopy

import pytest
from test_v2_dynamic_fusion_context import enable
from test_v2_shared_market import (  # noqa: F401
    Catalog,
    catalog_response,
    database,
    shared,
)
from test_v2_shared_market import scenario as stored_scenario
from test_v2_trend_entry import scenario

from services.v2_directional_replay import market_plan, replay_decision
from services.v2_jev_reference import public_features
from services.v2_shared_market_context import DynamicMarketContext
from v2_core.jev_advisory import prepare_request
from v2_core.signal_fusion import FusionPolicy, build_fusion, validated_public_features


def peer(own_signal, **changes):
    return {
        "signal_id": "qa-partial-tv",
        "source": "tv_bridge",
        "environment": "SANDBOX",
        **{
            key: own_signal[key]
            for key in ("symbol", "signal", "observed_at", "expires_at_ms")
        },
        **changes,
    }


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize(
    "absence", ["never_received", "expired", "outside_window", "other_symbol"]
)
def test_local_v6_candidate_without_tv_keeps_baseline_and_jev_unknown(side, absence):
    signal, context, config = scenario(side)
    baseline, _ = market_plan(signal, context, config)
    baseline_replay = json.loads(replay_decision(signal, context, config).features_json)
    now = context["assembled_at"]
    opposite = "TREND_DOWN" if side == "LONG" else "TREND_UP"
    peers = {
        "never_received": [],
        "expired": [peer(signal, signal=opposite, expires_at_ms=now)],
        "outside_window": [peer(signal, signal=opposite, observed_at=now - 60000)],
        "other_symbol": [peer(signal, signal=opposite, symbol="ETHUSDT")],
    }[absence]
    proof = build_fusion(
        signal,
        source="s3",
        peers=peers,
        policy=FusionPolicy(enabled=True),
        policy_version=1,
        now_ms=now,
    )
    context["signal_fusion"] = proof
    assert proof["status"] == "ABSENT"
    assert proof["score_bonus"] == 0 and proof["veto"] is False
    assert market_plan(signal, context, config)[0] == baseline
    assert baseline.reason == "CANDIDATE"
    result = replay_decision(signal, context, config)
    assert result.rationale == "DIRECTIONAL_REPLAY_ONLY"
    evidence = json.loads(result.features_json)
    assert evidence["sizing"] == baseline_replay["sizing"]
    features = public_features(
        context["sources"]["s3"],
        target=signal["symbol"],
        direction=side,
        context=context,
        now_ms=now,
    )
    # JevEntryContext adds the independently validated fusion facts after the
    # candle extractor; inspect the normalized request, not just its first stage.
    features.update(validated_public_features(proof, now))
    request = prepare_request(features, now_ms=now, max_age_ms=60000)
    assert request["request"]["state"]["tv_confirmation"] is None
    assert request["request"]["state"]["tv_observed_at_ms"] is None
    assert features["indicator_version"] == "s3-closed-1m-v6"
    # Optional TV is not an exemption from mandatory account/market gates.
    context["directional"]["sizing"]["available_margin"] = "0"
    assert replay_decision(signal, context, config).rationale == "INSUFFICIENT_BUDGET"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_tv_candidate_does_not_require_matching_local_event(side):
    signal, context, config = scenario(side, source="tv_bridge")
    context["signal_fusion"] = build_fusion(
        signal,
        source="tv_bridge",
        peers=[],
        policy=FusionPolicy(enabled=True),
        policy_version=1,
        now_ms=context["assembled_at"],
    )
    assert market_plan(signal, context, config)[0].reason == "CANDIDATE"
    assert (
        replay_decision(signal, context, config).rationale == "DIRECTIONAL_REPLAY_ONLY"
    )
    # Fresh local market data is still required; a TV message cannot replace it.
    context["directional"]["market"].pop("trend_structure")
    assert (
        market_plan(signal, context, config)[0].reason == "TREND_STRUCTURE_UNCONFIRMED"
    )


def test_partial_tv_database_evidence_does_not_leak_to_uncovered_symbols(shared):  # noqa: F811
    Catalog(shared, "SANDBOX", lambda: 3).save(
        catalog_response(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    )
    _, runtime, _, _ = stored_scenario(shared)
    assert enable(shared).code == "APPLIED"

    class Delegate:
        def with_signal_fusion(self, signal, proof):
            return {"symbol": signal["symbol"], "signal_fusion": proof}

    provider = DynamicMarketContext(
        shared, delegate=Delegate(), clock_ms=lambda: 3, source="s3"
    )
    base = {
        "symbol": "BTCUSDT",
        "signal": "TREND_UP",
        "observed_at": 2,
        "expires_at_ms": 100,
        "features": {},
    }
    runtime.data.signals.admit(
        source="tv_bridge",
        environment="SANDBOX",
        request_key="qa-covered-btc",
        snapshot=deepcopy(base),
    )
    for symbol, expected in (
        ("BTCUSDT", "CONFIRMS"),
        ("ETHUSDT", "ABSENT"),
        ("SOLUSDT", "ABSENT"),
    ):
        result = provider({**base, "symbol": symbol})
        assert result["universe_admission"]["eligible"] is True
        assert result["signal_fusion"]["status"] == expected
        assert result["signal_fusion"]["veto"] is False
    # A fresh contradiction affects only the matching symbol, never the universe.
    runtime.data.signals.admit(
        source="tv_bridge",
        environment="SANDBOX",
        request_key="qa-covered-btc-conflict",
        snapshot={**base, "signal": "TREND_DOWN", "observed_at": 3},
    )
    assert provider(base)["signal_fusion"]["veto"] is True
    assert (
        provider({**base, "symbol": "ETHUSDT"})["signal_fusion"]["status"] == "ABSENT"
    )
    with shared() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
        assert {
            row[0]
            for row in conn.execute(
                "SELECT symbol FROM v2_market_demands WHERE environment='SANDBOX'"
            )
        } == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
