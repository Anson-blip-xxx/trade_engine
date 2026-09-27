import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from market_fakes import Budget, MarketHTTP
from test_v2_directional_replay import scenario
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_admission import create_directional_admission_worker
from services.v2_shared_market import (
    IncrementalArchive,
    IncrementalCandles,
    SharedMarketHub,
    select_batch,
)
from services.v2_shared_market_context import DynamicMarketContext
from services.v2_testnet_daemon_entry import PrivateRatePermit, TestnetProcessConfig
from v2_core.evidence import canonical, digest
from v2_core.public_market import BinancePublicMarket
from v2_core.shared_market import Catalog, HostBudget, MarketPolicy, contracts
from v2_core.state import BusinessState, StateKey

database = database_fixture


@pytest.fixture
def shared(database):
    with database() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260929_shared_market.sql"
            ).read_text()
        )
    return database


def contract(target="BTCUSDT", **changes):
    return dict(
        symbol=target,
        status="TRADING",
        contractType="PERPETUAL",
        quoteAsset="USDT",
        filters=[{"filterType": "PRICE_FILTER", "tickSize": "0.01"}],
        **changes,
    )


def catalog_response(names):
    return {"symbols": [contract(s) for s in names]}


class Cache:
    def __init__(self):
        self.data = {}

    def hget(self, key, field):
        return self.data.get(key, {}).get(field)

    def expire(self, *args):
        return True

    def eval(self, _script, _keys, key, version, encoded, fingerprint):
        row = self.data.get(key)
        if row:
            if int(row["version"]) > int(version):
                return 1
            if row["version"] == version:
                return int(row["digest"] == fingerprint)
        self.data[key] = {"version": version, "payload": encoded, "digest": fingerprint}
        return 1


class CH:
    def __init__(self):
        self.rows = []
        self.inserts = []

    def query(self, _sql, parameters):
        p = parameters
        return SimpleNamespace(
            result_rows=[
                (t, payload, sha)
                for env, s, t, payload, sha in self.rows
                if env == p["environment"]
                and s == p["symbol"]
                and p["start"] <= t < p["end"]
            ]
        )

    def insert(self, _table, rows, column_names):
        self.rows.extend(rows)
        self.inserts.append(rows)


class Public:
    environment = "SANDBOX"

    def __init__(self, names=("BTCUSDT",), now=86404000):
        self.names, self.now, self.calls = names, now, []
        self.failed = set()

    def __call__(self, path, p):
        self.calls.append((path, dict(p)))
        if path.endswith("/time"):
            return {"serverTime": self.now}
        if path.endswith("/exchangeInfo"):
            return catalog_response(self.names)
        if path.endswith("/24hr"):
            return [{"symbol": s, "priceChangePercent": "1"} for s in self.names]
        assert path.endswith("/klines")
        if p["symbol"] in self.failed:
            raise TimeoutError("MARKET_TIMEOUT")
        return [
            [t, "100", "102", "99", "101", "10", t + 59999, "0", 1, "6", "0", "0"]
            for t in range(p["startTime"], p["endTime"] + 1, 60000)
        ]


@pytest.mark.parametrize(
    "changes",
    [
        {"batch_symbols": True},
        {"background_slots": 16},
        {"market_weight_limit": 1800},
        {"regime_symbols": ("ETHUSDT",)},
        {"catalog_seconds": 900},
    ],
)
def test_policy_rejects_unsafe_bounds(changes):
    with pytest.raises(ValueError):
        MarketPolicy(**changes)


def test_catalog_filters_contract_types_and_reports_unsupported_format():
    response = catalog_response(["BTCUSDT", "测试USDT"])
    response["symbols"] += [
        dict(contract("ETHBTC"), quoteAsset="BTC"),
        dict(contract("BTCUSDT_270326"), contractType="NEXT_QUARTER"),
    ]
    found = contracts(response)
    assert set(found) == {"BTCUSDT", "测试USDT"}
    assert found["BTCUSDT"]["supported"] and not found["测试USDT"]["supported"]
    with pytest.raises(ValueError, match="DUPLICATE"):
        contracts(catalog_response(["BTCUSDT", "BTCUSDT"]))


def test_catalog_freshness_and_digest_fail_closed(shared):
    clock = [100000]
    catalog = Catalog(shared, "SANDBOX", lambda: clock[0])
    catalog.save(catalog_response(["BTCUSDT"]))
    assert catalog.read(1000)["BTCUSDT"]["supported"]
    clock[0] += 1001
    with pytest.raises(ValueError, match="STALE"):
        catalog.read(1000)
    clock[0] = 99999
    with pytest.raises(ValueError, match="STALE"):
        catalog.read(1000)
    clock[0] = 100000
    with shared() as c:
        c.execute("UPDATE v2_market_catalog SET digest='wrong'")
    with pytest.raises(ValueError, match="CORRUPT"):
        catalog.read(1000)


def test_host_budget_atomic_across_accounts_and_preserves_management_reserve(shared):
    policy = MarketPolicy(shared_weight_limit=120, market_weight_limit=60)
    key = StateKey(
        "BINANCE",
        "market-data",
        "SANDBOX",
        "FUTURES",
        "shared-market-policy",
        "current",
    )
    BusinessState(shared).change(
        key,
        expected_version=0,
        request_key="qa-policy",
        payload=policy.values(),
        reason="QA",
    )

    def reserve(_):
        return HostBudget(shared, environment="SANDBOX", lane="market").permit(10)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(20)))
    assert sum(results) == 6
    manager = HostBudget(shared, environment="SANDBOX")
    assert manager.permit(60) and not manager.permit(1)
    manager.penalize(60)
    assert not HostBudget(shared, environment="SANDBOX", lane="market").permit(1)
    assert HostBudget(shared, environment="LIVE").permit(1)


@pytest.mark.parametrize(
    "limit,weight",
    [(1, 1), (99, 1), (100, 2), (499, 2), (500, 5), (1000, 5), (1001, 10), (1440, 10)],
)
def test_incremental_transport_exact_weights(limit, weight):
    budget = Budget()
    http = MarketHTTP(raw=b"[]")
    transport = BinancePublicMarket(
        environment="SANDBOX",
        budget=budget,
        enabled=True,
        incremental=True,
        connection_factory=http,
    )
    transport(
        "/fapi/v1/klines",
        {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "startTime": 86400000,
            "endTime": 86400000 + limit * 60000 - 1,
            "limit": limit,
        },
    )
    assert budget.weights == [weight]
    transport("/fapi/v1/ticker/24hr", {})
    assert budget.weights[-1] == 40


def test_incremental_history_writes_only_new_bars_and_recovers_after_cache_loss():
    ch = CH()
    cache = Cache()
    public = Public()
    collector = IncrementalCandles(public, cache, IncrementalArchive(ch, "SANDBOX"))
    first = collector.frame("BTCUSDT", 86400000)
    again = collector.frame("BTCUSDT", 86400000)
    assert first == again and len(ch.rows) == 1440 and len(public.calls) == 1
    later = collector.frame("BTCUSDT", 86460000)
    assert len(ch.rows) == 1441 and public.calls[-1][1]["limit"] == 1
    cache.data.clear()
    assert collector.frame("BTCUSDT", 86460000) == later
    assert len(public.calls) == 2 and len(ch.rows) == 1441
    with pytest.raises(ValueError, match="REGRESSION"):
        collector.frame("BTCUSDT", 86400000)


def test_archive_rejects_conflicting_duplicate_even_after_merge():
    ch = CH()
    archive = IncrementalArchive(ch, "SANDBOX")
    cache = Cache()
    IncrementalCandles(Public(), cache, archive).frame("BTCUSDT", 86400000)
    env, s, t, payload, _sha = ch.rows[0]
    changed = json.loads(payload)
    changed["c"] = "100"
    encoded = canonical(changed)
    ch.rows.append((env, s, t, encoded, digest(encoded)))
    with pytest.raises(ValueError, match="CONFLICT"):
        archive.read("BTCUSDT", 0, 86400000)


def test_archive_failure_never_publishes_cache(monkeypatch):
    cache = Cache()
    archive = IncrementalArchive(CH(), "SANDBOX")
    monkeypatch.setattr(
        archive, "put", lambda *_: (_ for _ in ()).throw(ConnectionError())
    )
    with pytest.raises(ConnectionError):
        IncrementalCandles(Public(), cache, archive).frame("BTCUSDT", 86400000)
    assert cache.data == {}


def test_full_market_batching_does_not_whitelist_or_starve_background():
    names = [f"ASSET{i}USDT" for i in range(600)] + ["BTCUSDT", "TVUSDT"]
    policy = MarketPolicy()
    heads = {}
    for step in range(160):
        batch = select_batch(
            names,
            heads,
            observed=86400000,
            now=100000000 + step * 60000,
            policy=policy,
            urgent=["TVUSDT"],
            movers=names[-30:],
        )
        assert len(batch) <= policy.batch_symbols
        for s in batch:
            heads[s] = {"observed": 86400000, "attempted": 100000000 + step * 60000}
    assert set(heads) == set(names)


def test_hub_isolates_failure_and_publishes_same_minute_for_multiple_symbols(shared):
    names = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "TVUSDT", "BROKENUSDT")
    public = Public(names)
    public.failed = {"BROKENUSDT"}
    hub = SharedMarketHub(
        shared,
        transport=public,
        cache=Cache(),
        archive=IncrementalArchive(CH(), "SANDBOX"),
        clock_ms=lambda: public.now,
        monotonic_ms=lambda: 0,
    )
    result = hub.run_once()
    assert (
        result["universe"] == 6
        and result["fresh"] == 5
        and result["regime"] == "PROJECTED"
    )
    assert any(r.get("reason") == "MARKET_TIMEOUT" for r in result["results"])
    with shared() as c:
        assert c.execute("SELECT count(*) FROM v2_s3_frames").fetchone()[0] == 5
        assert c.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
    assert hub.run_once()["fresh"] == 5
    public.now += 60000
    assert hub.run_once()["fresh"] == 5
    requests = [
        p["limit"]
        for path, p in public.calls
        if path.endswith("/klines") and p["symbol"] == "TVUSDT"
    ]
    assert requests == [1440, 1]


def test_dynamic_tv_context_requests_any_listed_symbol_and_links_internal_signal(
    shared,
):
    Catalog(shared, "SANDBOX", lambda: 3).save(catalog_response(["BTCUSDT", "NEWUSDT"]))
    _, _runtime, internal, context = scenario(shared)
    signal = {
        "symbol": "BTCUSDT",
        "observed_at": 2,
        "expires_at_ms": 10,
        "signal": "TREND_UP",
        "features": {},
    }
    provider = DynamicMarketContext(
        shared,
        delegate=lambda _: deepcopy(context),
        clock_ms=lambda: 3,
        source="tv_bridge",
    )
    observed = provider(signal)
    assert observed["universe_admission"]["eligible"]
    assert observed["signal_fusion"]["peers"][0]["signal_id"] == internal
    assert observed["signal_fusion"]["peers"][0]["relationship"] == "CONFIRMS"
    assert provider(dict(signal, symbol="NEWUSDT"))["universe_admission"]["eligible"]
    assert not provider(dict(signal, symbol="DELISTEDUSDT"))["universe_admission"][
        "eligible"
    ]
    with shared() as c:
        assert c.execute("SELECT count(*) FROM v2_market_demands").fetchone()[0] == 2


def test_cross_source_opening_is_fused_once_and_survives_retry(database):
    _, runtime, internal, context = scenario(database)
    runtime.execution.risk_reference = lambda _: pytest.fail("no exchange calls")
    context["universe_admission"] = {
        "eligible": True,
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
    }
    context["signal_fusion"] = {"window_ms": 60000}
    tv = runtime.data.signals.admit(
        source="tv_bridge",
        environment="SANDBOX",
        request_key="tv-fusion",
        snapshot={
            "symbol": "BTCUSDT",
            "observed_at": 2,
            "expires_at_ms": 10,
            "signal": "TREND_UP",
            "features": {"strength": 70, "price": "100"},
        },
    )

    def create(source):
        return create_directional_admission_worker(
            runtime,
            strategy="S6",
            analysis_mode="hard",
            max_delay_ms=5,
            enable_admission=True,
            source=source,
            dynamic_universe=True,
            tv_start_ms=1,
        )

    first = create("tv_bridge")
    second = create("s3")
    assert first.consume(tv, context=context)["status"] == "PREPARED"
    outcome = second.consume(internal, context=context)
    assert (
        outcome["status"] == "IGNORED"
        and outcome["reason"] == "CORRELATED_SIGNAL_ALREADY_ADMITTED"
    )
    assert second.consume(internal, context={})["reason"] == outcome["reason"]
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 1


def test_dynamic_tv_unknown_contract_ends_with_explicit_receipt(database):
    _, runtime, _, _ = scenario(database)
    runtime.execution.risk_reference = lambda _: None
    tv = runtime.data.signals.admit(
        source="tv_bridge",
        environment="SANDBOX",
        request_key="tv-delisted",
        snapshot={
            "symbol": "OLDUSDT",
            "observed_at": 2,
            "expires_at_ms": 10,
            "signal": "TREND_UP",
            "features": {},
        },
    )
    worker = create_directional_admission_worker(
        runtime,
        strategy="S6",
        analysis_mode="hard",
        max_delay_ms=5,
        enable_admission=True,
        source="tv_bridge",
        dynamic_universe=True,
        tv_start_ms=1,
    )
    outcome = worker.consume(
        tv,
        context={
            "universe_admission": {
                "eligible": False,
                "environment": "SANDBOX",
                "symbol": "OLDUSDT",
            }
        },
    )
    assert outcome["status"] == "IGNORED" and outcome["reason"] == "SYMBOL_NOT_TRADING"


def test_shared_process_has_no_legacy_scan_list_requirement():
    from test_v2_testnet_daemon_entry import settings

    config = settings()
    config.pop("V2_SYMBOLS")
    config["V2_SHARED_MARKET"] = "true"
    assert TestnetProcessConfig.from_mapping(config).shared_market


def test_concurrent_tv_and_internal_decisions_cannot_both_admit(database):
    _, runtime, internal, context = scenario(database)
    runtime.execution.risk_reference = lambda _: pytest.fail("no exchange calls")
    context["universe_admission"] = {
        "eligible": True,
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
    }
    context["signal_fusion"] = {"window_ms": 60000}
    tv = runtime.data.signals.admit(
        source="tv_bridge",
        environment="SANDBOX",
        request_key="tv-concurrent",
        snapshot={
            "symbol": "BTCUSDT",
            "observed_at": 2,
            "expires_at_ms": 10,
            "signal": "TREND_UP",
            "features": {"strength": 70, "price": "100"},
        },
    )
    workers = [
        create_directional_admission_worker(
            runtime,
            strategy="S6",
            analysis_mode="hard",
            max_delay_ms=5,
            enable_admission=True,
            source=source,
            dynamic_universe=True,
            tv_start_ms=1,
        )
        for source in ("s3", "tv_bridge")
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker.consume, signal_id, context=deepcopy(context))
            for worker, signal_id in zip(workers, [internal, tv], strict=True)
        ]
        results = [f.result()["status"] for f in futures]
    assert sorted(results) == ["IGNORED", "PREPARED"]
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 1


def test_dynamic_context_never_extends_tv_expiration(shared):
    Catalog(shared, "SANDBOX", lambda: 100).save(catalog_response(["BTCUSDT"]))
    provider = DynamicMarketContext(
        shared,
        delegate=lambda _: pytest.fail("expired request must not fetch account"),
        clock_ms=lambda: 100,
        source="tv_bridge",
    )
    with pytest.raises(ValueError, match="EXPIRED"):
        provider(
            {
                "symbol": "BTCUSDT",
                "observed_at": 1,
                "expires_at_ms": 100,
                "signal": "TREND_UP",
                "features": {},
            }
        )


def test_shared_risk_reference_uses_verified_per_symbol_archive_not_legacy_head(shared):
    from dataclasses import asdict

    from test_v2_directional_replay import SCOPE

    from services.v2_shared_risk_reference import SharedRiskReference

    public = Public(("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"))
    archive = IncrementalArchive(CH(), "SANDBOX")
    hub = SharedMarketHub(
        shared,
        transport=public,
        cache=Cache(),
        archive=archive,
        clock_ms=lambda: public.now,
        monotonic_ms=lambda: 0,
    )
    assert hub.run_once()["fresh"] == 4
    reference = SharedRiskReference(
        shared, scope=SCOPE, archive=archive, clock_ms=lambda: public.now
    )
    request = {**asdict(SCOPE), "leg": "OPEN", "symbol": "ETHUSDT"}
    value = reference(request)
    assert value.symbol == "ETHUSDT" and value.price == "101"
    assert value.provenance.frame_id.startswith("s3-shared-v1:ETHUSDT:")
    with shared() as c:
        assert c.execute("SELECT count(*) FROM v2_candle_deliveries").fetchone()[0] == 0
    public.now += 90000
    with pytest.raises(ValueError, match="STALE"):
        reference(request)
    with pytest.raises(ValueError, match="SCOPE"):
        reference({**request, "environment": "LIVE"})


def test_shared_worker_composes_all_execution_gates_without_network(shared):
    import threading

    from market_fakes import ArchiveClient
    from test_v2_account_inventory import TelegramConnection, sink
    from test_v2_testnet_daemon_entry import settings

    from services.v2_shared_market_context import SharedMarketStage, SharedRegimeStage
    from services.v2_shared_risk_reference import SharedRiskReference
    from services.v2_testnet_daemon_entry import create_testnet_process

    config = TestnetProcessConfig.from_mapping(
        settings(
            V2_SHARED_MARKET="true",
            V2_ENABLE_TV_SIGNALS="true",
            V2_TV_ADMISSION_START_MS="1",
        )
    )
    no_network = lambda *_args, **_kwargs: pytest.fail(
        "composition must not perform network requests"
    )
    process = create_testnet_process(
        config,
        {
            "BINANCE_TESTNET_API_KEY": "dummy-key",
            "BINANCE_TESTNET_API_SECRET": "dummy-secret",
            "TG_NOTIFY_TOKEN": "12345:abcdefghijklmnop",
            "TG_NOTIFY_CHAT_ID": "-123",
        },
        connect=shared,
        redis_client=Cache(),
        clickhouse=ArchiveClient(),
        stop=threading.Event(),
        telegram_sink=sink(TelegramConnection()),
        clock_ms=lambda: 86404000,
        monotonic_ms=lambda: 0,
        signed_connection_factory=no_network,
        public_connection_factory=no_network,
    )
    pipeline = process.daemon.pipeline
    assert isinstance(pipeline.market, SharedMarketStage) and isinstance(
        pipeline.regime, SharedRegimeStage
    )
    assert isinstance(process.runtime.execution.risk_reference, SharedRiskReference)
    assert (
        not pipeline.enable_entries
        and not pipeline.protection.allow_writes
        and not pipeline.exits.allow_writes
    )
    assert len(pipeline.schedulers) == 4
    assert all(
        json.loads(s.worker.config_json)["dynamic_universe"]
        for s in pipeline.schedulers
    )


@pytest.mark.parametrize("status,seconds", [(429, 60), (418, 86400)])
def test_private_rate_limit_blocks_shared_bucket_before_response_parsing(
    status, seconds
):
    from test_v2_transport import Connection

    from v2_core.transport import BinanceSignedTransport, ExchangeTransportError

    conn = Connection()
    conn.status = status
    conn.raw = b"not-json"
    budget = Budget()
    permit = PrivateRatePermit(budget, entries=False, protection=False, exits=False)
    transport = BinanceSignedTransport(
        account_id="qa",
        environment="SANDBOX",
        api_key="dummy-key",
        api_secret="dummy-secret",
        clock_ms=lambda: 1000,
        permit=permit,
        connection_factory=lambda *_args, **_kwargs: conn,
    )
    with pytest.raises(ExchangeTransportError):
        transport("GET", "/fapi/v3/account", {})
    assert budget.penalties == [seconds]
