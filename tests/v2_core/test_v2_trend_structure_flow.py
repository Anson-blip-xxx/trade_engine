"""Version6 source/consumer handshake; no active service or exchange writes."""

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_v2_directional_replay import SCOPE
from test_v2_indicator_v3_flow import archived_stage
from test_v2_indicator_v4_flow import archived_reference
from test_v2_s3_candles import batch
from test_v2_trend_entry import scenario

from services.v2_market_hub_entry import configured_feature_version
from services.v2_s0_regime import build_s0_state
from services.v2_s3_candles import PRICE_VERSION, STRUCTURE_VERSION, build_frame
from services.v2_s3_runtime import evaluate
from services.v2_s3_source import frame_digest
from services.v2_shared_market import IncrementalCandles
from services.v2_shared_risk_reference import SharedRiskReference


def test_feature_version_is_opt_in_and_source_reference_rebuilds_exact_frame():
    assert configured_feature_version({}) == PRICE_VERSION
    assert (
        configured_feature_version({"V2_MARKET_FEATURE_VERSION": STRUCTURE_VERSION})
        == STRUCTURE_VERSION
    )
    source = batch()
    at = source["closed_at"]
    archive = SimpleNamespace(read=lambda *_: source["candles"]["BTCUSDT"])
    producer = IncrementalCandles(
        SimpleNamespace(environment="SANDBOX"),
        SimpleNamespace(hget=lambda *_: None),
        archive,
        feature_version=STRUCTURE_VERSION,
    )
    frame = producer.prepare("BTCUSDT", at)["frame"]
    assert frame["frame_id"] == f"s3-shared-v6:BTCUSDT:{at}"
    assert frame["windows"]["BTCUSDT"]["trend_structure"]["status"] == "READY"
    provider = SharedRiskReference(
        lambda: pytest.fail("injected read-only head"),
        scope=SCOPE,
        archive=archive,
        clock_ms=lambda: at + 1,
    )
    head = [at, frame["frame_id"], frame_digest(frame)]
    provider._head = lambda _: tuple(head)
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}
    assert provider(request).price == "101"
    head[2] = "0" * 64
    with pytest.raises(ValueError, match="REFERENCE_INPUT_MISMATCH"):
        provider(request)


def test_archived_v6_and_s0_support_exact_version_and_reject_mixed_basket():
    provider, _ = archived_reference(STRUCTURE_VERSION)
    assert (
        provider({**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}).price == "101"
    )
    stage, _, published = archived_stage(STRUCTURE_VERSION)
    assert stage.run_once()["status"] == "PROJECTED"
    assert len(published) == 1
    frame = build_frame(
        batch(), environment="SANDBOX", feature_version=STRUCTURE_VERSION
    )
    windows = frame["windows"]
    windows["ETHUSDT"] = deepcopy(windows["BTCUSDT"])
    kwargs = {
        "universe": list(windows),
        "s3_frame_id": "test",
        "s3_input_digest": "a" * 64,
    }
    assert (
        build_s0_state(windows, **kwargs)["evidence"]["closed_at"]
        == frame["observed_at"]
    )
    windows["ETHUSDT"]["input_evidence"]["window_version"] = PRICE_VERSION
    with pytest.raises(ValueError):
        build_s0_state(windows, **kwargs)


@pytest.mark.parametrize("side,event", [("LONG", "TREND_UP"), ("SHORT", "TREND_DOWN")])
def test_s3_v6_uses_true_closed_structure_not_minute_extension(side, event):
    _, context, _ = scenario(side)
    windows = context["directional"]["market"]
    # Old rolling-window values would suppress trend or mislabel its amplitude.
    windows["1h"]["chg"] = "0.1" if side == "SHORT" else "-0.1"
    windows["4h"]["chg"] = "0.1" if side == "SHORT" else "-0.1"
    windows["24h"]["chg"] = "0"
    raw = batch()["candles"]["BTCUSDT"]
    events, _ = evaluate(
        {"BTCUSDT": windows},
        {"BTCUSDT": {"4h": raw[:240], "15m": raw[:15]}},
        {},
        86400000,
    )
    matches = [row for row in events if row["type"] == event]
    assert len(matches) == 1
    assert matches[0]["strength"] == 15
