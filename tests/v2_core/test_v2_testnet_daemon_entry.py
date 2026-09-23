import os
import threading
from copy import deepcopy
from pathlib import Path

import pytest
from market_fakes import ArchiveClient, Budget, MarketHTTP
from test_v2_account_inventory import TelegramConnection, sink
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_followup import DirectionalFollowupStage
from services.v2_s0_regime import ArchivedS0Stage
from services.v2_testnet_daemon_entry import (
    BinanceTestnetMark,
    PrivateRatePermit,
    TestnetProcessConfig,
    create_testnet_process,
    daemon_credentials,
)
from v2_core.venue_readiness import GuardedOpeningSubmit

database = database_fixture


def settings(**changes):
    values = {
        "V2_ACCOUNT_ID": "testnet-primary",
        "V2_ENVIRONMENT": "SANDBOX",
        "V2_INTERVAL_SECONDS": "10",
        "V2_ENABLE_ENTRIES": "false",
        "V2_ENABLE_PROTECTION_WRITES": "false",
        "V2_ENABLE_REDUCE_ONLY_EXITS": "false",
        "V2_TESTNET_WRITE_ACK": "",
        "V2_SYMBOLS": "BTCUSDT,ETHUSDT",
        "V2_EXTERNAL_POSITION_EXCLUSIONS": "",
        "V2_EXIT_FEE_RATE": "0.0004",
    }
    values.update(changes)
    return values


def test_process_config_is_explicit_public_and_never_accepts_environment_secrets():
    config = TestnetProcessConfig.from_mapping(settings())
    assert config.symbols == ("BTCUSDT", "ETHUSDT")
    assert config.public_summary() == {
        "account_id": "testnet-primary",
        "environment": "SANDBOX",
        "interval_seconds": 10,
        "enable_entries": False,
        "enable_protection_writes": False,
        "enable_reduce_only_exits": False,
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "external_position_exclusions": [],
        "exit_fee_rate": "0.0004",
        "notifications": True,
    }
    for change in (
        {"BINANCE_TESTNET_API_KEY": "secret"},
        {"V2_SYMBOLS": "BTCUSDT, BTCUSDT"},
        {"V2_SYMBOLS": "BTCUSDT,BTCUSDT"},
        {"V2_SYMBOLS": "../bad"},
        {"V2_EXTERNAL_POSITION_EXCLUSIONS": "BTCUSDT"},
        {"V2_EXTERNAL_POSITION_EXCLUSIONS": "ZORAUSDT, ZORAUSDT"},
        {"V2_EXIT_FEE_RATE": "4e-4"},
    ):
        with pytest.raises(ValueError):
            TestnetProcessConfig.from_mapping(settings(**change))

    excluded = TestnetProcessConfig.from_mapping(
        settings(V2_EXTERNAL_POSITION_EXCLUSIONS="ZORAUSDT")
    )
    assert excluded.external_position_exclusions == ("ZORAUSDT",)


def test_credentials_require_absolute_private_regular_file_and_select_testnet_only(
    tmp_path,
):
    path = tmp_path / "daemon.env"
    path.write_text(
        "BINANCE_API_KEY=live-forbidden\n"
        "BINANCE_TESTNET_API_KEY=test-key\n"
        "BINANCE_TESTNET_API_SECRET=test-secret\n"
        "TG_NOTIFY_TOKEN=12345:abcdefghijklmnop\n"
        "TG_NOTIFY_CHAT_ID=-123\n"
    )
    path.chmod(0o600)
    assert daemon_credentials(path) == {
        "BINANCE_TESTNET_API_KEY": "test-key",
        "BINANCE_TESTNET_API_SECRET": "test-secret",
        "TG_NOTIFY_TOKEN": "12345:abcdefghijklmnop",
        "TG_NOTIFY_CHAT_ID": "-123",
    }
    with pytest.raises(ValueError, match="absolute"):
        daemon_credentials("daemon.env")
    path.chmod(0o640)
    with pytest.raises(ValueError, match="protected"):
        daemon_credentials(path)
    path.chmod(0o600)
    alias = tmp_path / "alias.env"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="protected"):
        daemon_credentials(alias)


def test_private_endpoint_budget_is_chunked_and_write_permissions_are_independent():
    budget = Budget()
    permit = PrivateRatePermit(budget, entries=False, protection=True, exits=False)
    assert permit("GET", "/fapi/v1/openOrders")
    assert budget.weights == [10, 10, 10, 10]
    assert permit("POST", "/fapi/v1/algoOrder")
    assert permit("DELETE", "/fapi/v1/order")
    assert not permit("POST", "/fapi/v1/order")
    assert not permit("GET", "/fapi/v1/leverageBracket")


class Public:
    environment = "SANDBOX"

    def __init__(self, now=10000):
        self.now, self.calls = now, []
        self.change = lambda path, value: value

    def __call__(self, path, params):
        self.calls.append((path, deepcopy(params)))
        value = (
            {"symbol": "BTCUSDT", "markPrice": "100.2500", "time": self.now}
            if path.endswith("premiumIndex")
            else {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"}],
                    }
                ]
            }
        )
        return self.change(path, value)


def test_mark_reference_binds_symbol_tick_and_freshness():
    public = Public()
    provider = BinanceTestnetMark(public, clock_ms=lambda: 10000)
    assert provider("BTCUSDT") == {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "mark_price": "100.25",
        "tick_size": "0.1",
        "observed_at_ms": 10000,
        "source": "binance-public-mark-v1",
    }
    assert public.calls == [
        ("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"}),
        ("/fapi/v1/exchangeInfo", {}),
    ]
    public = Public(now=4999)
    with pytest.raises(ValueError, match="stale"):
        BinanceTestnetMark(public, clock_ms=lambda: 10000)("BTCUSDT")


def test_full_process_factory_wires_real_components_without_io(database):
    redis = pytest.importorskip("redis")
    cache = redis.Redis(
        unix_socket_path=os.environ["V2_REDIS_TEST_SOCKET"], decode_responses=True
    )
    cache.flushdb()
    public_http = MarketHTTP()
    telegram = TelegramConnection()
    config = TestnetProcessConfig.from_mapping(settings())
    process = create_testnet_process(
        config,
        {
            "BINANCE_TESTNET_API_KEY": "test-key",
            "BINANCE_TESTNET_API_SECRET": "test-secret",
            "TG_NOTIFY_TOKEN": "12345:abcdefghijklmnop",
            "TG_NOTIFY_CHAT_ID": "-123",
        },
        connect=database,
        redis_client=cache,
        clickhouse=ArchiveClient(),
        stop=threading.Event(),
        telegram_sink=sink(telegram),
        clock_ms=lambda: 86402001,
        monotonic_ms=lambda: 0,
        signed_connection_factory=lambda *_: pytest.fail("signed I/O during build"),
        public_connection_factory=public_http,
    )
    pipeline = process.daemon.pipeline
    assert isinstance(process.runtime.execution.submit, GuardedOpeningSubmit)
    assert isinstance(pipeline.followups, DirectionalFollowupStage)
    assert isinstance(pipeline.regime, ArchivedS0Stage)
    assert [item.worker.scope.producer for item in pipeline.schedulers] == ["s6", "s8"]
    assert pipeline.enable_entries is False
    assert pipeline.protection.allow_writes is False
    assert pipeline.exits.allow_writes is False
    with pytest.raises(ValueError, match="MISSING_CONTEXT"):
        pipeline.schedulers[0].context_provider(
            {"symbol": "BTCUSDT", "signal": "TREND_UP", "features": {}}
        )
    assert public_http.calls == [] and telegram.calls == []
    cache.close()


def test_systemd_template_is_hardened_unrendered_and_defaults_to_no_writes():
    root = Path(__file__).resolve().parents[2]
    unit = (root / "deploy/v2-testnet/trade-v2-testnet-daemon.service.in").read_text()
    environment = (
        root / "deploy/v2-testnet/trade-v2-testnet-daemon.env.example"
    ).read_text()
    for placeholder in (
        "@PROJECT_ROOT@",
        "@PYTHON@",
        "@ENV_FILE@",
        "@CREDENTIAL_FILE@",
    ):
        assert placeholder in unit
    for directive in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "PrivateTmp=yes",
        "CapabilityBoundingSet=",
        "TimeoutStopSec=30",
        "KillSignal=SIGTERM",
        "ExecStartPre=@PYTHON@ -m services.v2_dependency_preflight",
    ):
        assert directive in unit
    for dependency in (
        "postgresql@16-tradev2.service",
        "trade-v2-cache.service",
        "trade-v2-clickhouse.service",
    ):
        assert dependency in unit
    for name, family in (
        ("trade-v2-cache.service.in", "RestrictAddressFamilies=AF_UNIX"),
        (
            "trade-v2-clickhouse.service.in",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        ),
    ):
        dependency = (root / "deploy/v2-testnet" / name).read_text()
        assert "@PROJECT_ROOT@" in dependency
        assert "ProtectSystem=strict" in dependency
        assert "NoNewPrivileges=yes" in dependency
        assert (
            "ReadWritePaths=/var/lib/trade-engine-v2 /var/log/trade-engine-v2"
            in dependency
        )
        assert family in dependency
    cache_unit = (root / "deploy/v2-testnet/trade-v2-cache.service.in").read_text()
    assert "Type=notify" in cache_unit and "--supervised systemd" in cache_unit
    assert "BINANCE_TESTNET_API_KEY=" not in environment
    values = dict(
        line.split("=", 1)
        for line in environment.splitlines()
        if line and not line.startswith("#")
    )
    config = TestnetProcessConfig.from_mapping(values)
    assert config.daemon.write_enabled is False
