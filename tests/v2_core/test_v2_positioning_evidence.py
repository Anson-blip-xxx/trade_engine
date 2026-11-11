from copy import deepcopy
from decimal import localcontext

import pytest

from v2_core.evidence import canonical, digest
from v2_core.positioning_evidence import (
    ENDPOINTS,
    HOUR,
    SOURCE,
    STEP,
    VERSION,
    describe,
)

NOW = 20_000 * 24 * HOUR + 12 * HOUR + 120_000
TARGET = "TESTUSDT"


def rows(name):
    if name == "hourly_klines":
        end = NOW // HOUR * HOUR
        return [
            [
                t,
                "100",
                "101",
                "99",
                "100",
                "10",
                t + HOUR - 1,
                "1000",
                5,
                "4",
                "400",
                "0",
            ]
            for t in range(end - 72 * HOUR, end, HOUR)
        ]
    if name == "premium_index":
        return [
            {
                "symbol": TARGET,
                "time": NOW,
                "markPrice": "100",
                "indexPrice": "100",
                "lastFundingRate": "0",
                "nextFundingTime": NOW + HOUR,
            }
        ]
    if name == "funding_history":
        return [
            {"symbol": TARGET, "fundingTime": NOW - h * HOUR, "fundingRate": "0"}
            for h in (24, 16, 8, 0)
        ]
    end = NOW // STEP * STEP
    values = []
    for i, t in enumerate(range(end - 6 * HOUR, end + STEP, STEP)):
        item = {"symbol": TARGET, "timestamp": t}
        if name == "open_interest":
            item.update(
                sumOpenInterest=str(100 + i), sumOpenInterestValue=str(10000 + 100 * i)
            )
        else:
            item.update(longAccount="0.6", shortAccount="0.4", longShortRatio="1.5")
        values.append(item)
    return values


def seal(envelope):
    envelope["sha256"] = digest(canonical({"rows": envelope["rows"]}))
    return envelope


def bundle():
    return {
        "version": VERSION,
        "source": SOURCE,
        "source_host": "fapi.binance.com",
        "symbol": TARGET,
        "started_at_ms": NOW,
        "finished_at_ms": NOW,
        "components": {
            name: seal(
                {
                    "source": SOURCE,
                    "source_host": "fapi.binance.com",
                    "endpoint": endpoint,
                    "status": "OBSERVED",
                    "reason": None,
                    "fetched_at_ms": NOW,
                    "rows": rows(name),
                }
            )
            for name, endpoint in ENDPOINTS.items()
        },
    }


def report(value=None, **changes):
    return describe(value or bundle(), **{"cutoff_ms": NOW, "as_of_ms": NOW, **changes})


def test_complete_descriptors_have_no_authority_and_preserve_first_receipt():
    r = report()
    assert r["status"] == "OBSERVED"
    assert not any(
        r[k]
        for k in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )
    oi = r["components"]["open_interest"]
    assert oi["features"]["window_6h"]["base"]["change_pct"] == "72"
    assert oi["features"]["window_6h"]["value_usdt"]["change_pct"] == "72"
    assert oi["availability"] == "TIMELY_FIRST_SEEN"
    assert oi["historical_receipt_proven"] is False
    assert oi["first_seen_after_event_ms"] == 120_000
    hourly = r["components"]["hourly_klines"]["features"]
    assert hourly["complete_72h"] is True
    assert hourly["window_3h"]["direction"] == "FLAT"
    assert hourly["window_3h"]["taker_buy_fraction"] == "0.4"
    assert (
        r["components"]["funding_history"]["features"]["latest"]["actual_settled_rate"]
        == "0"
    )
    premium = r["components"]["premium_index"]["features"]
    assert premium["quoted_rate"] == "0"
    assert premium["future_actual_rate_known"] is False


@pytest.mark.parametrize(
    "name", ["open_interest", "global_accounts", "top_accounts", "top_positions"]
)
def test_publication_lag_keeps_complete_six_hour_window_anchored_to_latest(name):
    b = bundle()
    envelope = b["components"][name]
    for row in envelope["rows"]:
        row["timestamp"] -= STEP
    seal(envelope)
    component = report(b)["components"][name]
    features = component["features"]
    assert component["status"] == "OBSERVED"
    assert features["age_ms"] == 120_000 + STEP
    assert features["window_6h"]["status"] == "OBSERVED"
    assert features["window_6h"]["end_ms"] == NOW // STEP * STEP - STEP
    assert features["window_6h"]["start_ms"] == NOW // STEP * STEP - STEP - 6 * HOUR


@pytest.mark.parametrize("age_extra_ms,expected", [(0, "OBSERVED"), (1, "UNKNOWN")])
def test_ten_minute_buffer_never_relaxes_staleness_boundary(age_extra_ms, expected):
    b = bundle()
    envelope = b["components"]["open_interest"]
    for row in envelope["rows"]:
        row["timestamp"] -= 2 * STEP
    seal(envelope)
    cutoff = NOW // STEP * STEP + age_extra_ms
    component = report(b, cutoff_ms=cutoff)["components"]["open_interest"]
    assert component["status"] == expected
    if expected == "OBSERVED":
        assert component["features"]["window_6h"]["status"] == "OBSERVED"
        assert component["features"]["age_ms"] == 2 * STEP
    else:
        assert component["reason"] == "STALE_STATISTICS"


def test_only_selected_rows_enter_digest_and_future_bad_numbers_are_ignored():
    b = bundle()
    baseline = report(b)
    for name in ("open_interest", "global_accounts", "top_accounts", "top_positions"):
        b["components"][name]["rows"].append(
            {"timestamp": NOW + HOUR, "symbol": "WRONG", "sumOpenInterest": "NaN"}
        )
        seal(b["components"][name])
    b["components"]["funding_history"]["rows"].append(
        {"fundingTime": NOW + HOUR, "fundingRate": "NaN"}
    )
    seal(b["components"]["funding_history"])
    b["components"]["hourly_klines"]["rows"].append(
        [NOW // HOUR * HOUR, "bad future candle"]
    )
    seal(b["components"]["hourly_klines"])
    assert report(b) == baseline


def test_historical_cutoff_is_retrospective_not_a_causal_backtest():
    r = report(cutoff_ms=NOW - 3 * HOUR)
    assert r["components"]["open_interest"]["availability"] == "RETROSPECTIVE"
    assert r["components"]["premium_index"]["status"] == "UNKNOWN"
    assert r["historical_receipt_proven"] is False


def test_cannot_use_receipt_after_asof():
    r = report(cutoff_ms=NOW - 1, as_of_ms=NOW - 1)
    assert r["status"] == "UNKNOWN"
    assert {c["reason"] for c in r["components"].values()} == {"NOT_AVAILABLE_AS_OF"}


def test_zero_oi_and_zero_volume_are_not_missing_or_infinite():
    b = bundle()
    for row in b["components"]["open_interest"]["rows"]:
        row.update(sumOpenInterest="0", sumOpenInterestValue="0")
    for row in b["components"]["hourly_klines"]["rows"]:
        for i in (5, 7, 9, 10):
            row[i] = "0"
    for name in ("open_interest", "hourly_klines"):
        seal(b["components"][name])
    r = report(b)
    assert r["status"] == "OBSERVED"
    change = r["components"]["open_interest"]["features"]["window_6h"]["base"]
    assert change == {
        "start": "0",
        "end": "0",
        "change": "0",
        "change_pct": None,
        "zero_start": True,
    }
    flow = r["components"]["hourly_klines"]["features"]["window_3h"]
    assert flow["zero_volume"] is True and flow["taker_buy_fraction"] is None


@pytest.mark.parametrize("name", list(ENDPOINTS))
def test_missing_component_is_local_and_other_components_remain_usable(name):
    b = bundle()
    del b["components"][name]
    r = report(b)
    assert r["status"] == "PARTIAL"
    assert sum(c["status"] == "OBSERVED" for c in r["components"].values()) == 6


@pytest.mark.parametrize(
    "bad", ["NaN", "Infinity", "-Infinity", True, 0.5, "1e10000", "1e-10000", "-1"]
)
def test_invalid_or_negative_oi_is_unknown_not_zero(bad):
    b = bundle()
    e = b["components"]["open_interest"]
    e["rows"][-1]["sumOpenInterest"] = bad
    if isinstance(bad, float):
        e["sha256"] = "0" * 64
    else:
        seal(e)
    assert report(b)["components"]["open_interest"]["status"] == "UNKNOWN"


def test_missing_middle_point_suppresses_window_not_latest_point():
    b = bundle()
    e = b["components"]["open_interest"]
    del e["rows"][-10]
    seal(e)
    features = report(b)["components"]["open_interest"]["features"]
    assert features["latest"]["base"] == "172"
    assert features["window_3h"]["status"] == "UNKNOWN"
    assert features["window_6h"]["status"] == "UNKNOWN"


def test_duplicate_or_wrong_symbol_never_silently_deduplicated():
    for mutation in ("duplicate", "symbol"):
        b = bundle()
        e = b["components"]["top_positions"]
        if mutation == "duplicate":
            e["rows"].append(deepcopy(e["rows"][-1]))
        else:
            e["rows"][-1]["symbol"] = "OTHERUSDT"
        seal(e)
        assert report(b)["components"]["top_positions"]["status"] == "UNKNOWN"


def test_position_share_and_account_share_are_not_interchangeable():
    r = report()
    assert r["components"]["top_positions"]["features"]["basis"] == "POSITION_SHARE"
    assert r["components"]["top_accounts"]["features"]["basis"] == "ACCOUNT_SHARE"
    assert (
        r["components"]["top_accounts"]["features"]["window_3h"]["long_share_change_pp"]
        == "0"
    )


def test_share_changes_are_percentage_points_and_zero_share_is_observed():
    b = bundle()
    e = b["components"]["top_positions"]
    e["rows"][-1].update(longAccount="0", shortAccount="1", longShortRatio="0")
    seal(e)
    r = report(b)["components"]["top_positions"]
    assert r["status"] == "OBSERVED"
    assert r["features"]["window_3h"]["long_share_change_pp"] == "-60"
    assert r["features"]["window_3h"]["short_share_change_pp"] == "60"


def test_future_events_are_not_allowed_to_predate_local_receipt():
    b = bundle()
    e = b["components"]["premium_index"]
    e["rows"][0]["time"] = NOW + 1
    seal(e)
    r = report(b, cutoff_ms=NOW + 2, as_of_ms=NOW + 2)
    assert r["components"]["premium_index"]["reason"] == "EVENT_AFTER_RECEIPT"


def test_settlement_interval_change_is_observed_not_future_schedule():
    b = bundle()
    e = b["components"]["funding_history"]
    e["rows"] = [
        {"symbol": TARGET, "fundingTime": t, "fundingRate": rate}
        for t, rate in [
            (NOW - 5 * HOUR, "0.0001"),
            (NOW - HOUR, "-0.02"),
            (NOW, "-0.003"),
        ]
    ]
    seal(e)
    f = report(b)["components"]["funding_history"]["features"]
    assert f["previous_interval_ms"] == HOUR
    assert f["latest"]["actual_settled_rate"] == "-0.003"
    assert not f["future_interval_known"]


def test_corrupt_digest_source_and_clock_are_not_trusted():
    for field, value in (
        ("sha256", "0" * 64),
        ("source_host", "demo-fapi.binance.com"),
        ("fetched_at_ms", NOW + 1),
        ("endpoint", "/fapi/v1/order"),
    ):
        b = bundle()
        b["components"]["premium_index"][field] = value
        assert report(b)["components"]["premium_index"]["status"] == "UNKNOWN"


def test_ambient_decimal_precision_does_not_change_results():
    expected = report()
    with localcontext() as ctx:
        ctx.prec = 5
        assert report() == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "other"),
        ("source", "SANDBOX"),
        ("symbol", "BTC/USDT"),
        ("finished_at_ms", NOW - 1),
    ],
)
def test_invalid_bundle_rejected(field, value):
    b = bundle()
    b[field] = value
    with pytest.raises(ValueError):
        report(b)


def test_future_cutoff_cannot_exceed_asof():
    with pytest.raises(ValueError):
        report(cutoff_ms=NOW + 1)


def test_invalid_candle_and_stale_premium_are_local_unknown():
    b = bundle()
    b["components"]["hourly_klines"]["rows"][-1][9] = "11"
    seal(b["components"]["hourly_klines"])
    b["components"]["premium_index"]["rows"][0]["time"] = NOW - 2 * STEP
    seal(b["components"]["premium_index"])
    r = report(b)
    assert r["components"]["hourly_klines"]["status"] == "UNKNOWN"
    assert r["components"]["premium_index"]["reason"] == "STALE_PREMIUM"
    assert r["components"]["funding_history"]["status"] == "OBSERVED"
