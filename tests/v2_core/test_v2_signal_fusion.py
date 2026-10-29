"""TV evidence changes only bounded entry score; absent TV never waits."""

import json
from copy import deepcopy
from dataclasses import asdict

import pytest
from test_v2_directional import history, inputs, sizing

from services.v2_directional_replay import market_plan, replay_decision
from v2_core.directional import evaluate_market, size_candidate
from v2_core.signal_fusion import (
    FusionPolicy,
    build_fusion,
    validate_fusion,
    validated_public_features,
)

NOW = 100000


def signal(*, kind="TREND_UP", strength=30):
    return {
        "symbol": "BTCUSDT",
        "signal": kind,
        "observed_at": NOW,
        "expires_at_ms": NOW + 90000,
        "features": {"type": kind, "strength": strength},
    }


def peer(**changes):
    return {
        "signal_id": "qa-tv-1",
        "source": "tv_bridge",
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "signal": "TREND_UP",
        "observed_at": NOW - 1,
        "expires_at_ms": NOW + 90000,
        **changes,
    }


def fusion(value=None, peers=None, *, source="s3", now=NOW, enabled=True):
    return build_fusion(
        signal() if value is None else value,
        source=source,
        peers=[peer()] if peers is None else peers,
        policy=FusionPolicy(enabled=enabled),
        policy_version=1,
        now_ms=now,
    )


def scene(value=None, proof=None, *, market_only=False, source="s3"):
    value = signal() if value is None else value
    strategy = "S6" if value["signal"] in {"TREND_UP", "PULSE_UP"} else "S8"
    data = inputs(strategy)
    data["event"]["strength"] = str(value["features"]["strength"])
    limits = sizing()
    for key in ("price", "atr_pct", "analysis_factor"):
        limits.pop(key)
    scope = {
        "exchange": "BINANCE",
        "account_id": "qa-fusion",
        "environment": "SANDBOX",
        "product": "FUTURES",
    }
    context = {
        "symbol": value["symbol"],
        "environment": "SANDBOX",
        "account_scope": scope,
        "assembled_at": NOW,
        "valid_until_ms": NOW + 90000,
        "directional": {
            "market": data["market"],
            "price": data["price"],
            "regime": data["regime"],
            "short_ratio": None if market_only else "0.5",
            "history": history(),
            "sizing": limits,
            "expected_move_pct": "8",
            "funding_rate": "0",
        },
        **({"signal_fusion": proof} if proof is not None else {}),
        **({"market_only": True} if market_only else {}),
    }
    config = {
        "strategy": strategy,
        "source": source,
        "mode": "REPLAY_ONLY",
        "analysis_mode": "hard",
        "account_scope": scope,
    }
    return value, context, config


@pytest.mark.parametrize(
    "peers",
    [
        [],
        [peer(expires_at_ms=NOW)],
        [peer(observed_at=NOW + 1)],
        [peer(observed_at=NOW - 60000)],
        [peer(environment="LIVE")],
        [peer(symbol="ETHUSDT")],
        [peer(source="s3")],
        [peer(signal="EVENT_END")],
        [peer(observed_at=True)],
    ],
)
def test_absent_invalid_expired_or_wrong_scope_is_neutral(peers):
    proof = fusion(peers=peers)
    assert (
        proof["status"],
        proof["score_bonus"],
        proof["veto"],
        proof["valid_until_ms"],
    ) == ("ABSENT", 0, False, None)
    assert validated_public_features(proof, NOW) == {
        "tv_confirmation": None,
        "tv_observed_at_ms": None,
    }
    value, context, config = scene(proof=proof)
    baseline, _ = market_plan(
        value, {k: v for k, v in context.items() if k != "signal_fusion"}, config
    )
    assert market_plan(value, context, config)[0] == baseline


def test_default_policy_off_does_not_change_baseline():
    assert FusionPolicy().enabled is False
    proof = fusion(enabled=False)
    assert proof["status"] == "OFF" and proof["score_bonus"] == 0
    assert validated_public_features(proof, NOW)["tv_confirmation"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmation_bonus": 6},
        {"confirmation_bonus": -1},
        {"confirmation_bonus": True},
        {"window_ms": 999},
        {"window_ms": 300001},
        {"enabled": 1},
        {"conflict_veto": "false"},
    ],
)
def test_policy_limits(changes):
    with pytest.raises(ValueError, match="POLICY"):
        FusionPolicy(**changes)


def test_duplicates_cannot_stack_confirmation_and_latest_direction_wins():
    confirmed = fusion(peers=[peer(signal_id=f"qa-{i}") for i in range(50)])
    assert confirmed["score_bonus"] == 5 and len(confirmed["peers"]) == 1
    mixed = fusion(
        peers=[peer(), peer(signal_id="new", observed_at=NOW, signal="TREND_DOWN")]
    )
    assert mixed["status"] == "CONFLICTS" and mixed["veto"]
    assert validated_public_features(mixed, NOW) == {
        "tv_confirmation": -1,
        "tv_observed_at_ms": NOW,
    }


def test_cross_source_orientation_tv_uses_s3_and_no_tv_means_no_confirmation():
    value = signal()
    proof = fusion(value, [peer(source="s3")], source="tv_bridge")
    assert proof["status"] == "CONFIRMS"
    assert validated_public_features(proof, NOW)["tv_observed_at_ms"] == NOW
    assert fusion(value, [], source="tv_bridge")["status"] == "ABSENT"


@pytest.mark.parametrize("reverse", [False, True])
def test_simultaneous_contradiction_is_conflict_not_uuid_lottery(reverse):
    peers = [
        peer(signal_id="zz-confirm"),
        peer(signal_id="aa-conflict", signal="TREND_DOWN"),
    ]
    proof = fusion(peers=list(reversed(peers)) if reverse else peers)
    assert proof["status"] == "CONFLICTS" and proof["veto"]
    assert proof["score_bonus"] == 0
    assert proof["peers"][0]["signal_id"] == "aa-conflict"


def test_expiry_revalidation_never_restamps_original_peer():
    proof = fusion(peers=[peer(expires_at_ms=NOW + 10)])
    assert proof["valid_until_ms"] == NOW + 10
    assert (
        validate_fusion(signal(), source="s3", proof=proof, now_ms=NOW + 9)[
            "score_bonus"
        ]
        == 5
    )
    assert (
        validate_fusion(signal(), source="s3", proof=proof, now_ms=NOW + 10)["status"]
        == "ABSENT"
    )
    assert proof["peers"][0]["observed_at"] == NOW - 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("score_bonus", 50),
        ("policy_digest", "wrong"),
        ("environment", "LIVE"),
        ("source", "tv_bridge"),
        ("symbol", "ETHUSDT"),
        ("direction", "SHORT"),
        ("valid_until_ms", NOW + 900000),
    ],
)
def test_frozen_proof_tampering_is_rejected(key, value):
    proof = fusion()
    proof[key] = value
    with pytest.raises(ValueError, match="PROOF_INVALID"):
        validate_fusion(signal(), source="s3", proof=proof, now_ms=NOW)


def test_same_scope_replay_is_deterministic_without_current_policy_read():
    proof = fusion()
    original = deepcopy(proof)
    assert validate_fusion(signal(), source="s3", proof=proof, now_ms=NOW) == proof
    assert validate_fusion(signal(), source="s3", proof=proof, now_ms=NOW) == original
    assert proof == original
    assert (
        validate_fusion(
            signal(), source="s3", proof={"version": "shared-context-v1"}, now_ms=NOW
        )
        is None
    )


def test_confirmation_can_cross_soft_threshold_with_full_risk_and_audit():
    value, context, config = scene(proof=fusion())
    plan, evidence = market_plan(value, context, config)
    assert evidence["signal_fusion"]["baseline_plan"]["reason"] == "score_below_minimum"
    assert evidence["signal_fusion"]["baseline_plan"]["score"] == 25
    assert plan.reason == "CANDIDATE" and plan.score == 30
    result = replay_decision(value, context, config)
    assert result.rationale == "DIRECTIONAL_REPLAY_ONLY"
    proof = json.loads(result.features_json)
    assert proof["signal_fusion"]["allocation_score"] == 25
    assert proof["signal_fusion"]["fused_plan"]["score"] == 30
    assert proof["execution_authorized"] is False
    context["directional"]["expected_move_pct"] = "0.01"
    assert replay_decision(value, context, config).rationale == "REWARD_RISK_TOO_LOW"
    context["directional"]["expected_move_pct"] = "8"
    context["directional"]["sizing"]["available_margin"] = "0"
    assert replay_decision(value, context, config).rationale == "INSUFFICIENT_BUDGET"


def test_promoted_market_only_candidate_must_collect_full_account_context():
    value, context, config = scene(proof=fusion(), market_only=True)
    assert market_plan(value, context, config)[0].reason == "CANDIDATE"
    with pytest.raises(ValueError, match="FULL_ACCOUNT_CONTEXT_REQUIRED"):
        replay_decision(value, context, config)


@pytest.mark.parametrize(
    "mode",
    [
        "overextended",
        "entry_mode_unconfirmed",
        "atr_exceeded",
        "zero_volatility",
        "strength_below_minimum",
    ],
)
def test_confirmation_never_bypasses_non_score_market_rejections(mode):
    value = (
        signal(kind="TREND_DOWN", strength=55)
        if mode == "strength_below_minimum"
        else signal(strength=70)
    )
    proof = fusion(value, [peer(signal=value["signal"])])
    value, context, config = scene(value, proof)
    market = context["directional"]["market"]
    if mode == "overextended":
        context["directional"]["price"] = "103"
    elif mode == "entry_mode_unconfirmed":
        context["directional"]["price"] = "99"
    elif mode == "atr_exceeded":
        market["1h"]["atr_pct"] = "100"
    elif mode == "zero_volatility":
        market["1h"]["atr"] = "0"
    assert market_plan(value, context, config)[0].reason == mode


def test_conflict_veto_is_local_and_absence_baseline_can_still_trade():
    value = signal(strength=70)
    value, context, config = scene(value, fusion(value, [peer(signal="TREND_DOWN")]))
    assert replay_decision(value, context, config).rationale == "TV_DIRECTION_CONFLICT"
    context["signal_fusion"] = fusion(value, [])
    assert (
        replay_decision(value, context, config).rationale == "DIRECTIONAL_REPLAY_ONLY"
    )


def test_confirmation_does_not_raise_leverage_or_allocation():
    data = inputs()
    data["event"]["strength"] = "60"
    baseline = evaluate_market(**data)
    boosted = evaluate_market(**data, score_bonus=5)
    assert baseline.score == 55 and boosted.score == 60
    assert baseline.leverage == boosted.leverage == 2
    assert size_candidate(
        boosted, allocation_score=baseline.score, **sizing()
    ) == size_candidate(baseline, **sizing())
    with pytest.raises(ValueError, match="allocation"):
        size_candidate(boosted, allocation_score=boosted.score + 1, **sizing())


def test_versioned_policy_is_explicit_and_default_off(monkeypatch):
    from types import SimpleNamespace

    from services import v2_signal_fusion_policy as store

    records = [
        None,
        SimpleNamespace(
            deleted=False,
            version=3,
            payload_json=json.dumps(asdict(FusionPolicy(enabled=True))),
        ),
    ]

    class State:
        def __init__(self, _connect):
            pass

        def read(self, key):
            assert (
                key.environment == "SANDBOX"
                and key.namespace == "signal-fusion-policy-v1"
            )
            return records.pop(0)

    monkeypatch.setattr(store, "BusinessState", State)
    assert store.read_policy(None) == (FusionPolicy(), 0)
    assert store.read_policy(None) == (FusionPolicy(enabled=True), 3)


@pytest.mark.parametrize("enabled", [False, True])
def test_dynamic_context_attaches_evidence_before_delegate_screen(monkeypatch, enabled):
    from contextlib import contextmanager

    from services import v2_shared_market_context as shared
    from services import v2_signal_fusion_policy as store
    from v2_core.shared_market import MarketPolicy

    monkeypatch.setattr(MarketPolicy, "read", lambda *_: MarketPolicy())
    monkeypatch.setattr(
        shared.Catalog,
        "snapshot",
        lambda *_: {
            "contracts": {"BTCUSDT": {"supported": True}},
            "observed_at_ms": NOW,
            "digest": "qa",
        },
    )
    monkeypatch.setattr(
        store, "read_policy", lambda *_: (FusionPolicy(enabled=enabled), 1)
    )

    class Cursor:
        def __init__(self, sql):
            self.sql = sql

        def fetchall(self):
            snapshot = {
                "symbol": "BTCUSDT",
                "signal": "TREND_UP",
                "observed_at": NOW - 1,
                "expires_at_ms": NOW + 90000,
            }
            if "snapshot,source" in self.sql:
                return [("peer", snapshot, "tv_bridge")]
            return [("peer", snapshot)]

    class Conn:
        def execute(self, sql, _args):
            return Cursor(sql)

    @contextmanager
    def connect():
        yield Conn()

    seen = []

    class Early:
        def __call__(self, _signal):
            pytest.fail("fusion must be injected before screening")

        def with_signal_fusion(self, value, proof):
            seen.append(proof)
            _, context, config = scene(value, proof)
            plan, _ = market_plan(value, context, config)
            assert plan.reason == ("CANDIDATE" if enabled else "score_below_minimum")
            return context

    provider = shared.DynamicMarketContext(
        connect, delegate=Early(), clock_ms=lambda: NOW, source="s3"
    )
    result = provider(signal())
    assert len(seen) == 1
    assert result["signal_fusion"] == seen[0]
    assert result["universe_admission"]["eligible"] is True
    if enabled:
        provider.delegate = lambda _signal: {}
        with pytest.raises(ValueError, match="EARLY_CONTEXT_REQUIRED"):
            provider(signal())


def test_directional_context_promoted_score_collects_fresh_account(monkeypatch):
    from services.v2_directional_context import DirectionalContext
    from services.v2_directional_replay import MARKET_ONLY_REJECTIONS
    from v2_core.account_risk import AccountScope

    value, context, config = scene(proof=fusion())
    scope = AccountScope(**context["account_scope"])
    windows = deepcopy(context["directional"]["market"])
    windows["15m"]["close"] = context["directional"]["price"]
    market = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "assembled_at": NOW,
        "valid_until_ms": NOW + 90000,
        "sources": {
            "s3": {"features": windows},
            "s0": {"features": {"regime": "neutral"}},
        },
    }
    calls = []

    def account(_signal):
        calls.append(True)
        return {
            "symbol": "BTCUSDT",
            "account_scope": asdict(scope),
            "evidence": {"real_read_fixture": True},
            "observed_at_ms": NOW,
            "valid_until_ms": NOW + 10000,
            **context["directional"],
        }

    provider = DirectionalContext(
        lambda _signal: market,
        account,
        scope=scope,
        clock_ms=lambda: NOW,
        market_screen=lambda sig, ctx: (
            market_plan(sig, ctx, config)[0].reason in MARKET_ONLY_REJECTIONS
        ),
    )
    promoted = provider.with_signal_fusion(value, fusion())
    assert calls == [True]
    assert "account_evidence" in promoted and not promoted.get("market_only", False)
    assert (
        replay_decision(value, promoted, config).rationale == "DIRECTIONAL_REPLAY_ONLY"
    )
