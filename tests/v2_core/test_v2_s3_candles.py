from copy import deepcopy
from types import SimpleNamespace

import pytest

from s3.core import build_window_features, ema
from services.v2_market_publishers import feature_values
from services.v2_s3_candles import S3CandleRunner, build_frame


def batch():
    return {
        "source": "BINANCE_FUTURES",
        "environment": "SANDBOX",
        "interval": "1m",
        "closed_at": 86400000,
        "candles": {
            "BTCUSDT": [
                {
                    "t": 86400000 - (i + 1) * 60000,
                    "o": "100",
                    "h": "102",
                    "l": "99",
                    "c": "101",
                    "v": "10",
                    "tbv": "6",
                }
                for i in range(1440)
            ]
        },
    }


def build(value):
    return build_frame(value, environment="SANDBOX")


def test_windows_match_pure_core_and_preserve_source_evidence():
    source = batch()
    before = deepcopy(source)
    result = build(source)
    assert source == before
    assert result["observed_at"] == source["closed_at"]
    assert result["frame_id"] == "s3-closed-1m-v1:86400000"
    for name, count in (("15m", 15), ("1h", 60), ("4h", 240), ("24h", 1440)):
        ascending = [
            {k: float(v) if k != "t" else v for k, v in bar.items()}
            for bar in reversed(source["candles"]["BTCUSDT"][:count])
        ]
        closes = [bar["c"] for bar in ascending]
        expected = build_window_features(ascending, ema(closes, 20), ema(closes, 60))
        assert result["windows"]["BTCUSDT"][name] == feature_values(expected)
    assert len(result["raw_windows"]["BTCUSDT"]["4h"]) == 240
    assert result["raw_windows"]["BTCUSDT"]["15m"][0]["t"] == 86340000
    assert result["windows"]["BTCUSDT"]["input_evidence"]["bar_count"] == 1440


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", "LIVE"),
        ("source", "OTHER"),
        ("interval", "15m"),
        ("closed_at", 86400001),
        ("closed_at", True),
        ("closed_at", 0),
    ],
)
def test_rejects_wrong_scope_or_boundary(field, value):
    value_batch = batch()
    value_batch[field] = value
    with pytest.raises(ValueError):
        build(value_batch)


@pytest.mark.parametrize(
    "defect",
    [
        "gap",
        "duplicate",
        "reverse",
        "in_progress",
        "short",
        "extra",
        "missing_taker",
        "float",
        "nan",
        "negative",
        "taker_excess",
        "outside_ohlc",
        "underflow",
    ],
)
def test_rejects_incomplete_or_invalid_candles(defect):
    value = batch()
    bars = value["candles"]["BTCUSDT"]
    if defect == "gap":
        bars[20]["t"] -= 60000
    elif defect == "duplicate":
        bars[20] = dict(bars[19])
    elif defect == "reverse":
        bars.reverse()
    elif defect == "in_progress":
        bars[0]["t"] = value["closed_at"]
    elif defect == "short":
        bars.pop()
    elif defect == "extra":
        bars.append(dict(bars[-1]))
    elif defect == "missing_taker":
        del bars[0]["tbv"]
    else:
        key, bad = {
            "float": ("c", 100.0),
            "nan": ("c", "NaN"),
            "negative": ("v", "-1"),
            "taker_excess": ("tbv", "11"),
            "outside_ohlc": ("o", "200"),
            "underflow": ("c", "1e-999"),
        }[defect]
        bars[0][key] = bad
    with pytest.raises(ValueError):
        build(value)


def test_input_digest_detects_change_in_bar_not_used_by_breakout():
    source = batch()
    original = build(source)
    source["candles"]["BTCUSDT"][-1]["o"] = "100.5"
    changed = build(source)
    assert original["frame_id"] == changed["frame_id"]
    assert original["raw_windows"] == changed["raw_windows"]
    assert original["windows"]["BTCUSDT"]["24h"] == changed["windows"]["BTCUSDT"]["24h"]
    assert (
        original["windows"]["BTCUSDT"]["input_evidence"]
        != changed["windows"]["BTCUSDT"]["input_evidence"]
    )


def runner(*, result=None, failure=None, ack=True, source_batch=None):
    calls = []

    def process(**frame):
        calls.append(("publish", frame["frame_id"]))
        if failure:
            raise failure
        return result or {
            "status": "RECORDED",
            "market_status": "PROJECTED",
            "signal_ids": ["one"],
        }

    def acknowledge(frame_id):
        calls.append(("ack", frame_id))
        return ack

    runtime = SimpleNamespace(
        process=process,
        processor=SimpleNamespace(publisher=SimpleNamespace(environment="SANDBOX")),
    )
    source = SimpleNamespace(
        read=lambda: batch() if source_batch is None else source_batch, ack=acknowledge
    )
    return S3CandleRunner(runtime, source=source), calls


def test_ack_follows_commit_and_projection():
    instance, calls = runner()
    assert instance.run_once()["status"] == "ACKNOWLEDGED"
    assert [c[0] for c in calls] == ["publish", "ack"]


@pytest.mark.parametrize("stage", ["PUBLISH", "PROJECT", "ACK", "VALIDATE", "READ"])
def test_failures_are_bounded_and_do_not_ack_uncommitted_batch(stage):
    instance, calls = runner(
        failure=RuntimeError("secret response") if stage == "PUBLISH" else None,
        result={"status": "RECORDED", "market_status": "UNAVAILABLE"}
        if stage == "PROJECT"
        else None,
        ack=stage != "ACK",
        source_batch={} if stage == "VALIDATE" else None,
    )
    if stage == "READ":

        def failed():
            raise ConnectionError("secret endpoint")

        instance.source.read = failed
    result = instance.run_once()
    assert result["status"] == "RETRY" and result["stage"] == stage
    assert "secret" not in str(result)
    assert ("ack" in [c[0] for c in calls]) == (stage == "ACK")


def test_idle_does_not_publish_or_ack():
    instance, calls = runner()
    instance.source.read = lambda: None
    assert instance.run_once() == {"status": "IDLE"} and calls == []


def test_tiny_positive_prices_cannot_round_to_zero_context():
    source = batch()
    for bar in source["candles"]["BTCUSDT"]:
        for key in ("o", "h", "l", "c"):
            bar[key] = "1e-12"
    with pytest.raises(ValueError, match="precision"):
        build(source)


def test_multisymbol_alignment_and_deterministic_order():
    source = batch()
    source["candles"]["ETHUSDT"] = deepcopy(source["candles"]["BTCUSDT"])
    first = build(source)
    source["candles"] = dict(reversed(list(source["candles"].items())))
    assert build(source) == first
    source["candles"]["ETHUSDT"][0]["t"] -= 60000
    with pytest.raises(ValueError, match="contiguous"):
        build(source)
