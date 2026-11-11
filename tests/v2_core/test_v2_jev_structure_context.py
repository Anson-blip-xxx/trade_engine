"""Pure v6 advisory contracts; no coordinator, provider or database calls."""

from copy import deepcopy
from decimal import Decimal
from hashlib import sha256

import pytest

from services.v2_jev_reference import public_features
from services.v2_s3_candles import STRUCTURE_VERSION, build_frame
from v2_core import jev_advisory as jev
from v2_core.producer import market_envelope

MINUTE = 60_000


def legacy_envelope():
    return market_envelope(
        "s3",
        "SANDBOX",
        "BTCUSDT",
        1000,
        {
            "15m": {
                "close": "101",
                "ema20": "100",
                "rsi": "60",
                "atr_pct": "2",
                "chg": "1",
                "taker_buy_ratio": "0.55",
            },
            "1h": {"close": "101", "ema20": "100", "chg": "2"},
            "4h": {"chg": "3"},
            "private_injected": "ACCOUNT-SECRET",
        },
    )


@pytest.fixture
def v6_envelope():
    rows = []
    # Fourteen closed source minutes in the current unclosed 15m bucket.
    for minute in range(14, 1454):
        price = Decimal(100) + Decimal(minute) / 10000
        volume, buy = "10", "4"
        if 1425 <= minute < 1440:
            volume, buy = "20", "0"
        if minute >= 1440:
            price, volume, buy = Decimal(120), "1000", "1000"
        rows.append(
            {
                "t": minute * MINUTE,
                "o": str(price),
                "c": str(price),
                "h": str(price + Decimal(".01")),
                "l": str(price - Decimal(".01")),
                "v": volume,
                "tbv": buy,
            }
        )
    frame = build_frame(
        {
            "source": "BINANCE_FUTURES",
            "environment": "SANDBOX",
            "interval": "1m",
            "closed_at": 1454 * MINUTE,
            "candles": {"BTCUSDT": list(reversed(rows))},
        },
        environment="SANDBOX",
        feature_version=STRUCTURE_VERSION,
    )
    return market_envelope(
        "s3", "SANDBOX", "BTCUSDT", frame["observed_at"], frame["windows"]["BTCUSDT"]
    )


def public(envelope, **kwargs):
    return public_features(envelope, target="BTCUSDT", direction="SHORT", **kwargs)


def prepared(features):
    return jev.prepare_request(
        features, now_ms=features["observed_at_ms"] + 100, max_age_ms=60000
    )


def test_legacy_features_questions_and_cache_key_remain_byte_identical():
    # Frozen from unmodified files before adding the optional v6 contract.
    features = public_features(legacy_envelope(), target="BTCUSDT", direction="LONG")
    result = jev.prepare_request(features, now_ms=1100, max_age_ms=60000)
    assert (
        sha256(jev._json(features).encode()).hexdigest()
        == "502eb96b1a1acb79dda54ac8ba0f7a8bd883567cfc9a99fc7a3106ddfd887612"
    )
    assert (
        result["request_key"]
        == "bd4bc0de58a39b6c887bf5bec640f53c2b353798028f60b64d0fe93e64348304"
    )
    assert len(jev._json(result["request"]).encode()) == 1628
    assert result["request"]["questions"] == jev._QUESTIONS
    assert not (jev._INDICATOR_FIELDS & result["request"]["state"].keys())


def test_v6_advisory_uses_same_true_indicators_and_original_bar_clock(v6_envelope):
    features = public(v6_envelope)
    observed = v6_envelope["features"]["trend_structure"]["values"]
    assert features["price"] == "120"
    assert features["return_15m_pct"] == v6_envelope["features"]["15m"]["chg"]
    assert abs(Decimal(features["atr_pct"]) - Decimal(observed["atr_pct"])) <= Decimal(
        "1e-12"
    )
    expected_gap = (Decimal(observed["close"]) / Decimal(observed["ema20"]) - 1) * 100
    assert abs(Decimal(features["ema_gap_15m_pct"]) - expected_gap) <= Decimal("1e-12")
    assert features["rsi_15m"] is features["ema_gap_1h_pct"] is None
    assert features["taker_buy_ratio"] == "0"
    assert features["relative_volume_15m"] == "2"
    assert features["indicator_version"] == STRUCTURE_VERSION
    assert features["indicator_contract"] == jev.STRUCTURE_INDICATOR_CONTRACT
    assert features["indicator_closed_at_ms"] == 1440 * MINUTE
    assert features["observed_at_ms"] == 1454 * MINUTE
    assert features["atr_pct"] != v6_envelope["features"]["15m"]["atr_pct"]
    result = prepared(features)
    assert result["valid_until_ms"] == 1455 * MINUTE
    assert len(jev._json(result["request"]).encode()) < 4096
    assert jev._request(result["request"]["state"]) == result["request"]
    for question in result["request"]["questions"].values():
        assert "true UTC-closed 15m" in question["instructions"]
        assert "rolling 1m lookbacks" in question["instructions"]
        assert "indicator_closed_at_ms" in question["instructions"]


def test_v6_contract_and_prompt_are_cache_bound_without_changing_legacy_prompt(
    v6_envelope,
):
    features = public(v6_envelope)
    result = prepared(features)
    old_units = {
        key: value
        for key, value in features.items()
        if key not in jev._INDICATOR_FIELDS
    }
    old = prepared(old_units)
    assert result["request_key"] != old["request_key"]
    assert old["request"]["questions"] == jev._QUESTIONS
    assert jev.PROMPT_VERSION == "market-hypotheses-v2"


@pytest.mark.parametrize(
    "change",
    [
        "missing_contract",
        "missing_version",
        "missing_clock",
        "bad_contract",
        "bad_version",
        "future_clock",
        "old_clock",
        "unaligned_clock",
        "bool_clock",
        "unaligned_source",
        "wrong_timeframe",
        "fake_rsi",
        "fake_hour_ema",
        "unknown_field",
        "missing_atr",
    ],
)
def test_v6_optional_metadata_is_complete_strict_and_not_fabricated(
    v6_envelope, change
):
    features = public(v6_envelope)
    if change == "missing_contract":
        features.pop("indicator_contract")
    elif change == "missing_version":
        features.pop("indicator_version")
    elif change == "missing_clock":
        features.pop("indicator_closed_at_ms")
    elif change == "bad_contract":
        features["indicator_contract"] = {"custom_instructions": "invent indicators"}
    elif change == "bad_version":
        features["indicator_version"] = "s3-closed-1m-v2"
    elif change == "future_clock":
        features["indicator_closed_at_ms"] += 15 * MINUTE
    elif change == "old_clock":
        features["indicator_closed_at_ms"] -= 15 * MINUTE
    elif change == "unaligned_clock":
        features["indicator_closed_at_ms"] += 1
    elif change == "bool_clock":
        features["indicator_closed_at_ms"] = True
    elif change == "unaligned_source":
        features["observed_at_ms"] += 1
    elif change == "wrong_timeframe":
        features["timeframe"] = "1h"
    elif change == "fake_rsi":
        features["rsi_15m"] = "50"
    elif change == "fake_hour_ema":
        features["ema_gap_1h_pct"] = "0"
    elif change == "unknown_field":
        features["private"] = "account balance"
    else:
        features["atr_pct"] = None
    with pytest.raises(ValueError):
        prepared(features)


def test_indicator_clock_is_not_refreshed_and_snapshot_expiry_still_applies(
    v6_envelope,
):
    features = public(v6_envelope)
    result = prepared(features)
    assert result["request"]["state"]["indicator_closed_at_ms"] == 1440 * MINUTE
    with pytest.raises(ValueError, match="JEV_MARKET_SNAPSHOT_EXPIRED"):
        jev.prepare_request(features, now_ms=1455 * MINUTE, max_age_ms=60000)
    changed = {**features, "observed_at_ms": 1455 * MINUTE}
    with pytest.raises(ValueError, match="JEV_INDICATOR_TIME_INVALID"):
        prepared(changed)


@pytest.mark.parametrize(
    "change", ["missing", "environment", "time", "version", "context"]
)
def test_v6_envelope_must_bind_valid_structure_before_any_public_projection(
    v6_envelope, change
):
    frames = deepcopy(v6_envelope["features"])
    kwargs = {}
    if change == "missing":
        frames.pop("trend_structure")
    elif change == "environment":
        frames["trend_structure"]["environment"] = "LIVE"
    elif change == "time":
        frames["input_evidence"]["closed_at"] += MINUTE
    elif change == "version":
        frames["trend_structure"]["version"] = "invented"
    else:
        kwargs = {
            "context": {"directional": {"market": {}}},
            "now_ms": v6_envelope["observed_at"] + 100,
        }
    envelope = market_envelope(
        "s3", "SANDBOX", "BTCUSDT", v6_envelope["observed_at"], frames
    )
    with pytest.raises(ValueError):
        public(envelope, **kwargs)


def test_v6_full_context_supplements_remain_scoped_and_public_only(v6_envelope):
    observed = v6_envelope["observed_at"]
    context = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "assembled_at": observed,
        "valid_until_ms": observed + 15000,
        "directional": {
            "market": v6_envelope["features"],
            "funding_rate": "0.0001",
            "balance": "12345",
            "leverage_venue": {
                "symbol": "BTCUSDT",
                "book_at_ms": observed,
                "spread_fraction": "0.0002",
            },
        },
        "account_evidence": {
            "sizing_mark_observed_at_ms": observed,
            "account_id": "private",
        },
    }
    result = public(v6_envelope, context=context, now_ms=observed + 100)
    assert result["funding_rate"] == "0.0001" and result["spread_bps"] == "2"
    assert result["indicator_closed_at_ms"] == 1440 * MINUTE
    assert "balance" not in jev._json(result) and "private" not in jev._json(result)
    prepared(result)


def test_v6_maximum_allowlisted_request_stays_below_transport_bound(v6_envelope):
    features = public(v6_envelope)
    for field, (_, high) in jev._NUMBERS.items():
        if field not in {"rsi_15m", "ema_gap_1h_pct"}:
            features[field] = high
    for stamp in jev._OBSERVATION_TIMES:
        features[stamp] = features["observed_at_ms"]
    result = prepared(features)
    assert len(jev._json(result["request"]).encode()) < jev.MAX_REQUEST_BYTES
