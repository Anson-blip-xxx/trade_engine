from copy import deepcopy

import pytest

from services.v2_market_publishers import S0Publisher, S3Publisher, feature_values
from v2_core.producer import market_envelope


class Publisher:
    def __init__(self, source):
        self.source, self.calls = source, []

    def publish(self, **frame):
        self.calls.append(deepcopy(frame))
        return {"status": "RECORDED"}


def test_s0_preserves_original_identity_and_time_with_no_signal():
    publisher = Publisher("s0")
    state = {"regime": "range", "breadth": 0.52, "risk_off": False}
    before = deepcopy(state)
    assert (
        S0Publisher(publisher).publish_state(state, frame_id="s0:1", observed_at=100)[
            "status"
        ]
        == "RECORDED"
    )
    assert publisher.calls == [
        {
            "frame_id": "s0:1",
            "observed_at": 100,
            "contexts": {
                "*": {"regime": "range", "breadth": "0.52", "risk_off": False}
            },
            "events": [],
        }
    ]
    assert state == before


def test_s3_preserves_computed_features_and_end_is_not_opening_type():
    publisher = Publisher("s3")
    windows = {"BTCUSDT": {"1h": {"atr": 1.25}}}
    events = [
        {
            "event_id": "s3:1:end",
            "symbol": "BTCUSDT",
            "type": "TREND_UP",
            "state": "END",
            "strength": 70,
        }
    ]
    before = deepcopy((windows, events))
    S3Publisher(publisher).publish_frame(
        frame_id="s3:1", observed_at=100, windows=windows, events=events
    )
    assert publisher.calls[0]["events"][0]["signal"] == "EVENT_END"
    assert publisher.calls[0]["events"][0]["features"]["type"] == "TREND_UP"
    assert publisher.calls[0]["contexts"]["BTCUSDT"]["1h"]["atr"] == "1.25"
    assert (windows, events) == before


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf"), object(), {1: "bad"}]
)
def test_feature_normalization_rejects_unsupported_values(bad):
    with pytest.raises(ValueError):
        feature_values({"feature": bad})


def test_s3_cannot_invent_missing_event_identity_or_lifecycle():
    publisher = Publisher("s3")
    for event in (
        {"type": "TREND_UP", "symbol": "BTCUSDT"},
        {
            "event_id": "s3:1",
            "type": "TREND_UP",
            "symbol": "BTCUSDT",
            "state": "UNKNOWN",
        },
    ):
        with pytest.raises(ValueError):
            S3Publisher(publisher).publish_frame(
                frame_id="s3:1",
                observed_at=100,
                windows={"BTCUSDT": {}},
                events=[event],
            )
    assert not publisher.calls


@pytest.mark.parametrize(
    "source,target", [("s0", "BTCUSDT"), ("s3", "*"), ("s2", "*"), ("bad", "BTCUSDT")]
)
def test_market_scope_is_explicit(source, target):
    with pytest.raises(ValueError):
        market_envelope(source, "SANDBOX", target, 100, {})


def test_market_envelope_hashes_scope_time_and_frozen_features():
    features = {"price": "100"}
    original = market_envelope("s3", "SANDBOX", "BTCUSDT", 100, features)
    features["price"] = "999"
    assert original["features"]["price"] == "100"
    assert (
        original["snapshot_id"]
        != market_envelope("s3", "LIVE", "BTCUSDT", 100, {"price": "100"})[
            "snapshot_id"
        ]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"snapshot_id": "another-digest"},
        {"observed_at": 101},
        {"environment": "LIVE"},
        {"symbol": "ETHUSDT"},
        {"source": "s2"},
        {"unexpected": "field"},
    ],
)
def test_context_provider_rejects_missing_or_mismatched_producer_context(changes):
    from v2_core.ingress import ContextProvider, IntakeRejected

    envelope = market_envelope("s3", "SANDBOX", "BTCUSDT", 100, {"price": "100"})
    provider = ContextProvider(
        environment="SANDBOX",
        policy={"s3": {"scope": "SYMBOL", "max_age_ms": 50}},
        read=lambda *_: envelope,
        clock_ms=lambda: 120,
    )
    reference = {**{k: v for k, v in envelope.items() if k != "features"}, **changes}
    with pytest.raises(IntakeRejected):
        provider({"symbol": "BTCUSDT", "features": {"producer_context": reference}})


def test_newer_valid_context_can_satisfy_original_producer_reference():
    from v2_core.ingress import ContextProvider

    old = market_envelope("s3", "SANDBOX", "BTCUSDT", 100, {"price": "100"})
    newer = market_envelope("s3", "SANDBOX", "BTCUSDT", 110, {"price": "101"})
    provider = ContextProvider(
        environment="SANDBOX",
        policy={"s3": {"scope": "SYMBOL", "max_age_ms": 50}},
        read=lambda *_: newer,
        clock_ms=lambda: 120,
    )
    reference = {k: v for k, v in old.items() if k != "features"}
    assert (
        provider({"symbol": "BTCUSDT", "features": {"producer_context": reference}})[
            "sources"
        ]["s3"]
        == newer
    )
