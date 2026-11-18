from copy import deepcopy

import pytest

from services.v2_market_source_compare import collect_comparisons
from services.v2_s3_candles import STRUCTURE_VERSION
from services.v2_shared_market import IncrementalArchive, IncrementalCandles
from tests.v2_core.test_v2_shared_market import CH, Cache, Public
from v2_core.producer import RedisMarketContext, market_envelope


def populated():
    cache = Cache()
    for environment in ("LIVE", "SANDBOX"):
        public = Public()
        public.environment = environment
        frame = IncrementalCandles(
            public,
            cache,
            IncrementalArchive(CH(), environment),
            feature_version=STRUCTURE_VERSION,
        ).frame("BTCUSDT", 86400000)
        RedisMarketContext(cache, environment=environment).put(
            market_envelope(
                "s3",
                environment,
                "BTCUSDT",
                frame["observed_at"],
                frame["windows"]["BTCUSDT"],
            )
        )
    return cache


def test_reader_compares_original_frozen_snapshots_without_mutation():
    cache = populated()
    before = deepcopy(cache.data)
    report = collect_comparisons(
        cache, ["BTCUSDT", "牛来USDT"], clock_ms=lambda: 86404000
    )
    assert report["counts"] == {"COMPARABLE": 1, "PARTIAL": 0, "UNKNOWN": 1}
    assert report["rows"][0]["metrics"]["15m"]["close_basis_bps"]["value"] == "0"
    assert report["rows"][1]["sources"]["LIVE"]["reason"] == "MISSING_OBSERVATION"
    assert not report["execution_authorized"] and not report["pnl_evaluated"]
    assert cache.data == before


def test_one_bad_cached_source_does_not_abort_other_symbols_or_leak_payload():
    cache = populated()
    cache.data["v2:market:LIVE:s3:BADUSDT"] = {"payload": "private-error-payload"}
    report = collect_comparisons(
        cache, ["BADUSDT", "BTCUSDT"], clock_ms=lambda: 86404000
    )
    assert report["rows"][0]["source_read_failures"] == ["LIVE"]
    assert report["rows"][0]["status"] == "UNKNOWN"
    assert report["rows"][1]["status"] == "COMPARABLE"
    assert "private-error-payload" not in str(report)


@pytest.mark.parametrize("symbols", [[], ["BTCUSDT"] * 2, ["BTCUSDT"] * 21, "BTCUSDT"])
def test_unbounded_or_ambiguous_requests_rejected_before_cache_reads(symbols):
    with pytest.raises(ValueError):
        collect_comparisons(None, symbols, clock_ms=lambda: 86404000)


def test_read_delay_cannot_refresh_source_timestamps():
    report = collect_comparisons(populated(), ["BTCUSDT"], clock_ms=lambda: 86500000)
    assert report["rows"][0]["status"] == "UNKNOWN"
    assert report["rows"][0]["sources"]["LIVE"]["reason"] == "STALE_OBSERVATION"
