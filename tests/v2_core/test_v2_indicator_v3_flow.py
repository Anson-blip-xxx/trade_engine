from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_v2_directional_replay import SCOPE
from test_v2_s3_candles import batch

from services.v2_s0_regime import ArchivedS0Stage, build_s0_state
from services.v2_s3_candles import PRICE_VERSION, ROLLING_VERSION, VERSION, build_frame
from services.v2_s3_source import frame_digest
from services.v2_shared_risk_reference import SharedRiskReference
from v2_core.evidence import canonical, digest


@pytest.mark.parametrize(
    "version,prefix",
    [
        (VERSION, "s3-shared-v1"),
        (PRICE_VERSION, "s3-shared-v2"),
        (ROLLING_VERSION, "s3-shared-v3"),
    ],
)
def test_reference_reconstructs_exact_pg_attested_version(version, prefix):
    source = batch()
    observed = source["closed_at"]
    frame = build_frame(source, environment="SANDBOX", feature_version=version)
    head = [observed, f"{prefix}:BTCUSDT:{observed}", frame_digest(frame)]
    reads = []

    def archive_read(target, start, end):
        reads.append((target, start, end))
        return source["candles"][target]

    provider = SharedRiskReference(
        lambda: pytest.fail("use immutable PG head fixture"),
        scope=SCOPE,
        archive=SimpleNamespace(read=archive_read),
        clock_ms=lambda: observed + 1,
    )
    provider._head = lambda _: tuple(head)
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}
    assert provider(request).price == "101"
    assert reads == [("BTCUSDT", observed - 86400000, observed)]
    head[2] = "0" * 64
    with pytest.raises(ValueError, match="REFERENCE_INPUT_MISMATCH"):
        provider(request)
    head[1] = f"s3-shared-v99:BTCUSDT:{observed}"
    with pytest.raises(ValueError, match="REFERENCE_FEATURE_VERSION_UNSUPPORTED"):
        provider(request)


def test_reference_v3_cannot_use_v2_fingerprint_or_mutated_archive():
    source = batch()
    observed = source["closed_at"]
    old = build_frame(source, environment="SANDBOX", feature_version=PRICE_VERSION)
    new = build_frame(source, environment="SANDBOX", feature_version=ROLLING_VERSION)
    head = [observed, f"s3-shared-v3:BTCUSDT:{observed}", frame_digest(old)]
    provider = SharedRiskReference(
        lambda: pytest.fail("injected readonly head"),
        scope=SCOPE,
        archive=SimpleNamespace(read=lambda *_: source["candles"]["BTCUSDT"]),
        clock_ms=lambda: observed + 1,
    )
    provider._head = lambda _: tuple(head)
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "BTCUSDT"}
    with pytest.raises(ValueError, match="REFERENCE_INPUT_MISMATCH"):
        provider(request)
    head[2] = frame_digest(new)
    source["candles"]["BTCUSDT"][-1]["v"] = "11"
    with pytest.raises(ValueError, match="REFERENCE_INPUT_MISMATCH"):
        provider(request)


@pytest.mark.parametrize(
    "second_version,allowed",
    [
        (ROLLING_VERSION, True),
        (PRICE_VERSION, False),
        (VERSION, False),
        ("s3-closed-1m-v99", False),
    ],
)
def test_s0_accepts_uniform_v3_and_rejects_mixed_or_unknown(second_version, allowed):
    source = batch()
    frame = build_frame(source, environment="SANDBOX", feature_version=ROLLING_VERSION)
    windows = frame["windows"]
    windows["ETHUSDT"] = deepcopy(windows["BTCUSDT"])
    windows["ETHUSDT"]["input_evidence"]["window_version"] = second_version
    kwargs = {
        "universe": list(windows),
        "s3_frame_id": "basket",
        "s3_input_digest": "a" * 64,
    }
    if allowed:
        assert (
            build_s0_state(windows, **kwargs)["evidence"]["closed_at"]
            == source["closed_at"]
        )
    else:
        with pytest.raises(ValueError):
            build_s0_state(windows, **kwargs)


def archived_stage(version):
    source = batch()
    frame = build_frame(source, environment="SANDBOX", feature_version=version)
    encoded = canonical(source)
    fingerprint = frame_digest(frame)
    head = [
        frame["frame_id"],
        frame["observed_at"],
        digest(encoded),
        fingerprint,
        "ACKNOWLEDGED",
        fingerprint,
        True,
    ]
    published = []

    def publish(state, **kwargs):
        published.append((state, kwargs))
        return {"status": "RECORDED", "market_status": "PROJECTED"}

    stage = ArchivedS0Stage(
        lambda: pytest.fail("head fixture, no DB"),
        publisher=SimpleNamespace(
            publisher=SimpleNamespace(source="s0", environment="SANDBOX"),
            publish_state=publish,
        ),
        archive=SimpleNamespace(get=lambda _: encoded),
        universe=["BTCUSDT"],
    )
    stage._head = lambda: tuple(head)
    return stage, head, published


@pytest.mark.parametrize("version", [VERSION, PRICE_VERSION, ROLLING_VERSION])
def test_archived_s0_replays_version_from_acknowledged_pg_head(version):
    stage, head, published = archived_stage(version)
    assert stage.run_once() == {"status": "PROJECTED", "frame_id": head[0]}
    assert len(published) == 1
    assert published[0][0]["evidence"]["s3_input_digest"] == head[3]


@pytest.mark.parametrize(
    "defect", ["unknown", "wrong_version", "wrong_digest", "superseded"]
)
def test_archived_s0_v3_rejects_unverified_version_or_receipt(defect):
    stage, head, published = archived_stage(ROLLING_VERSION)
    if defect == "unknown":
        head[0] = head[0].replace(ROLLING_VERSION, "s3-closed-1m-v99")
    elif defect == "wrong_version":
        head[0] = head[0].replace(ROLLING_VERSION, PRICE_VERSION)
    elif defect == "wrong_digest":
        head[3] = head[5] = "0" * 64
    else:
        captured = tuple(head)
        iterator = iter((captured, ("different", *captured[1:])))
        stage._head = lambda: next(iterator)
    with pytest.raises(ValueError):
        stage.run_once()
    assert published == []
