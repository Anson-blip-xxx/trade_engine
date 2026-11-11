"""Fresh, bounded optional-reference evidence never replaces hard admission."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_v2_directional_admission import formal
from test_v2_directional_replay import scenario

from services.v2_directional_context import DirectionalContext
from tests.support.v2_admission import database as database_fixture
from v2_core.jev_entry_gate import GateAssessment
from v2_core.signal_fusion import FusionPolicy, build_fusion

database = database_fixture


def fallback(original, **changes):
    evidence = {
        "status": "FALLBACK",
        "reason": "JEV_BASELINE_FALLBACK",
        "fallback_cause": "TOKEN_BUDGET_EXHAUSTED",
        "policy": {"enabled": True, "unavailable_action": "BASELINE"},
        "execution_authorized": False,
        "risk_override": False,
        "valid_until_ms": 9,
        **changes,
    }
    return GateAssessment(
        "FALLBACK", "JEV_BASELINE_FALLBACK", original, json.dumps(evidence)
    )


@pytest.mark.parametrize(
    "cause",
    [
        "TOKEN_BUDGET_EXHAUSTED",
        "PROVIDER_REQUEST_REJECTED",
        "MODEL_CONFIGURATION_UNAVAILABLE",
        "MODEL_TIMEOUT",
    ],
)
def test_missing_model_tokens_preserves_risk_decision_and_one_order(database, cause):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    worker.entry_filter = SimpleNamespace(
        assess=lambda original, **_: fallback(original, fallback_cause=cause)
    )
    result = worker.consume(signal, context=context)
    assert result["status"] == "PREPARED"
    saved = worker.decision(signal)
    assert saved["quantity"] == "1.250000000000000000"
    assert saved["snapshot"]["expires_at_ms"] == 8
    assert (
        saved["snapshot"]["features"]["evaluation"]["jev_entry_gate"]["status"]
        == "FALLBACK"
    )
    worker.entry_filter.assess = lambda *_a, **_k: pytest.fail("No re-review on retry")
    worker.consume(signal, context={})
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)


@pytest.mark.parametrize(
    "change",
    [
        {"fallback_cause": "MODEL_RESPONSE_INVALID"},
        {"fallback_cause": "UNKNOWN_FAILURE"},
        {"policy": {"enabled": True, "unavailable_action": "SKIP"}},
        {"policy": {"enabled": False, "unavailable_action": "BASELINE"}},
        {"execution_authorized": True},
        {"risk_override": True},
        {"valid_until_ms": 10},
        {"valid_until_ms": 3},
        {"valid_until_ms": True},
        {"status": "PASS"},
    ],
)
def test_invalid_fallback_proof_never_authorizes(database, change):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    worker.entry_filter = SimpleNamespace(
        assess=lambda original, **_: fallback(original, **change)
    )
    result = worker.consume(signal, context=context)
    assert (result["status"], result["reason"]) == ("IGNORED", "JEV_GATE_UNAVAILABLE")
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


def test_fallback_cannot_resize_original_or_override_hard_risk(database):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    context["directional"]["sizing"]["available_margin"] = "0"
    worker.entry_filter = SimpleNamespace(
        assess=lambda *_a, **_k: pytest.fail("Hard rejection must precede fallback")
    )
    assert worker.consume(signal, context=context)["status"] == "IGNORED"


def test_fallback_modified_quantity_is_rejected(database):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    worker.entry_filter = SimpleNamespace(
        assess=lambda original, **_: fallback(replace(original, quantity="99"))
    )
    assert worker.consume(signal, context=context)["reason"] == "JEV_GATE_UNAVAILABLE"


def fused(database, signal, context, *, expires=5):
    with database() as c:
        snapshot = c.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal,)
        ).fetchone()[0]
    peer = {
        "signal_id": "public-tv-reference",
        "source": "tv_bridge",
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "signal": "TREND_UP",
        "observed_at": 1,
        "expires_at_ms": expires,
    }
    context["signal_fusion"] = build_fusion(
        snapshot,
        source="s3",
        peers=[peer],
        policy=FusionPolicy(enabled=True),
        policy_version=1,
        now_ms=3,
    )
    return context


def test_used_tv_peer_clamps_order_lifetime_and_prevents_late_dispatch(database):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    result = worker.consume(signal, context=fused(database, signal, context))
    assert result["status"] == "PREPARED"
    saved = worker.decision(signal)
    assert saved["snapshot"]["expires_at_ms"] == 5
    assert (
        saved["snapshot"]["features"]["evaluation"]["signal_fusion"]["status"]
        == "CONFIRMS"
    )
    runtime.clock_ms = lambda: 5
    runtime.execution.submit = lambda *_: pytest.fail("Expired TV proof must not POST")
    assert runtime.execution.dispatch(result["order_id"]) == "DENIED"


def test_peer_expiring_during_evaluation_skips_without_model_request(database):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    original = worker.decide

    def delayed(*args):
        decision = original(*args)
        runtime.clock_ms = lambda: 5
        return decision

    worker.decide = delayed
    worker.entry_filter = SimpleNamespace(
        assess=lambda *_a, **_k: pytest.fail("Expired TV evidence must precede JEV")
    )
    result = worker.consume(signal, context=fused(database, signal, context))
    assert result["reason"] == "SIGNAL_FUSION_EVIDENCE_EXPIRED"


def test_directional_context_injects_fusion_before_screen_without_mutating_sources():
    from test_v2_directional import inputs
    from test_v2_directional_replay import SCOPE

    market = {
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "assembled_at": 2,
        "valid_until_ms": 9,
        "sources": {
            "s3": {"features": inputs()["market"]},
            "s0": {"features": {"regime": "neutral"}},
        },
    }
    market["sources"]["s3"]["features"]["15m"]["close"] = "100"
    original = deepcopy(market)
    seen = []
    context = DirectionalContext(
        lambda _: market,
        lambda _: pytest.fail("Screen rejects"),
        scope=SCOPE,
        clock_ms=lambda: 3,
        market_screen=lambda s, c: seen.append(c["signal_fusion"]) or True,
    )
    proof = {"version": "explicit-test-hook"}
    result = context.with_signal_fusion(
        {"symbol": "BTCUSDT", "observed_at": 1, "expires_at_ms": 10}, proof
    )
    assert seen == [proof] and result["market_only"] is True
    assert market == original and "signal_fusion" not in market
    result["signal_fusion"]["changed"] = True
    assert "changed" not in proof
