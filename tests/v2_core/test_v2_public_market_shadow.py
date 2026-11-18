"""LIVE public observations cannot become SANDBOX candles or order signals."""

from copy import deepcopy

import pytest

from services.v2_market_hub_entry import create_hub
from services.v2_s3_candles import STRUCTURE_VERSION
from services.v2_shared_market import (
    IncrementalArchive,
    IncrementalCandles,
    SharedMarketHub,
)
from tests.v2_core.test_v2_shared_market import (
    CH,
    Cache,
    Public,
)
from tests.v2_core.test_v2_shared_market import (
    database as database_fixture,
)
from tests.v2_core.test_v2_shared_market import (
    shared as shared_fixture,
)
from v2_core.producer import RedisMarketContext

database = database_fixture
shared = shared_fixture


def live_public(names=("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")):
    public = Public(names)
    public.environment = "LIVE"
    return public


def hub(connect, public, cache, ch, **kwargs):
    return SharedMarketHub(
        connect,
        transport=public,
        cache=cache,
        archive=IncrementalArchive(ch, public.environment),
        clock_ms=lambda: public.now,
        monotonic_ms=lambda: 0,
        feature_version=STRUCTURE_VERSION,
        **kwargs,
    )


@pytest.mark.parametrize(
    "source,partition", [("LIVE", "SANDBOX"), ("SANDBOX", "LIVE"), ("UNKNOWN", "LIVE")]
)
def test_candle_source_cannot_poison_another_archive_partition(source, partition):
    public, ch, cache = Public(), CH(), Cache()
    public.environment = source
    with pytest.raises(ValueError, match="CANDLE_SOURCE_ARCHIVE_SCOPE_MISMATCH"):
        IncrementalCandles(public, cache, IncrementalArchive(ch, partition))
    assert not public.calls and not ch.rows and not cache.data


@pytest.mark.parametrize(
    "changes",
    [
        {"publish_signals": "false"},
        {"publish_signals": 0},
        {"publish_signals": False, "quant_research": object()},
        {"publish_signals": True, "priority_environment": "SANDBOX"},
        {"publish_signals": False, "priority_environment": "UNKNOWN"},
    ],
)
def test_shadow_rejects_ambiguous_modes_before_io(changes):
    public = live_public()
    with pytest.raises(ValueError, match="MARKET_PUBLIC_SHADOW_BINDING_INVALID"):
        hub(lambda: pytest.fail("no database access"), public, Cache(), CH(), **changes)
    assert not public.calls


def test_shadow_mode_cannot_silently_disable_existing_testnet_signals():
    with pytest.raises(ValueError, match="MARKET_PUBLIC_SHADOW_BINDING_INVALID"):
        hub(None, Public(), Cache(), CH(), publish_signals=False)


def test_factory_keeps_live_observation_and_testnet_execution_separate():
    connect = lambda: pytest.fail("factory performs no I/O")
    shadow = create_hub(
        connect, Cache(), CH(), feature_version=STRUCTURE_VERSION, public_shadow=True
    )
    assert (
        shadow.environment
        == shadow.transport.budget.environment
        == shadow.candles.archive.environment
        == "LIVE"
    )
    assert shadow.transport.budget.host == "fapi.binance.com"
    assert shadow.transport.budget.lane == "market"
    assert shadow.priority_environment == "SANDBOX"
    assert shadow.publisher is shadow.regime is shadow.quant_research is None
    normal = create_hub(connect, Cache(), CH(), feature_version=STRUCTURE_VERSION)
    assert (
        normal.environment
        == normal.priority_environment
        == normal.transport.budget.environment
        == "SANDBOX"
    )
    assert normal.publish_signals and normal.publisher is not None
    with pytest.raises(ValueError, match="MODE_INVALID"):
        create_hub(
            connect,
            Cache(),
            CH(),
            feature_version=STRUCTURE_VERSION,
            public_shadow="false",
        )


def test_shadow_startup_requires_explicit_feature_version_before_database_access(
    monkeypatch,
):
    from services.v2_market_hub_entry import main

    monkeypatch.delenv("V2_MARKET_FEATURE_VERSION", raising=False)
    monkeypatch.setattr(
        "services.v2_market_hub_entry.deployment_database",
        lambda: pytest.fail("no I/O before feature selection"),
    )
    with pytest.raises(ValueError, match="FEATURE_VERSION_REQUIRED"):
        main(public_shadow=True)


def test_live_shadow_collects_without_any_signal_or_lifecycle_and_preserves_sandbox(
    shared, monkeypatch
):
    cache, ch = Cache(), CH()
    normal = hub(shared, Public(), cache, ch)
    normal.run_once()
    sandbox_cache = deepcopy(cache.data)
    sandbox_rows = deepcopy(ch.rows)
    with shared() as c:
        before = c.execute(
            "SELECT (SELECT count(*) FROM v2_inbound_signals),"
            "(SELECT count(*) FROM v2_s3_frames),(SELECT count(*) FROM v2_producer_batches),"
            "(SELECT count(*) FROM v2_trade_intents),(SELECT count(*) FROM v2_business_state)"
        ).fetchone()
        sandbox_status = c.execute(
            "SELECT observed_at_ms,payload FROM v2_market_hub_status WHERE environment='SANDBOX'"
        ).fetchone()
        c.execute(
            "INSERT INTO v2_market_demands VALUES ('SANDBOX','LIVEONLYUSDT',999999999)"
        )

    def forbidden(*_, **__):
        pytest.fail("shadow must never create a publisher or run signal lifecycle")

    monkeypatch.setattr("services.v2_shared_market.ProducerPublisher", forbidden)
    monkeypatch.setattr("services.v2_shared_market.S3Runtime", forbidden)
    public = live_public(("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LIVEONLYUSDT"))
    shadow = hub(
        shared, public, cache, ch, publish_signals=False, priority_environment="SANDBOX"
    )
    assert "LIVEONLYUSDT" in shadow._priorities(public.now)
    report = shadow.run_once()
    assert report["status"] == "CURRENT" and report["fresh"] == 5
    assert (
        report["publication_mode"] == "PUBLIC_SHADOW"
        and report["publishes_signals"] is False
    )
    assert (
        report["market_environment"] == "LIVE"
        and report["priority_environment"] == "SANDBOX"
    )
    assert {k: v for k, v in cache.data.items() if ":SANDBOX:" in k} == sandbox_cache
    assert [row for row in ch.rows if row[0] == "SANDBOX"] == sandbox_rows
    context = RedisMarketContext(cache, environment="LIVE").read(
        "s3", "LIVE", "BTCUSDT"
    )
    assert context["features"]["input_evidence"]["environment"] == "LIVE"
    assert context["features"]["input_evidence"]["window_version"] == STRUCTURE_VERSION
    with shared() as c:
        after = c.execute(
            "SELECT (SELECT count(*) FROM v2_inbound_signals),"
            "(SELECT count(*) FROM v2_s3_frames),(SELECT count(*) FROM v2_producer_batches),"
            "(SELECT count(*) FROM v2_trade_intents),(SELECT count(*) FROM v2_business_state)"
        ).fetchone()
        assert after == before
        assert (
            c.execute(
                "SELECT observed_at_ms,payload FROM v2_market_hub_status WHERE environment='SANDBOX'"
            ).fetchone()
            == sandbox_status
        )
        assert c.execute(
            "SELECT contracts ? 'LIVEONLYUSDT' FROM v2_market_catalog WHERE environment='SANDBOX'"
        ).fetchone() == (False,)
        assert c.execute(
            "SELECT contracts ? 'LIVEONLYUSDT' FROM v2_market_catalog WHERE environment='LIVE'"
        ).fetchone() == (True,)
    # Cache loss recovers the original source partition without rereading candles.
    original = deepcopy(context)
    cache.data = {k: v for k, v in cache.data.items() if ":LIVE:" not in k}
    rebuilt = IncrementalCandles(
        public, cache, IncrementalArchive(ch, "LIVE"), feature_version=STRUCTURE_VERSION
    ).frame("BTCUSDT", context["observed_at"])
    assert rebuilt["windows"]["BTCUSDT"] == original["features"]
    assert (
        len(
            [
                p
                for path, p in public.calls
                if path.endswith("/klines") and p["symbol"] == "BTCUSDT"
            ]
        )
        == 1
    )


def test_shadow_symbol_failure_is_visible_without_demo_fallback(shared):
    public = live_public(("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "BADUSDT"))
    public.failed = {"BADUSDT"}
    cache, ch = Cache(), CH()
    shadow = hub(shared, public, cache, ch, publish_signals=False)
    report = shadow.run_once()
    assert report["fresh"] == 4
    assert any(row.get("reason") == "MARKET_TIMEOUT" for row in report["results"])
    assert all(row[0] == "LIVE" for row in ch.rows)
    assert all(":LIVE:" in key for key in cache.data)
    with shared() as c:
        assert c.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM v2_s3_frames").fetchone() == (0,)


def test_shadow_archive_failure_cannot_publish_context_or_signal(shared, monkeypatch):
    cache, public = Cache(), live_public()
    shadow = hub(shared, public, cache, CH(), publish_signals=False)
    monkeypatch.setattr(
        shadow.candles.archive,
        "put_many",
        lambda *_: (_ for _ in ()).throw(ConnectionError()),
    )
    with pytest.raises(ConnectionError):
        shadow.run_once()
    assert not cache.data
    with shared() as c:
        assert c.execute("SELECT count(*) FROM v2_market_hub_status").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM v2_inbound_signals").fetchone() == (0,)


def test_shadow_uses_shared_live_host_budget_and_respects_penalty(shared):
    from v2_core.shared_market import HostBudget

    budget = HostBudget(shared, environment="LIVE", lane="market")
    assert budget.permit(1)
    budget.penalize(60)
    shadow = create_hub(
        shared, Cache(), CH(), feature_version=STRUCTURE_VERSION, public_shadow=True
    )
    with pytest.raises(RuntimeError, match="QUOTA_DENIED"):
        shadow.transport("/fapi/v1/exchangeInfo", {})
    # An exchange-level LIVE cooldown never relabels or penalizes Testnet.
    assert HostBudget(shared, environment="SANDBOX", lane="market").permit(1)


@pytest.mark.parametrize("delta", [-1, 90000, 90001])
def test_shadow_cannot_project_future_or_expired_observations(delta):
    public = live_public()
    cache = Cache()
    shadow = hub(None, public, cache, CH(), publish_signals=False)
    public.now = 86400000 + delta
    with pytest.raises(ValueError, match="STALE_OR_FUTURE"):
        shadow._project_shadow("s0", "*", 86400000, {"regime": "neutral"})
    assert not cache.data


def test_slow_archive_never_refreshes_observation_time(shared, monkeypatch):
    public, cache, ch = live_public(), Cache(), CH()
    shadow = hub(shared, public, cache, ch, publish_signals=False)
    put_many = shadow.candles.archive.put_many

    def delayed(groups):
        put_many(groups)
        public.now += 90000

    monkeypatch.setattr(shadow.candles.archive, "put_many", delayed)
    report = shadow.run_once()
    assert report["status"] == "WARMING" and report["fresh"] == 0
    assert all(
        row["reason"] == "MARKET_PUBLIC_SHADOW_STALE_OR_FUTURE"
        for row in report["results"]
    )
    assert not any(key.startswith("v2:market:") for key in cache.data)
    # Archived history remains available for retrospective analysis, not entry.
    assert len(ch.rows) == 4 * 1440
