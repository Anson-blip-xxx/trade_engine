"""Database policy and cross-source context wiring, no external requests."""

from copy import deepcopy
from dataclasses import asdict

import pytest
from test_v2_shared_market import (  # noqa: F401
    Catalog,
    catalog_response,
    database,
    scenario,
    shared,
)

from services.v2_shared_market_context import DynamicMarketContext
from services.v2_signal_fusion_policy import policy_key
from v2_core.signal_fusion import FusionPolicy
from v2_core.state import BusinessState


def enable(connect):
    return BusinessState(connect).change(
        policy_key(),
        expected_version=0,
        request_key="qa-enable-fusion",
        payload=asdict(FusionPolicy(enabled=True)),
        reason="QA_ONLY",
    )


def test_pg_fusion_uses_early_hook_and_keeps_no_tv_baseline(shared):  # noqa: F811
    Catalog(shared, "SANDBOX", lambda: 3).save(catalog_response(["BTCUSDT"]))
    _, runtime, internal, context = scenario(shared)
    assert enable(shared).code == "APPLIED"
    seen = []

    class Delegate:
        def __call__(self, signal):
            pytest.fail("enabled fusion must precede market prefilter")

        def with_signal_fusion(self, signal, proof):
            seen.append(proof)
            return {**deepcopy(context), "signal_fusion": proof}

    signal = {
        "symbol": "BTCUSDT",
        "signal": "TREND_UP",
        "observed_at": 2,
        "expires_at_ms": 10,
        "features": {},
    }
    provider = DynamicMarketContext(
        shared, delegate=Delegate(), clock_ms=lambda: 3, source="s3"
    )
    absent = provider(signal)["signal_fusion"]
    assert absent["status"] == "ABSENT"
    assert absent["score_bonus"] == 0 and not absent["veto"]
    tv = runtime.data.signals.admit(
        source="tv_bridge",
        environment="SANDBOX",
        request_key="tv-same-direction",
        snapshot=signal,
    )
    confirmed = provider(signal)["signal_fusion"]
    assert confirmed["status"] == "CONFIRMS" and confirmed["score_bonus"] == 5
    assert confirmed["peers"][0]["signal_id"] == tv
    assert confirmed["valid_until_ms"] == 10
    assert len(seen) == 2
    with shared() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
    # TV's own direct candidate also receives the actual persisted internal peer.
    reverse = DynamicMarketContext(
        shared, delegate=Delegate(), clock_ms=lambda: 3, source="tv_bridge"
    )
    assert reverse(signal)["signal_fusion"]["peers"][0]["signal_id"] == internal


def test_enabled_fusion_cannot_silently_use_legacy_delegate(shared):  # noqa: F811
    Catalog(shared, "SANDBOX", lambda: 3).save(catalog_response(["BTCUSDT"]))
    assert enable(shared).code == "APPLIED"
    provider = DynamicMarketContext(
        shared, delegate=lambda _: {}, clock_ms=lambda: 3, source="s3"
    )
    with pytest.raises(ValueError, match="SIGNAL_FUSION_EARLY_CONTEXT_REQUIRED"):
        provider(
            {
                "symbol": "BTCUSDT",
                "signal": "TREND_UP",
                "observed_at": 2,
                "expires_at_ms": 10,
            }
        )
