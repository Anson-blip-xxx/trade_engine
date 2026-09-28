from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_v2_directional import inputs
from test_v2_directional_replay import SCOPE
from test_v2_s3_candles import batch

from services.v2_directional_replay import MARKET_ONLY_REJECTIONS, market_plan
from services.v2_s3_candles import PRICE_VERSION, VERSION, build_frame
from services.v2_s3_source import frame_digest
from services.v2_shared_risk_reference import SharedRiskReference
from v2_core.directional import evaluate_market
from v2_core.ledger import amount


@pytest.mark.parametrize("price", ["0.000751", "0.000000000001", "101"])
def test_price_precision_is_scale_independent_and_legacy_replay_is_unchanged(price):
    source = batch()
    px = Decimal(price)
    for bar in source["candles"]["BTCUSDT"]:
        bar.update(o=price, c=price, h=str(px * Decimal("1.0001")), l=price)
    before = deepcopy(source)
    current = build_frame(source, environment="SANDBOX", feature_version=PRICE_VERSION)
    assert source == before
    assert current["frame_id"].startswith(PRICE_VERSION)
    for name in ("15m", "1h", "4h", "24h"):
        features = current["windows"]["BTCUSDT"][name]
        assert amount(features["atr"], positive=True) == px * Decimal("0.0001")
        assert amount(features["ema20"], positive=True) == px
        assert amount(features["close"], positive=True) == px
        assert features["atr_pct"] == "0.01"
    assert (
        current["windows"]["BTCUSDT"]["input_evidence"]["window_version"]
        == PRICE_VERSION
    )
    if price == "0.000751":
        legacy = build_frame(source, environment="SANDBOX")
        assert legacy["windows"]["BTCUSDT"]["1h"]["atr"] == "0.0"
        assert legacy["frame_id"].startswith(VERSION)
        assert legacy == build_frame(
            source, environment="SANDBOX", feature_version=VERSION
        )


def test_precision_never_floors_unrepresentable_volatility_to_a_tradeable_value():
    source = batch()
    for bar in source["candles"]["BTCUSDT"]:
        bar.update(o="1e-18", c="1e-18", l="1e-18", h="1.01e-18")
    with pytest.raises(ValueError, match="MARKET_INDICATOR_BELOW_PRECISION"):
        build_frame(source, environment="SANDBOX", feature_version=PRICE_VERSION)


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize("window", ["15m", "1h"])
def test_zero_volatility_is_a_terminal_market_rejection_not_an_outage(strategy, window):
    data = inputs(strategy)
    data["market"][window]["atr"] = "0"
    assert evaluate_market(**data).reason == "zero_volatility"
    assert "zero_volatility" in MARKET_ONLY_REJECTIONS
    data["market"][window]["atr"] = "-0.000001"
    with pytest.raises(ValueError):
        evaluate_market(**data)


@pytest.mark.parametrize(
    "version,prefix", [(VERSION, "s3-shared-v1"), (PRICE_VERSION, "s3-shared-v2")]
)
def test_shared_risk_reference_verifies_exact_version_and_never_falls_back(
    version, prefix
):
    source = batch()
    frame = build_frame(source, environment="SANDBOX", feature_version=version)
    observed = source["closed_at"]
    head = [observed, f"{prefix}:BTCUSDT:{observed}", frame_digest(frame)]
    provider = SharedRiskReference(
        lambda: pytest.fail("only injected readonly head needed"),
        scope=SCOPE,
        archive=SimpleNamespace(read=lambda *_: source["candles"]["BTCUSDT"]),
        clock_ms=lambda: observed + 1,
    )
    provider._head = lambda _: tuple(head)
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}
    assert provider(request).price == "101"
    head[2] = "0" * 64
    with pytest.raises(ValueError, match="REFERENCE_INPUT_MISMATCH"):
        provider(request)
    head[1] = f"s3-shared-v99:BTCUSDT:{observed}"
    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        provider(request)


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
@pytest.mark.parametrize("strength", [0, 10, 20, 29, 30, 59, 60, 70])
@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"tv.trend_policy_enabled": True},
        {
            "entry.min_score": 80,
            "entry.short_min_strength": "0",
            "score.crowding_bonus": "100",
            "score.crowding_penalty": "100",
        },
        {"score.crowding_bonus": "0", "score.crowding_penalty": "0"},
    ],
)
def test_score_upper_bound_never_discards_a_candidate_real_sentiment_can_rescue(
    strategy, source, strength, policy
):
    facts = inputs(strategy)
    signal = {
        "signal": facts["event"]["type"],
        "observed_at": 1,
        "features": {**facts["event"], "strength": strength},
    }
    context = {
        "assembled_at": 1,
        "market_only": True,
        "directional": {
            "market": facts["market"],
            "price": facts["price"],
            "regime": facts["regime"],
            "short_ratio": None,
        },
    }
    config = {"strategy": strategy, "source": source, "policy": policy}
    plan, proof = market_plan(signal, context, config)
    assert context["directional"]["short_ratio"] is None
    for ratio in ("0", "0.1", "0.4", "0.5", "0.6", "0.9", "1"):
        actual = deepcopy(context)
        actual.pop("market_only")
        actual["directional"]["short_ratio"] = ratio
        real_plan, _ = market_plan(signal, actual, config)
        if real_plan.reason == "CANDIDATE":
            assert plan.reason not in MARKET_ONLY_REJECTIONS
        if plan.reason == "score_unreachable":
            assert real_plan.reason == "score_below_minimum"
            assert proof["score_bound"] == "MAXIMUM_CROWDING_BONUS_BELOW_THRESHOLD"
