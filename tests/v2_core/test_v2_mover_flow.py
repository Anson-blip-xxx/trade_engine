import json
from copy import deepcopy
from decimal import Decimal, Inexact, localcontext

import pytest

from v2_core.mover_flow import DAY, HOUR, analyze_flow_evidence

START = 21_000 * DAY - 8 * HOUR
CUTOFF = START + 6 * HOUR


def bundle():
    spot = []
    for hour in range(24):
        opening = 100 if hour < 3 else 100 + hour - 3
        closing = opening if hour < 3 else opening + 1
        quote, buy = ("100", "60") if hour < 3 else ("200", "150")
        spot.append(
            [
                START + hour * HOUR,
                str(opening),
                str(closing),
                str(opening),
                str(closing),
                "2",
                START + (hour + 1) * HOUR - 1,
                quote,
                10,
                "1",
                buy,
                "0",
            ]
        )
    envelope = {
        "status": "OBSERVED",
        "reason": None,
        "fetched_at_ms": START + DAY + HOUR,
    }
    return {
        "version": "mover-flow-public-v1",
        "source": "BINANCE_LIVE_PUBLIC",
        "symbol": "TESTUSDT",
        "start_ms": START,
        "end_ms": START + DAY,
        "mapping": {
            "status": "MATCHED",
            "basis": "CURRENT_EXACT_SYMBOL_BASE_QUOTE",
            "spot_symbol": "TESTUSDT",
            "base_asset": "TEST",
            "quote_asset": "USDT",
        },
        "spot": {**envelope, "source": "BINANCE_SPOT_LIVE", "rows": spot},
        "open_interest": {
            **envelope,
            "source": "BINANCE_USDM_LIVE",
            "rows": [
                {
                    "symbol": "TESTUSDT",
                    "timestamp": START + hour * HOUR,
                    "sumOpenInterest": str(100 + hour * 10),
                    "sumOpenInterestValue": str(10000 + hour * 1000),
                }
                for hour in range(25)
            ],
        },
        "funding": {
            **envelope,
            "source": "BINANCE_USDM_LIVE",
            "rows": [
                {
                    "symbol": "TESTUSDT",
                    "fundingTime": START + hour * HOUR,
                    "fundingRate": "0.0001",
                    "markPrice": "100",
                }
                for hour in (-8, 0, 8, 16, 24)
            ],
        },
    }


def analyze(data=None, **kwargs):
    return analyze_flow_evidence(
        bundle() if data is None else data,
        **{"cutoff_ms": CUTOFF, "side": "BUY", **kwargs},
    )


def component(data, name):
    return analyze(data)["components"][name]


def test_observed_spot_reports_signed_quote_notional_and_relative_volume():
    report = analyze()
    assert report["status"] == "OBSERVED"
    spot = report["components"]["spot"]["features"]
    assert spot["notional_unit"] == "USDT"
    assert spot["baseline"]["buy_quote"] == "180"
    assert spot["baseline"]["sell_quote"] == "120"
    assert spot["recent"]["buy_quote"] == "450"
    assert spot["recent"]["sell_quote"] == "150"
    assert spot["recent"]["aggressor_delta_quote"] == "300"
    assert spot["recent"]["imbalance_ratio"] == "0.5"
    assert spot["recent"]["price_return_pct"] == "3"
    assert spot["quote_volume_ratio"] == "2"


def test_side_flips_directional_flow_and_response_but_not_observations():
    buy, sell = analyze(), analyze(side="SELL")
    a, b = (r["components"]["spot"]["features"]["recent"] for r in (buy, sell))
    assert a["aggressor_delta_quote"] == b["aggressor_delta_quote"] == "300"
    assert b["side_adjusted_delta_quote"] == "-300"
    assert b["side_adjusted_imbalance_ratio"] == "-0.5"
    assert b["side_adjusted_price_return_pct"] == "-3"
    assert buy["components"]["open_interest"] == sell["components"]["open_interest"]
    assert buy["components"]["funding"] == sell["components"]["funding"]
    assert buy["input_digest"] != sell["input_digest"]


def test_sell_pressure_is_positive_when_evaluated_for_sell():
    data = bundle()
    for offset, row in enumerate(data["spot"]["rows"][3:6]):
        row[1:5] = [
            str(100 - offset),
            str(100 - offset),
            str(99 - offset),
            str(99 - offset),
        ]
        row[10] = "50"
    features = analyze(data, side="SELL")["components"]["spot"]["features"]["recent"]
    assert features["aggressor_delta_quote"] == "-300"
    assert features["side_adjusted_delta_quote"] == "300"
    assert features["side_adjusted_price_return_pct"] == "3"


def test_all_timestamped_future_rows_and_unused_old_values_are_ignored():
    data = bundle()
    expected = analyze(data)
    data["spot"]["rows"] = data["spot"]["rows"][:6] + [
        [CUTOFF, "not a number"],
        [CUTOFF, "duplicate future"],
        [START - HOUR, "unused old value"],
    ]
    data["open_interest"]["rows"] = data["open_interest"]["rows"][:7] + [
        {"timestamp": CUTOFF + HOUR, "symbol": "WRONG", "sumOpenInterest": "NaN"},
        {"timestamp": CUTOFF + HOUR},
        {"timestamp": START - HOUR},
    ]
    data["funding"]["rows"] = [
        data["funding"]["rows"][1],
        {"fundingTime": CUTOFF, "symbol": "WRONG", "fundingRate": "NaN"},
        {"fundingTime": CUTOFF + HOUR},
        {"fundingTime": CUTOFF + HOUR},
        {"fundingTime": CUTOFF - 13 * HOUR, "fundingRate": "NaN"},
    ]
    assert analyze(data) == expected


def test_oi_uses_base_units_and_ignores_quote_valuation_changes():
    data = bundle()
    expected = analyze(data)
    for row in data["open_interest"]["rows"]:
        row["sumOpenInterestValue"] = "price multiplied this by a million"
    assert analyze(data) == expected
    features = expected["components"]["open_interest"]["features"]
    assert features["unit"] == "BASE_ASSET"
    assert features["start_base"] == "130"
    assert features["end_base"] == "160"
    assert features["change_base"] == "30"
    assert len(features["hourly_points"]) == 4


def test_oi_decline_remains_negative_for_either_side():
    data = bundle()
    data["open_interest"]["rows"][6]["sumOpenInterest"] = "100"
    for side in ("BUY", "SELL"):
        result = analyze(data, side=side)["components"]["open_interest"]["features"]
        assert result["change_base"] == "-30"
        assert Decimal(result["change_pct"]) < 0


def test_latest_funding_strictly_before_cutoff_is_an_actual_unannualized_rate():
    data = bundle()
    data["funding"]["rows"].extend(
        [
            {
                "symbol": "TESTUSDT",
                "fundingTime": CUTOFF - HOUR,
                "fundingRate": "-0.0002",
            },
            {"symbol": "TESTUSDT", "fundingTime": CUTOFF, "fundingRate": "0.2"},
        ]
    )
    result = component(data, "funding")["features"]
    assert result == {
        "actual_rate": "-0.0002",
        "actual_rate_pct": "-0.02",
        "funding_time_ms": CUTOFF - HOUR,
        "age_ms": HOUR,
        "rate_basis": "ACTUAL_SETTLED_FUNDING_RATE",
        "funding_interval_hours": None,
    }


@pytest.mark.parametrize(
    "age,observed", [(12 * HOUR, True), (12 * HOUR + 1, False), (0, False)]
)
def test_funding_staleness_and_exact_cutoff(age, observed):
    data = bundle()
    data["funding"]["rows"] = [
        {
            "symbol": "TESTUSDT",
            "fundingTime": CUTOFF - age,
            "fundingRate": "0",
        }
    ]
    result = component(data, "funding")
    assert (result["status"] == "OBSERVED") == observed
    if not observed:
        assert result["reason"] == "FUNDING_MISSING_OR_STALE"


@pytest.mark.parametrize("name", ["spot", "open_interest", "funding"])
def test_missing_component_preserves_other_evidence(name):
    data = bundle()
    del data[name]
    result = analyze(data)
    assert result["status"] == "PARTIAL"
    assert result["components"][name]["reason"] == "SOURCE_NOT_SUPPLIED"
    assert all(
        c["status"] == "OBSERVED" for n, c in result["components"].items() if n != name
    )
    assert result["input_digest"]


@pytest.mark.parametrize("name", ["spot", "open_interest", "funding"])
def test_unavailable_source_keeps_bounded_reader_reason(name):
    data = bundle()
    data[name] = {"status": "UNKNOWN", "reason": "HTTP_429"}
    assert component(data, name)["reason"] == "HTTP_429"


@pytest.mark.parametrize("name", ["spot", "open_interest", "funding"])
def test_wrong_source_rejected_independently(name):
    data = bundle()
    data[name]["source"] = "BINANCE_USDM_SANDBOX"
    assert component(data, name)["reason"] == "SOURCE_MISMATCH"
    assert analyze(data)["status"] == "PARTIAL"


@pytest.mark.parametrize(
    "mapping",
    [
        None,
        {},
        {"status": "UNKNOWN"},
        {"spot_symbol": "OTHERUSDT"},
        {"base_asset": "1000TEST"},
        {"quote_asset": "USDC"},
        {"basis": "FUZZY_MATCH"},
    ],
)
def test_spot_requires_exact_mapping_without_disabling_derivatives(mapping):
    data = bundle()
    if mapping and "status" not in mapping:
        data["mapping"].update(mapping)
    else:
        data["mapping"] = mapping
    result = analyze(data)
    assert result["components"]["spot"]["status"] == "UNKNOWN"
    assert result["components"]["open_interest"]["status"] == "OBSERVED"
    assert result["components"]["funding"]["status"] == "OBSERVED"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing", "SPOT_WINDOW_GAP"),
        ("duplicate", "DUPLICATE_SPOT_BAR"),
        ("misaligned", "MISALIGNED_SPOT_BAR"),
        ("close", "INVALID_SPOT_CLOSE_TIME"),
        ("row", "INVALID_SPOT_ROW"),
        ("ohlc", "INVALID_SPOT_OHLC"),
        ("buy_quote", "INCONSISTENT_SPOT_VOLUME"),
        ("buy_base", "INCONSISTENT_SPOT_VOLUME"),
        ("zero_base", "INCONSISTENT_SPOT_VOLUME"),
        ("count", "INVALID_SPOT_TRADE_COUNT"),
        ("timestamp", "INVALID_SPOT_TIMESTAMP"),
    ],
)
def test_spot_corruption_never_fills_or_synthesizes_evidence(change, reason):
    data = bundle()
    rows = data["spot"]["rows"]
    if change == "missing":
        rows.pop(1)
    elif change == "duplicate":
        rows.append(deepcopy(rows[1]))
    elif change == "row":
        rows[1].pop()
    else:
        index, value = {
            "misaligned": (0, START + HOUR + 1),
            "close": (6, CUTOFF + HOUR),
            "ohlc": (2, "99"),
            "buy_quote": (10, "101"),
            "buy_base": (9, "3"),
            "zero_base": (5, "0"),
            "count": (8, True),
            "timestamp": (0, "bad"),
        }[change]
        rows[1][index] = value
    result = component(data, "spot")
    assert result["reason"] == reason
    assert result["features"] is None and result["input_digest"] is None


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing", "OI_WINDOW_GAP"),
        ("end_missing", "OI_WINDOW_GAP"),
        ("duplicate", "DUPLICATE_OI_POINT"),
        ("misaligned", "MISALIGNED_OI_POINT"),
        ("symbol", "OI_SYMBOL_MISMATCH"),
        ("timestamp", "INVALID_OI_TIMESTAMP"),
        ("field", "MISSING_ROW_FIELD"),
        ("negative", "INVALID_DECIMAL"),
    ],
)
def test_oi_requires_exact_hourly_base_observations(change, reason):
    data = bundle()
    rows = data["open_interest"]["rows"]
    if change == "missing":
        rows.pop(4)
    elif change == "end_missing":
        rows.pop(6)
    elif change == "duplicate":
        rows.append(deepcopy(rows[4]))
    elif change == "field":
        del rows[4]["sumOpenInterest"]
    else:
        key, value = {
            "misaligned": ("timestamp", START + 4 * HOUR + 1),
            "symbol": ("symbol", "OTHERUSDT"),
            "timestamp": ("timestamp", True),
            "negative": ("sumOpenInterest", "-1"),
        }[change]
        rows[4][key] = value
    assert component(data, "open_interest")["reason"] == reason


@pytest.mark.parametrize(
    "change,reason",
    [
        ("duplicate", "DUPLICATE_FUNDING_POINT"),
        ("symbol", "FUNDING_SYMBOL_MISMATCH"),
        ("timestamp", "INVALID_FUNDING_TIMESTAMP"),
        ("field", "MISSING_ROW_FIELD"),
        ("rate", "FUNDING_RATE_BOUND"),
    ],
)
def test_funding_corruption_is_explicit_unknown(change, reason):
    data = bundle()
    rows = data["funding"]["rows"]
    if change == "duplicate":
        rows.append(deepcopy(rows[1]))
    elif change == "field":
        del rows[1]["fundingRate"]
    else:
        key, value = {
            "symbol": ("symbol", "OTHERUSDT"),
            "timestamp": ("fundingTime", 1.5),
            "rate": ("fundingRate", "1.01"),
        }[change]
        rows[1][key] = value
    assert component(data, "funding")["reason"] == reason


@pytest.mark.parametrize(
    "value",
    [
        True,
        1.2,
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "1e100",
        "1e-100",
        "0e-100000",
        "no",
    ],
)
def test_invalid_numeric_values_never_enter_any_component(value):
    data = bundle()
    data["spot"]["rows"][1][7] = value
    data["open_interest"]["rows"][4]["sumOpenInterest"] = value
    data["funding"]["rows"][1]["fundingRate"] = value
    report = analyze(data)
    assert report["status"] == "UNKNOWN"
    assert all(c["features"] is None for c in report["components"].values())
    assert report["input_digest"] is None
    json.dumps(report, allow_nan=False)


def test_zero_spot_volume_preserves_known_zero_values_and_undefined_ratios():
    data = bundle()
    for row in data["spot"]["rows"]:
        for index in (5, 7, 9, 10):
            row[index] = "0"
    features = component(data, "spot")["features"]
    assert features["recent"]["buy_quote"] == "0"
    assert features["recent"]["sell_quote"] == "0"
    assert features["recent"]["aggressor_delta_quote"] == "0"
    assert features["recent"]["imbalance_ratio"] is None
    assert features["quote_volume_ratio"] is None
    assert features["undefined_ratios"] == ["ZERO_BASELINE_QUOTE_VOLUME"]


def test_zero_initial_oi_keeps_change_known_without_division():
    data = bundle()
    data["open_interest"]["rows"][3]["sumOpenInterest"] = "0"
    features = component(data, "open_interest")["features"]
    assert features["change_base"] == "160"
    assert features["change_pct"] is None
    assert features["undefined_ratios"] == ["ZERO_START_OPEN_INTEREST"]


def test_retrospective_receipt_is_explicit_and_does_not_reject_historical_data():
    result = analyze()
    for item in result["components"].values():
        assert item["status"] == "OBSERVED"
        assert item["receipt"] == {
            "fetched_at_ms": START + DAY + HOUR,
            "status": "HISTORICAL_RECEIPT_UNPROVEN",
            "availability": "RETROSPECTIVE_RESEARCH_ONLY",
            "historical_receipt_proven": False,
        }


def test_declared_fetch_at_cutoff_is_not_promoted_to_historical_proof():
    data = bundle()
    for name in ("spot", "open_interest", "funding"):
        data[name]["fetched_at_ms"] = CUTOFF
    result = analyze(data)
    assert result["status"] == "OBSERVED"
    assert result["input_digest"] == analyze()["input_digest"]
    for item in result["components"].values():
        assert item["receipt"]["status"] == "RECEIPT_REPORTED_AT_OR_BEFORE_CUTOFF"
        assert item["receipt"]["historical_receipt_proven"] is False


@pytest.mark.parametrize("name", ["spot", "open_interest", "funding"])
def test_fetch_cannot_precede_selected_observations(name):
    data = bundle()
    data[name]["fetched_at_ms"] = START - 1
    assert component(data, name)["reason"] == "FETCH_PRECEDES_SELECTED_EVENT"


@pytest.mark.parametrize("value", [None, True, -1, 1.2, "1800000000000", 2**53])
def test_invalid_receipt_timestamp_is_unknown(value):
    data = bundle()
    data["spot"]["fetched_at_ms"] = value
    assert component(data, "spot")["reason"] == "INVALID_FETCH_TIMESTAMP"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": "LONG"},
        {"side": None},
        {"window_hours": 0},
        {"window_hours": True},
        {"window_hours": 13},
        {"cutoff_ms": CUTOFF - 1},
        {"cutoff_ms": True},
        {"cutoff_ms": 0},
        {"cutoff_ms": 2**53},
    ],
)
def test_invalid_call_configuration_raises(kwargs):
    with pytest.raises(ValueError, match="FLOW_CONFIGURATION_INVALID"):
        analyze(**kwargs)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"version": "v0"}, "BUNDLE_VERSION_INVALID"),
        ({"source": "BINANCE_SANDBOX_PUBLIC"}, "BUNDLE_SOURCE_INVALID"),
        ({"symbol": "TEST/USDT"}, "BUNDLE_SYMBOL_INVALID"),
        ({"symbol": "TESTUSDC"}, "BUNDLE_SYMBOL_INVALID"),
        ({"start_ms": START + 1}, "BUNDLE_DAY_OR_WINDOW_INVALID"),
        ({"end_ms": START + 2 * DAY}, "BUNDLE_DAY_OR_WINDOW_INVALID"),
        ({"start_ms": False}, "BUNDLE_DAY_OR_WINDOW_INVALID"),
    ],
)
def test_invalid_bundle_metadata_is_unknown(change, reason):
    data = bundle()
    data.update(change)
    result = analyze(data)
    assert result["reason"] == reason and result["input_digest"] is None
    assert all(c["status"] == "UNKNOWN" for c in result["components"].values())


def test_cutoff_window_must_fit_requested_utc8_day():
    assert (
        analyze(cutoff_ms=START + 5 * HOUR)["reason"] == "BUNDLE_DAY_OR_WINDOW_INVALID"
    )


def test_utc8_civil_day_is_accepted_and_utc_day_is_rejected():
    assert START % DAY == 16 * HOUR
    assert analyze()["status"] == "OBSERVED"
    data = bundle()
    data.update(start_ms=START + 8 * HOUR, end_ms=START + 8 * HOUR + DAY)
    assert (
        analyze(data, cutoff_ms=CUTOFF + 8 * HOUR)["reason"]
        == "BUNDLE_DAY_OR_WINDOW_INVALID"
    )


@pytest.mark.parametrize("symbol", ["币安人生USDT", "TOKEN_NAMEUSDT", "testUSDT"])
def test_public_symbols_need_not_fit_engine_order_symbol_rules(symbol):
    data = bundle()
    data["symbol"] = symbol
    data["mapping"].update(spot_symbol=symbol, base_asset=symbol[:-4])
    for name in ("open_interest", "funding"):
        for row in data[name]["rows"]:
            row["symbol"] = symbol
    assert analyze(data)["status"] == "OBSERVED"


@pytest.mark.parametrize("data", [False, "bad", [], 123])
def test_non_mapping_bundle_returns_unknown_without_execution(data):
    report = analyze_flow_evidence(data, cutoff_ms=CUTOFF, side="BUY")
    assert report["status"] == "UNKNOWN"
    assert report["reason"] == "BUNDLE_VERSION_INVALID"
    assert report["input_digest"] is None


@pytest.mark.parametrize("name", ["spot", "open_interest", "funding"])
def test_source_row_count_is_bounded(name):
    data = bundle()
    data[name]["rows"] *= 600
    assert component(data, name)["reason"] == "SOURCE_ROWS_BOUND"


def test_order_decimal_context_and_equivalent_notation_do_not_change_evidence():
    data = bundle()
    expected = analyze(data)
    for row in data["spot"]["rows"]:
        for index in (1, 2, 3, 4, 5, 7, 9, 10):
            row[index] = Decimal(row[index])
    for row in data["open_interest"]["rows"]:
        row["sumOpenInterest"] += ".000"
    for name in ("spot", "open_interest", "funding"):
        data[name]["rows"].reverse()
    with localcontext() as ctx:
        ctx.prec = 3
        ctx.traps[Inexact] = True
        assert analyze(data) == expected


def test_input_is_immutable_json_safe_and_has_no_signal_or_mechanism_claim():
    data = bundle()
    saved = deepcopy(data)
    report = analyze(data)
    assert data == saved
    json.dumps(report, allow_nan=False)
    forbidden = {
        "signal",
        "order",
        "recommendation",
        "probability",
        "confidence",
        "liquidation",
        "absorption",
    }
    for item in (report, *report["components"].values()):
        assert not forbidden.intersection(item)
    assert any("not net capital inflow" in text for text in report["limitations"])


def test_selected_value_changes_input_digest():
    data = bundle()
    expected = analyze(data)
    data["spot"]["rows"][4][10] = "151"
    assert analyze(data)["input_digest"] != expected["input_digest"]


def test_later_fixed_cutoffs_use_only_their_own_closed_windows():
    for hour in (6, 12, 18):
        report = analyze(cutoff_ms=START + hour * HOUR)
        assert report["status"] == "OBSERVED"
        assert report["windows"]["baseline"]["start_ms"] == START + (hour - 6) * HOUR
        assert report["windows"]["recent"]["start_ms"] == START + (hour - 3) * HOUR
