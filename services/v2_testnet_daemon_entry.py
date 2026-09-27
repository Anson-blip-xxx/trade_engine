"""Deployable, fail-closed composition for the V2 Binance Testnet daemon.

Imports of Redis/ClickHouse clients and all process side effects live in ``main``.
The factory remains injectable for isolated QA and performs no network requests.
"""

import argparse
import json
import os
import signal
import stat
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from services.v2_directional_account_context import BinanceDirectionalAccountContext
from services.v2_directional_admission import create_directional_admission_scheduler
from services.v2_directional_context import DirectionalContext
from services.v2_directional_execution import bind_directional_venue_gate
from services.v2_market_pipeline import create_market_pipeline
from services.v2_risk_reference import create_guarded_runtime
from services.v2_s0_regime import create_s0_stage
from services.v2_testnet_daemon import (
    TestnetTradingDaemon,
    create_directional_testnet_pipeline,
)
from services.v2_testnet_daemon_bootstrap import (
    DaemonTelegramNotifier,
    TestnetDaemonConfig,
)
from services.v2_testnet_inventory import config_fields, deployment_database
from v2_core.account_risk import AccountScope
from v2_core.binance import BinanceFutures
from v2_core.directional import number
from v2_core.directional_outcomes import DirectionalHistory
from v2_core.drawdown import DrawdownState
from v2_core.ingress import ContextProvider, milliseconds, symbol
from v2_core.ledger import amount
from v2_core.producer import RedisMarketContext
from v2_core.public_market import BinancePublicMarket, PublicRateBudget
from v2_core.telegram import TelegramOperationalSink
from v2_core.trade_notifications import TradeLifecycleNotifications
from v2_core.transport import BinanceSignedTransport

_SECRET_ENV = {
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "TG_NOTIFY_TOKEN",
    "TG_NOTIFY_CHAT_ID",
}


@dataclass(frozen=True)
class TestnetProcessConfig:
    __test__ = False

    daemon: TestnetDaemonConfig
    symbols: tuple[str, ...]
    external_position_exclusions: tuple[str, ...]
    exit_fee_rate: str
    enable_tv_signals: bool = False
    tv_admission_start_ms: int | None = None

    @classmethod
    def from_mapping(cls, values):
        if not hasattr(values, "get") or any(key in values for key in _SECRET_ENV):
            raise ValueError(
                "daemon secrets must come from a protected credential file"
            )
        daemon = TestnetDaemonConfig.from_mapping(values)
        raw = values.get("V2_SYMBOLS")
        if not isinstance(raw, str) or not raw or any(char.isspace() for char in raw):
            raise ValueError("explicit canonical Testnet symbols required")
        items = raw.split(",")
        if not 1 <= len(items) <= 20 or len(set(items)) != len(items):
            raise ValueError("bounded unique Testnet symbols required")
        targets = tuple(symbol(item) for item in items)
        raw_exclusions = values.get("V2_EXTERNAL_POSITION_EXCLUSIONS", "")
        if not isinstance(raw_exclusions, str) or any(
            char.isspace() for char in raw_exclusions
        ):
            raise ValueError("canonical external position exclusions required")
        exclusions = (
            tuple(symbol(item) for item in raw_exclusions.split(","))
            if raw_exclusions
            else ()
        )
        if (
            len(exclusions) > 20
            or len(set(exclusions)) != len(exclusions)
            or set(exclusions) & set(targets)
        ):
            raise ValueError("bounded disjoint position exclusions required")
        fee = values.get("V2_EXIT_FEE_RATE")
        if not isinstance(fee, str):
            raise TypeError("explicit exit fee rate required")
        normalized = number(fee, minimum=0, maximum=Decimal(".01"))
        if fee != format(normalized, "f"):
            raise ValueError("canonical exit fee rate required")
        tv_enabled = values.get("V2_ENABLE_TV_SIGNALS", "false")
        if not isinstance(tv_enabled, str) or tv_enabled not in {"true", "false"}:
            raise ValueError("explicit TV signal switch required")
        tv_start = values.get("V2_TV_ADMISSION_START_MS")
        if tv_enabled == "true":
            if (
                not isinstance(tv_start, str)
                or not tv_start.isascii()
                or not tv_start.isdigit()
                or not 0 < int(tv_start) <= 2**53 - 1
            ):
                raise ValueError("TV admission start timestamp required")
            tv_start = int(tv_start)
        elif tv_start is not None:
            raise ValueError("TV start timestamp requires TV signals enabled")
        return cls(daemon, targets, exclusions, fee, tv_enabled == "true", tv_start)

    def public_summary(self):
        return {
            **self.daemon.public_summary(),
            "symbols": list(self.symbols),
            "external_position_exclusions": list(self.external_position_exclusions),
            "exit_fee_rate": self.exit_fee_rate,
            "enable_tv_signals": self.enable_tv_signals,
            "tv_admission_start_ms": self.tv_admission_start_ms,
            "notifications": True,
        }


def daemon_credentials(path, *, notification_only=False):
    location = Path(path)
    if not location.is_absolute():
        raise ValueError("absolute credential path required")
    details = location.lstat()
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_mode & 0o077
        or not 1 <= details.st_size <= 65536
    ):
        raise ValueError("protected regular credential file required")
    if notification_only and any(
        line.split("=", 1)[0].strip().startswith("BINANCE_")
        for line in location.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ):
        raise ValueError("NOTIFICATION_FILE_CONTAINS_EXCHANGE_CREDENTIALS")
    return config_fields(
        location,
        {"TG_NOTIFY_TOKEN", "TG_NOTIFY_CHAT_ID"}
        if notification_only
        else {
            "BINANCE_TESTNET_API_KEY",
            "BINANCE_TESTNET_API_SECRET",
            "TG_NOTIFY_TOKEN",
            "TG_NOTIFY_CHAT_ID",
        },
    )


def process_credentials(path, *, values, config, connect):
    """Opt-in encrypted startup path. Never fall back after selecting vault mode."""
    source = values.get("V2_CREDENTIAL_SOURCE", "legacy")
    if source == "legacy":
        if any(key.startswith("V2_VAULT_") for key in values):
            raise ValueError("EXPLICIT_VAULT_MODE_REQUIRED")
        return daemon_credentials(path)
    if source != "vault":
        raise ValueError("INVALID_CREDENTIAL_SOURCE")
    # Lazy imports keep the old deployed runtime independent of optional crypto.
    from v2_core.account_risk import AccountScope
    from v2_core.runtime_credentials import resolve_runtime_credentials
    from v2_core.systemd_master_keys import SystemdMasterKeys

    raw_version = values.get("V2_VAULT_BINDING_VERSION", "")
    if (
        not isinstance(raw_version, str)
        or not raw_version.isascii()
        or not raw_version.isdecimal()
        or not 1 <= len(raw_version) <= 18
    ):
        raise ValueError("PINNED_BINDING_VERSION_REQUIRED")
    provider = SystemdMasterKeys(values["CREDENTIALS_DIRECTORY"])
    private = resolve_runtime_credentials(
        connect,
        scope=AccountScope("BINANCE", config.daemon.account_id, "SANDBOX", "FUTURES"),
        tenant_id=values["V2_VAULT_TENANT_ID"],
        registry_id=values["V2_VAULT_REGISTRY_ID"],
        binding_version=int(raw_version),
        key_provider=provider,
    )
    notifications = daemon_credentials(path, notification_only=True)
    return {
        **notifications,
        "BINANCE_TESTNET_API_KEY": private["api_key"],
        "BINANCE_TESTNET_API_SECRET": private["api_secret"],
    }


def execution_guard_for(values, *, config, connect):
    """Managed vault workers cannot start a write-capable legacy fallback."""
    from functools import partial
    from uuid import UUID

    from v2_core.execution_routes import ExecutionRoutes

    fields = {"V2_EXECUTION_EPOCH", "V2_EXECUTION_WORKER_TOKEN"}
    requested = any(key in values for key in fields)
    if not requested:
        if (
            values.get("V2_CREDENTIAL_SOURCE") == "vault"
            and config.daemon.write_enabled
        ):
            raise ValueError("MANAGED_EXECUTION_BINDING_REQUIRED")
        return None
    if not fields <= values.keys() or values.get("V2_CREDENTIAL_SOURCE") != "vault":
        raise ValueError("MANAGED_VAULT_MODE_REQUIRED")
    for key in ("V2_EXECUTION_EPOCH", "V2_VAULT_BINDING_VERSION"):
        raw = values.get(key, "")
        if (
            not isinstance(raw, str)
            or not raw.isascii()
            or not raw.isdecimal()
            or not 1 <= len(raw) <= 18
            or int(raw) < 1
        ):
            raise ValueError("PINNED_EXECUTION_VERSION_REQUIRED")
    token = str(UUID(values["V2_EXECUTION_WORKER_TOKEN"]))
    return partial(
        ExecutionRoutes(connect).submit_guard,
        tenant_id=values["V2_VAULT_TENANT_ID"],
        environment="SANDBOX",
        registry_id=values["V2_VAULT_REGISTRY_ID"],
        binding_version=int(values["V2_VAULT_BINDING_VERSION"]),
        epoch=int(values["V2_EXECUTION_EPOCH"]),
        worker_token=token,
    )


class PrivateRatePermit:
    _READS: ClassVar = {
        "/fapi/v1/algoOrder": 1,
        "/fapi/v1/order": 1,
        "/fapi/v1/userTrades": 5,
        "/fapi/v1/positionSide/dual": 30,
        "/fapi/v1/income": 30,
        "/fapi/v3/account": 5,
        "/fapi/v3/positionRisk": 5,
        "/fapi/v1/openOrders": 40,
        "/fapi/v1/openAlgoOrders": 40,
        "/fapi/v1/multiAssetsMargin": 30,
        "/fapi/v1/accountConfig": 5,
        "/fapi/v1/symbolConfig": 5,
        "/fapi/v1/leverageBracket": 1,
    }

    def __init__(self, budget, *, entries, protection, exits):
        if any(type(flag) is not bool for flag in (entries, protection, exits)):
            raise ValueError("explicit private endpoint permissions required")
        self.budget = budget
        self.writes = {
            ("POST", "/fapi/v1/order"): entries or exits,
            ("POST", "/fapi/v1/algoOrder"): protection,
            ("POST", "/fapi/v1/leverage"): entries,
            ("POST", "/fapi/v1/marginType"): entries,
            ("DELETE", "/fapi/v1/order"): protection,
            ("DELETE", "/fapi/v1/algoOrder"): protection,
        }

    def __call__(self, method, path):
        weight = self._READS.get(path) if method == "GET" else 1
        if method != "GET" and self.writes.get((method, path)) is not True:
            return False
        if method == "GET" and path not in self._READS:
            return False
        while weight:
            chunk = min(weight, 10)
            if self.budget.permit(chunk) is not True:
                return False
            weight -= chunk
        return True


class BinanceTestnetMark:
    def __init__(self, public_market, *, clock_ms, max_age_ms=5000):
        if (
            getattr(public_market, "environment", None) != "SANDBOX"
            or not callable(public_market)
            or not callable(clock_ms)
            or type(max_age_ms) is not int
            or not 1000 <= max_age_ms <= 10000
        ):
            raise ValueError("explicit fresh Testnet mark source required")
        self.public, self.clock, self.max_age = public_market, clock_ms, max_age_ms

    def __call__(self, raw_symbol):
        target = symbol(raw_symbol)
        started = milliseconds(self.clock())
        premium = self.public("/fapi/v1/premiumIndex", {"symbol": target})
        exchange = self.public("/fapi/v1/exchangeInfo", {})
        finished = milliseconds(self.clock())
        if (
            not isinstance(premium, dict)
            or premium.get("symbol") != target
            or not isinstance(premium.get("markPrice"), str)
            or type(premium.get("time")) is not int
            or not started <= finished <= started + self.max_age
            or not 0 <= finished - milliseconds(premium["time"]) < self.max_age
            or not isinstance(exchange, dict)
            or not isinstance(exchange.get("symbols"), list)
        ):
            raise ValueError("invalid or stale Testnet mark")
        matches = [row for row in exchange["symbols"] if row.get("symbol") == target]
        if len(matches) != 1 or not isinstance(matches[0].get("filters"), list):
            raise ValueError("unique Testnet price rule required")
        prices = [
            row
            for row in matches[0]["filters"]
            if isinstance(row, dict) and row.get("filterType") == "PRICE_FILTER"
        ]
        if len(prices) != 1:
            raise ValueError("unique Testnet price rule required")
        mark = format(amount(premium["markPrice"], positive=True).normalize(), "f")
        tick = format(amount(prices[0]["tickSize"], positive=True).normalize(), "f")
        return {
            "symbol": target,
            "environment": "SANDBOX",
            "mark_price": mark,
            "tick_size": tick,
            "observed_at_ms": premium["time"],
            "source": "binance-public-mark-v1",
        }


@dataclass
class TestnetProcess:
    __test__ = False

    daemon: TestnetTradingDaemon
    market: object
    runtime: object


def create_testnet_process(
    config,
    secrets,
    *,
    connect,
    redis_client,
    clickhouse,
    stop,
    telegram_sink,
    clock_ms,
    monotonic_ms,
    signed_connection_factory=None,
    public_connection_factory=None,
    execution_guard=None,
):
    if (
        not isinstance(config, TestnetProcessConfig)
        or set(secrets)
        != {
            "BINANCE_TESTNET_API_KEY",
            "BINANCE_TESTNET_API_SECRET",
            "TG_NOTIFY_TOKEN",
            "TG_NOTIFY_CHAT_ID",
        }
        or not isinstance(telegram_sink, TelegramOperationalSink)
        or telegram_sink.environment != "SANDBOX"
    ):
        raise ValueError("explicit Testnet process dependencies required")
    daemon_cfg = config.daemon
    scope = AccountScope("BINANCE", daemon_cfg.account_id, "SANDBOX", "FUTURES")
    market_notify = lambda event: telegram_sink(
        event["event_id"], event["environment"], event["kind"], event
    )
    market = create_market_pipeline(
        connect,
        clickhouse=clickhouse,
        redis_client=redis_client,
        config={
            "environment": "SANDBOX",
            "symbols": list(config.symbols),
            "egress_scope": "v2-testnet-public",
            "weight_limit": 1200,
            "max_pending": 3,
            "max_age_ms": 120000,
            "lifetime_ms": 90000,
            "enabled": True,
        },
        notify=market_notify,
        clock_ms=clock_ms,
        monotonic_ms=monotonic_ms,
        **(
            {"http_connection_factory": public_connection_factory}
            if public_connection_factory is not None
            else {}
        ),
    )
    regime = create_s0_stage(
        connect,
        archive=market.source.archive,
        redis_client=redis_client,
        environment="SANDBOX",
        symbols=config.symbols,
        clock_ms=clock_ms,
        max_age_ms=90000,
        lifetime_ms=120000,
    )
    public_market = market.collector.transport
    sentiment_market = BinancePublicMarket(
        environment="LIVE",
        budget=PublicRateBudget(connect, scope="v2-testnet-live-sentiment", limit=1200),
        enabled=True,
        **(
            {"connection_factory": public_connection_factory}
            if public_connection_factory is not None
            else {}
        ),
    )
    private_budget = PublicRateBudget(
        connect, scope="v2-testnet-private:" + scope.account_id, limit=2400
    )
    permit = PrivateRatePermit(
        private_budget,
        entries=daemon_cfg.enable_entries,
        protection=daemon_cfg.enable_protection_writes,
        exits=daemon_cfg.enable_reduce_only_exits,
    )
    request = BinanceSignedTransport(
        account_id=scope.account_id,
        environment="SANDBOX",
        api_key=secrets["BINANCE_TESTNET_API_KEY"],
        api_secret=secrets["BINANCE_TESTNET_API_SECRET"],
        clock_ms=clock_ms,
        permit=permit,
        enable_trading=(
            daemon_cfg.enable_entries or daemon_cfg.enable_reduce_only_exits
        ),
        enable_testnet_cancellation=daemon_cfg.enable_protection_writes,
        enable_testnet_order_cancellation=daemon_cfg.enable_protection_writes,
        enable_testnet_protection=daemon_cfg.enable_protection_writes,
        enable_testnet_settings=daemon_cfg.enable_entries,
        **(
            {"connection_factory": signed_connection_factory}
            if signed_connection_factory is not None
            else {}
        ),
    )
    if execution_guard is not None:
        from v2_core.guarded_transport import GuardedSignedRequest

        request = GuardedSignedRequest(
            request,
            account_id=scope.account_id,
            environment=scope.environment,
            guard=execution_guard,
        )
    venue = BinanceFutures(
        request, account_id=scope.account_id, environment=scope.environment
    )
    drawdown = DrawdownState(
        connect,
        scope,
        source="binance-account-context-v1",
        currency="USDT",
        max_age_ms=15000,
    )

    def risk_check(order):
        if order.get("leg") == "CLOSE":
            return order.get("reduce_only") is True
        state = json.loads(drawdown.current().payload_json)
        return state["state"]["factor"] != "0"

    runtime = create_guarded_runtime(
        connect,
        scope=scope,
        archive=market.source.archive,
        clock_ms=clock_ms,
        max_reference_age_ms=90000,
        submit=venue.submit,
        query=venue.query,
        risk_check=risk_check,
        enabled=daemon_cfg.enable_entries or daemon_cfg.enable_reduce_only_exits,
    )
    mark = BinanceTestnetMark(public_market, clock_ms=clock_ms)
    bind_directional_venue_gate(
        runtime,
        request,
        mark_reference=mark,
        enabled=daemon_cfg.enable_entries,
        reduce_only_enabled=daemon_cfg.enable_reduce_only_exits,
        excluded_position_symbols=config.external_position_exclusions,
    )
    cache = RedisMarketContext(redis_client, environment="SANDBOX")
    market_context = ContextProvider(
        environment="SANDBOX",
        policy={
            "s0": {"scope": "GLOBAL", "max_age_ms": 120000},
            "s3": {"scope": "SYMBOL", "max_age_ms": 90000},
        },
        read=cache.read,
        clock_ms=clock_ms,
    )
    schedulers = []
    sources = [("S6", "s3"), ("S8", "s3")]
    if config.enable_tv_signals:
        sources.extend((("S6", "tv_bridge"), ("S8", "tv_bridge")))
    for strategy, source in sources:
        producer = strategy.lower()
        history = DirectionalHistory(
            connect, scope=scope, producer=producer, clock_ms=clock_ms
        )
        account = BinanceDirectionalAccountContext(
            connect,
            request,
            public_market,
            history,
            drawdown,
            scope=scope,
            clock_ms=clock_ms,
            sentiment_market=sentiment_market,
        )
        context = DirectionalContext(
            market_context, account, scope=scope, clock_ms=clock_ms
        )
        schedulers.append(
            create_directional_admission_scheduler(
                runtime,
                context_provider=context,
                strategy=strategy,
                analysis_mode="hard",
                max_delay_ms=30000,
                enable_admission=True,
                source=source,
                allowed_symbols=config.symbols if source == "tv_bridge" else (),
                tv_start_ms=config.tv_admission_start_ms
                if source == "tv_bridge"
                else None,
            )
        )
    pipeline = create_directional_testnet_pipeline(
        runtime=runtime,
        request=request,
        public_market=public_market,
        market=market,
        regime=regime,
        schedulers=schedulers,
        mark_reference=mark,
        clock_ms=clock_ms,
        exit_fee_rate=config.exit_fee_rate,
        enable_entries=daemon_cfg.enable_entries,
        enable_protection_writes=daemon_cfg.enable_protection_writes,
        enable_reduce_only_exits=daemon_cfg.enable_reduce_only_exits,
        external_position_exclusions=config.external_position_exclusions,
    )
    notifier = DaemonTelegramNotifier(
        telegram_sink, account_id=scope.account_id, clock_ms=clock_ms
    )
    trade_notifications = TradeLifecycleNotifications(
        connect, telegram_sink, scope=scope, pin_messages=True
    )
    daemon = TestnetTradingDaemon(
        pipeline,
        stop=stop,
        notify=notifier,
        interval_seconds=daemon_cfg.interval_seconds,
        trade_notifications=trade_notifications,
    )
    return TestnetProcess(daemon, market, runtime)


def main(environ=None):
    import clickhouse_connect
    import redis

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True)
    args = parser.parse_args()
    config = TestnetProcessConfig.from_mapping(
        os.environ if environ is None else environ
    )
    connect = deployment_database()
    execution_guard = execution_guard_for(
        os.environ if environ is None else environ, config=config, connect=connect
    )
    if execution_guard is not None:
        with execution_guard(operation="MANAGE"):
            pass  # Reject unowned startup before decrypting credentials or building clients.
    secrets = process_credentials(
        args.credentials,
        values=os.environ if environ is None else environ,
        config=config,
        connect=connect,
    )
    cache = redis.Redis(
        unix_socket_path="/var/lib/trade-engine-v2/redis.sock",
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    archive = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18123,
        database="trade_v2_testnet",
        connect_timeout=3,
        send_receive_timeout=10,
    )
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    sink = TelegramOperationalSink(
        token=secrets["TG_NOTIFY_TOKEN"],
        chat_id=secrets["TG_NOTIFY_CHAT_ID"],
        alerts_chat_id=(os.environ if environ is None else environ).get(
            "V2_TG_ALERT_CHAT_ID"
        ),
        environment="SANDBOX",
    )
    try:
        process = create_testnet_process(
            config,
            secrets,
            connect=connect,
            redis_client=cache,
            clickhouse=archive,
            stop=stop,
            telegram_sink=sink,
            clock_ms=lambda: time.time_ns() // 1000000,
            monotonic_ms=lambda: time.monotonic_ns() // 1000000,
            execution_guard=execution_guard,
        )
        print(
            json.dumps(
                {"status": "STARTING", "configuration": config.public_summary()}
            ),
            flush=True,
        )
        process.daemon.serve()
    finally:
        archive.close()
        cache.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never expose credentials or signed requests
        print('{"status":"TESTNET_DAEMON_FAILED"}', flush=True)
        raise SystemExit(1) from None
