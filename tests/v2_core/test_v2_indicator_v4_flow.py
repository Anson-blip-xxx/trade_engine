from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_v2_directional_replay import SCOPE
from test_v2_indicator_v3_flow import archived_stage
from test_v2_s3_candles import batch

from services.v2_risk_reference import ArchivedRiskReference
from services.v2_s0_regime import build_s0_state
from services.v2_s3_candles import (
    BREAKOUT_VERSION,
    PRICE_VERSION,
    ROLLING_VERSION,
    VERSION,
    build_frame,
)
from services.v2_s3_source import frame_digest
from services.v2_shared_market import IncrementalCandles
from services.v2_shared_risk_reference import SharedRiskReference
from v2_core.evidence import canonical, digest


@pytest.mark.parametrize(
    "version,prefix",
    [(PRICE_VERSION, "v2"), (ROLLING_VERSION, "v3"), (BREAKOUT_VERSION, "v4")],
)
def test_incremental_source_builds_explicit_version_without_changing_default(
    version, prefix
):
    source = batch()
    archive = SimpleNamespace(read=lambda *_: source["candles"]["BTCUSDT"])
    cache = SimpleNamespace(hget=lambda *_: None)
    transport = SimpleNamespace(environment="SANDBOX")
    producer = IncrementalCandles(transport, cache, archive, feature_version=version)
    frame = producer.prepare("BTCUSDT", source["closed_at"])["frame"]
    assert frame["frame_id"] == f"s3-shared-{prefix}:BTCUSDT:{source['closed_at']}"
    assert frame["windows"]["BTCUSDT"]["input_evidence"]["window_version"] == version
    assert (
        IncrementalCandles(transport, cache, archive).feature_version == PRICE_VERSION
    )


def test_reference_requires_v4_receipt_instead_of_accepting_v3_features():
    source = batch()
    observed = source["closed_at"]
    old = build_frame(source, environment="SANDBOX", feature_version=ROLLING_VERSION)
    new = build_frame(source, environment="SANDBOX", feature_version=BREAKOUT_VERSION)
    head = [observed, f"s3-shared-v4:BTCUSDT:{observed}", frame_digest(old)]
    provider = SharedRiskReference(
        lambda: pytest.fail("readonly injected head"),
        scope=SCOPE,
        archive=SimpleNamespace(read=lambda *_: source["candles"]["BTCUSDT"]),
        clock_ms=lambda: observed + 1,
    )
    provider._head = lambda _: tuple(head)
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}
    with pytest.raises(ValueError, match="REFERENCE_INPUT_MISMATCH"):
        provider(request)
    head[2] = frame_digest(new)
    assert provider(request).price == "101"
    source["candles"]["BTCUSDT"][-1]["v"] = "11"
    with pytest.raises(ValueError, match="REFERENCE_INPUT_MISMATCH"):
        provider(request)


@pytest.mark.parametrize(
    "second_version",
    [BREAKOUT_VERSION, ROLLING_VERSION, PRICE_VERSION, VERSION, "s3-closed-1m-v99"],
)
def test_s0_accepts_uniform_v4_but_never_a_mixed_basket(second_version):
    frame = build_frame(
        batch(), environment="SANDBOX", feature_version=BREAKOUT_VERSION
    )
    windows = frame["windows"]
    windows["ETHUSDT"] = deepcopy(windows["BTCUSDT"])
    windows["ETHUSDT"]["input_evidence"]["window_version"] = second_version
    kwargs = {
        "universe": list(windows),
        "s3_frame_id": "basket",
        "s3_input_digest": "a" * 64,
    }
    if second_version == BREAKOUT_VERSION:
        assert (
            build_s0_state(windows, **kwargs)["evidence"]["closed_at"]
            == frame["observed_at"]
        )
    else:
        with pytest.raises(ValueError):
            build_s0_state(windows, **kwargs)


def test_archived_s0_reconstructs_acknowledged_v4():
    stage, head, published = archived_stage(BREAKOUT_VERSION)
    assert stage.run_once() == {"status": "PROJECTED", "frame_id": head[0]}
    assert published[0][0]["evidence"]["s3_input_digest"] == head[3]


def test_archived_s0_rejects_v3_identity_with_v4_receipt():
    stage, head, published = archived_stage(BREAKOUT_VERSION)
    head[0] = head[0].replace(BREAKOUT_VERSION, ROLLING_VERSION)
    with pytest.raises(ValueError, match="S0_SOURCE_RECEIPT_MISMATCH"):
        stage.run_once()
    assert published == []


def archived_reference(version):
    source = batch()
    observed = source["closed_at"]
    frame = build_frame(source, environment="SANDBOX", feature_version=version)
    encoded = canonical(source)
    fingerprint = frame_digest(frame)
    head = [
        frame["frame_id"],
        observed,
        observed + 120000,
        90000,
        digest(encoded),
        fingerprint,
        "ACKNOWLEDGED",
        fingerprint,
        True,
    ]
    provider = ArchivedRiskReference(
        lambda: pytest.fail("readonly injected head"),
        scope=SCOPE,
        archive=SimpleNamespace(get=lambda _: encoded),
        clock_ms=lambda: observed + 1,
        max_age_ms=90000,
    )
    provider._head = lambda: tuple(head)
    return provider, head


@pytest.mark.parametrize(
    "version", [VERSION, PRICE_VERSION, ROLLING_VERSION, BREAKOUT_VERSION]
)
def test_archived_risk_reference_reconstructs_exact_attested_version(version):
    provider, head = archived_reference(version)
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}
    result = provider(request)
    assert result.price == "101"
    assert result.provenance.frame_id == head[0]
    assert result.provenance.input_digest == head[5]


@pytest.mark.parametrize(
    "defect",
    ["unknown", "wrong_version", "wrong_digest", "superseded", "wrong_identity"],
)
def test_archived_v4_risk_reference_rejects_unverified_receipts(defect):
    provider, head = archived_reference(BREAKOUT_VERSION)
    if defect == "unknown":
        head[0] = head[0].replace(BREAKOUT_VERSION, "s3-closed-1m-v99")
        provider.archive.get = lambda _: pytest.fail(
            "unknown version must fail before archive read"
        )
    elif defect == "wrong_version":
        head[0] = head[0].replace(BREAKOUT_VERSION, ROLLING_VERSION)
    elif defect == "wrong_digest":
        head[5] = head[7] = "0" * 64
    elif defect == "wrong_identity":
        head[0] += ":unexpected"
    else:
        captured = tuple(head)
        iterator = iter((captured, ("different", *captured[1:])))
        provider._head = lambda: next(iterator)
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}
    with pytest.raises(ValueError):
        provider(request)
