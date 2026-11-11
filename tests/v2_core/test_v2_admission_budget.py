"""Isolated quotas: candidate reads cannot exhaust position-management reserve."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from market_fakes import ArchiveClient, MarketHTTP
from test_v2_account_inventory import TelegramConnection, sink
from test_v2_directional_admission import formal
from test_v2_directional_replay import scenario
from test_v2_shared_market import shared as shared_fixture
from test_v2_testnet_daemon_entry import settings

from services.v2_testnet_daemon_entry import (
    TestnetProcessConfig,
    create_testnet_process,
)
from tests.support.v2_admission import database as database_fixture
from v2_core.scheduling import StrategyScheduler
from v2_core.shared_market import AdmissionBudget, HostBudget, MarketPolicy
from v2_core.state import BusinessState, StateKey
from v2_core.transport import ExchangeTransportError

database = database_fixture
shared = shared_fixture


def small_policy(connect):
    BusinessState(connect).change(
        StateKey(
            "BINANCE",
            "market-data",
            "SANDBOX",
            "FUTURES",
            "shared-market-policy",
            "current",
        ),
        expected_version=0,
        request_key="qa-small-budget",
        reason="QA",
        payload=MarketPolicy(shared_weight_limit=120, market_weight_limit=60).values(),
    )


def test_market_and_admission_share_atomic_noncritical_cap_across_instances(shared):
    small_policy(shared)

    def permit(index):
        return HostBudget(
            shared,
            environment="SANDBOX",
            lane="market" if index % 2 else "admission",
        ).permit(10)

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(permit, range(30))) == 6
    with shared() as conn:
        assert conn.execute(
            "SELECT used_weight,market_weight FROM v2_egress_budgets WHERE host='demo-fapi.binance.com'"
        ).fetchone() == (60, 60)
    # Admission exhaustion does not exhaust the management half or another host.
    with pytest.raises(ValueError, match="^ADMISSION_BUDGET_UNAVAILABLE$"):
        AdmissionBudget(shared, environment="SANDBOX").permit(1)
    manager = HostBudget(shared, environment="SANDBOX")
    assert manager.permit(60)
    assert not manager.permit(1)
    assert AdmissionBudget(shared, environment="LIVE").permit(1)


def test_budget_rollover_and_host_penalty_apply_to_every_lane(shared):
    small_policy(shared)
    admission = AdmissionBudget(shared, environment="SANDBOX")
    assert admission.permit(60)
    with shared() as conn:
        conn.execute("UPDATE v2_egress_budgets SET window_id=window_id-1")
    assert admission.permit(60)
    HostBudget(shared, environment="SANDBOX").penalize(60)
    with pytest.raises(ValueError, match="^ADMISSION_BUDGET_UNAVAILABLE$"):
        admission.permit(1)
    assert not HostBudget(shared, environment="SANDBOX").permit(1)
    assert not HostBudget(shared, environment="SANDBOX", lane="market").permit(1)


def test_only_local_denial_is_an_expected_wait_not_database_failure(monkeypatch):
    def failed(_self, _weight):
        raise ConnectionError("QA database unavailable")

    monkeypatch.setattr(HostBudget, "permit", failed)
    with pytest.raises(ConnectionError):
        AdmissionBudget(lambda: None, environment="SANDBOX").permit(5)


def test_scheduler_quota_wait_is_not_global_failure_or_fake_decision(database):
    _, runtime, signal, _ = scenario(database)
    worker = formal(runtime, "S6")

    def unavailable(_):
        raise ValueError("ADMISSION_BUDGET_UNAVAILABLE")

    scheduler = StrategyScheduler(worker, context_provider=unavailable)
    result = scheduler.run_once(limit=1)
    assert result[signal] == "DEFERRED"
    assert worker.decision(signal) is None
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            0,
        )
        assert conn.execute(
            "SELECT error_code,completed_at FROM v2_strategy_tasks WHERE signal_id=%s",
            (signal,),
        ).fetchone() == ("ADMISSION_BUDGET_UNAVAILABLE", None)


@pytest.mark.parametrize("managed", [False, True])
def test_shared_factory_separates_all_admission_io_from_management(shared, managed):
    redis = pytest.importorskip("redis")
    cache = redis.Redis(
        unix_socket_path=os.environ["V2_REDIS_TEST_SOCKET"], decode_responses=True
    )
    public_http = MarketHTTP()
    process = create_testnet_process(
        TestnetProcessConfig.from_mapping(settings(V2_SHARED_MARKET="true")),
        {
            "BINANCE_TESTNET_API_KEY": "test-key",
            "BINANCE_TESTNET_API_SECRET": "test-secret",
            "TG_NOTIFY_TOKEN": "12345:abcdefghijklmnop",
            "TG_NOTIFY_CHAT_ID": "-123",
        },
        connect=shared,
        redis_client=cache,
        clickhouse=ArchiveClient(),
        stop=threading.Event(),
        telegram_sink=sink(TelegramConnection()),
        clock_ms=lambda: 86402001,
        monotonic_ms=lambda: 0,
        signed_connection_factory=lambda *_args, **_kwargs: pytest.fail(
            "no signed HTTP permitted in this QA"
        ),
        public_connection_factory=public_http,
        execution_guard=(lambda **_: pytest.fail("read-only client cannot write"))
        if managed
        else None,
    )
    management = process.runtime.execution.submit.readiness.request
    if managed:
        management = management.request
    assert management._permit.budget.lane == "management"
    assert (
        process.daemon.pipeline.market.collector.transport.budget.lane == "management"
    )
    for scheduler in process.daemon.pipeline.schedulers:
        provider = scheduler.context_provider
        while not hasattr(provider, "account"):
            provider = provider.delegate
        account = provider.account
        private = account.request.request if managed else account.request
        assert private is not management
        assert isinstance(private._permit.budget, AdmissionBudget)
        assert isinstance(account.public.budget, AdmissionBudget)
        assert isinstance(account.sentiment.budget, AdmissionBudget)
        assert account.public.environment == "SANDBOX"
        assert account.sentiment.environment == "LIVE"
        # No credential changes, no mutable balance cache, no write capability.
        assert private.account_id == management.account_id
        for method, path in (
            ("POST", "/fapi/v1/order"),
            ("POST", "/fapi/v1/algoOrder"),
            ("POST", "/fapi/v1/leverage"),
            ("DELETE", "/fapi/v1/order"),
        ):
            with pytest.raises(
                ExchangeTransportError, match="ENDPOINT_OR_WRITE_DISABLED"
            ):
                private(method, path, {})
    # Exhaust non-critical only, then demonstrate that all three actual context
    # ports defer before HTTP while the unchanged management permit still works.
    for environment in ("SANDBOX", "LIVE"):
        budget = AdmissionBudget(shared, environment=environment)
        assert budget.permit(1)
    with shared() as conn:
        conn.execute("UPDATE v2_egress_budgets SET used_weight=900,market_weight=900")
    for call in (
        lambda: account.request("GET", "/fapi/v3/account", {}),
        lambda: account.public("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"}),
        lambda: account.sentiment(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": "BTCUSDT", "period": "1h", "limit": 3},
        ),
    ):
        with pytest.raises(ValueError, match="^ADMISSION_BUDGET_UNAVAILABLE$"):
            call()
    assert management._permit("GET", "/fapi/v3/account") is True
    assert not public_http.calls
    cache.close()
