"""Real Testnet controller ports: encrypted credentials, inventory and child worker.

No shell interpolation, plaintext credential files, LIVE endpoints or shared
mutable account configuration. Every child receives an immutable binding.
"""

import json
import multiprocessing
import os
import time
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from functools import partial
from pathlib import Path
from uuid import uuid4

from services.v2_testnet_daemon_entry import (
    PrivateRatePermit,
    TestnetProcessConfig,
    create_testnet_process,
)
from v2_core.account_inventory import AccountInventory, persist_inventory
from v2_core.account_risk import AccountPolicy, AccountRisk, AccountScope
from v2_core.database import connection_factory
from v2_core.execution_routes import ExecutionRoutes
from v2_core.public_market import PublicRateBudget
from v2_core.runtime_credentials import resolve_runtime_credentials
from v2_core.runtime_policy import PolicyStore
from v2_core.systemd_master_keys import SystemdMasterKeys
from v2_core.telegram import TelegramOperationalSink
from v2_core.transport import BinanceSignedTransport


def database():
    return connection_factory(
        "host=/var/run/postgresql port=55432 dbname=trade_v2_testnet user=ubuntu",
        schema="trade_v2",
    )


@contextmanager
def vault_connection():
    with database()() as c:
        c.execute("SET LOCAL ROLE trade_v2_vault_operator")
        yield c


def scope_for(tenant, target):
    with vault_connection() as c:
        row = c.execute(
            "SELECT exchange,account_id,environment,product,version FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=%s",
            (tenant, target["target_registry"]),
        ).fetchone()
    if row is None or row[2] != "SANDBOX" or row[4] != target["binding_version"]:
        raise ValueError("TESTNET_BINDING_CHANGED")
    return AccountScope(*row[:4])


def private_for(tenant, target, scope):
    return resolve_runtime_credentials(
        vault_connection,
        scope=scope,
        tenant_id=tenant,
        registry_id=target["target_registry"],
        binding_version=target["binding_version"],
        key_provider=SystemdMasterKeys(os.environ["CREDENTIALS_DIRECTORY"]),
    )


def readonly_request(tenant, target, scope):
    private = private_for(tenant, target, scope)
    return BinanceSignedTransport(
        account_id=scope.account_id,
        environment="SANDBOX",
        api_key=private["api_key"],
        api_secret=private["api_secret"],
        clock_ms=lambda: time.time_ns() // 1000000,
        permit=PrivateRatePermit(
            PublicRateBudget(
                database(), scope="v2-testnet-private:" + scope.account_id, limit=2400
            ),
            entries=False,
            protection=False,
            exits=False,
        ),
    )


class TestnetPorts:
    def __init__(self, tenant, values):
        self.tenant, self.values = tenant, dict(values)

    def inventory(self, target):
        scope = scope_for(self.tenant, target)
        request = readonly_request(self.tenant, target, scope)
        observation = AccountInventory(
            request, scope=scope, clock_ms=lambda: time.time_ns() // 1000000
        ).collect(str(uuid4()))
        persist_inventory(database(), scope=scope, observation=observation)
        blockers = list(observation["blockers"])
        config = request("GET", "/fapi/v1/accountConfig", {})
        if config.get("canTrade") is not True:
            blockers.append("TRADE_PERMISSION_UNVERIFIED")
        return scope, observation, blockers

    @staticmethod
    def ledger_clear(scope):
        with database()() as c:
            unresolved = c.execute(
                "SELECT count(*) FROM v2_trade_intents i JOIN v2_episodes e ON e.episode_id=i.intent_id "
                "WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s) AND e.status NOT IN ('SETTLED','ABORTED')",
                (scope.exchange, scope.account_id, scope.environment, scope.product),
            ).fetchone()[0]
            orders = c.execute(
                "SELECT count(*) FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
                "WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s) AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED')",
                (scope.exchange, scope.account_id, scope.environment, scope.product),
            ).fetchone()[0]
        return [] if not unresolved and not orders else ["ACCOUNT_LEDGER_NOT_CLEAR"]

    def source_clear(self, target):
        try:
            scope, _, blockers = self.inventory(target)
            return [
                b
                for b in blockers
                if b not in {"NO_AVAILABLE_BALANCE", "TRADE_PERMISSION_UNVERIFIED"}
            ] + self.ledger_clear(scope)
        except Exception:  # noqa: BLE001
            return ["SOURCE_READINESS_UNAVAILABLE"]

    def preflight(self, target):
        try:
            scope, observation, blockers = self.inventory(target)
            blockers += self.ledger_clear(scope)
            if blockers:
                return blockers
            wallet = Decimal(observation["summary"]["balance"]["totalWalletBalance"])
            initial = min(
                wallet, Decimal(self.values.get("V2_CONTROLLER_INITIAL_EQUITY", "1000"))
            )
            if initial <= 0:
                return ["INVALID_INITIAL_EQUITY"]
            connect = database()
            # Copy configuration, never an account's accrued PnL/high-water state.
            template_scope = AccountScope(
                "BINANCE", self.values["V2_ACCOUNT_ID"], "SANDBOX", "FUTURES"
            )
            profile = PolicyStore(connect, template_scope).read().values
            profile = {
                **profile,
                "capital.enabled": True,
                "capital.model_enabled": True,
                "capital.initial_equity": format(initial, "f"),
                "capital.reference_wallet": format(wallet, "f"),
                "capital.started_at_ms": time.time_ns() // 1000000,
                "capital.pool_fraction": "0.70",
            }
            with connect() as c:
                nested = lambda: nullcontext(c)
                store = PolicyStore(nested, scope)
                if store.read().version == 0:
                    store.patch(
                        profile,
                        expected_version=0,
                        reason="MANAGED_TESTNET_INITIAL_PROFILE",
                    )
                if not c.execute(
                    "SELECT 1 FROM v2_risk_accounts WHERE scope=%s", (scope.key,)
                ).fetchone():
                    AccountRisk(nested).configure(
                        scope,
                        AccountPolicy(
                            "USDT", None, None, 0, "binance-closed-1m-v1", 90000
                        ),
                        expected_version=0,
                    )
            return []
        except Exception:  # noqa: BLE001
            return ["TARGET_PREFLIGHT_FAILED"]

    def spawn(self, target):
        return ChildWorker(self.tenant, target, self.values)


def child_main(tenant, target, values, stop, release, channel):
    cache = archive = None
    try:
        import clickhouse_connect
        import redis

        scope = scope_for(tenant, target)
        private = private_for(tenant, target, scope)
        keys = SystemdMasterKeys(os.environ["CREDENTIALS_DIRECTORY"])
        keys("v1")
        notifications = json.loads(
            (
                Path(os.environ["CREDENTIALS_DIRECTORY"]) / "v2-notifications"
            ).read_bytes()
        )
        if set(notifications) != {"TG_NOTIFY_TOKEN", "TG_NOTIFY_CHAT_ID"}:
            raise ValueError("NOTIFICATION_FIELDS_REQUIRED")
        entries = values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false")
        if entries not in {"true", "false"}:
            raise ValueError("EXPLICIT_ENTRY_MODE_REQUIRED")
        config = TestnetProcessConfig.from_mapping(
            {
                **values,
                "V2_ACCOUNT_ID": scope.account_id,
                "V2_ENVIRONMENT": "SANDBOX",
                "V2_ENABLE_ENTRIES": entries,
                "V2_ENABLE_PROTECTION_WRITES": "true",
                "V2_ENABLE_REDUCE_ONLY_EXITS": "true",
                "V2_TESTNET_WRITE_ACK": "SANDBOX:" + scope.account_id,
                "V2_EXTERNAL_POSITION_EXCLUSIONS": "",
                **(
                    {"V2_TV_ADMISSION_START_MS": str(time.time_ns() // 1000000)}
                    if values.get("V2_ENABLE_TV_SIGNALS") == "true"
                    else {}
                ),
            }
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
        cache.ping()
        archive.command("SELECT 1")
        connect = database()
        guard = partial(
            ExecutionRoutes(connect).submit_guard,
            tenant_id=tenant,
            environment="SANDBOX",
            registry_id=target["target_registry"],
            binding_version=target["binding_version"],
            epoch=target["epoch"],
            worker_token=target["worker_token"],
        )
        sink = TelegramOperationalSink(
            token=notifications["TG_NOTIFY_TOKEN"],
            chat_id=notifications["TG_NOTIFY_CHAT_ID"],
            alerts_chat_id=values.get("V2_TG_ALERT_CHAT_ID"),
            environment="SANDBOX",
        )
        process = create_testnet_process(
            config,
            {
                **notifications,
                "BINANCE_TESTNET_API_KEY": private["api_key"],
                "BINANCE_TESTNET_API_SECRET": private["api_secret"],
            },
            connect=connect,
            redis_client=cache,
            clickhouse=archive,
            stop=stop,
            telegram_sink=sink,
            clock_ms=lambda: time.time_ns() // 1000000,
            monotonic_ms=lambda: time.monotonic_ns() // 1000000,
            execution_guard=guard,
        )
        channel.send("READY")
        while not release.wait(0.5):
            if stop.is_set():
                return
        while not stop.is_set():
            with connect() as c:
                row = c.execute(
                    "SELECT phase,worker_token::text FROM v2_execution_routes WHERE tenant_id=%s AND environment='SANDBOX'",
                    (tenant,),
                ).fetchone()
            if (
                row is None
                or row[1] != target["worker_token"]
                or row[0] not in {"ACTIVE", "DRAINING"}
            ):
                return
            process.daemon.pipeline.enable_entries = (
                entries == "true" and row[0] == "ACTIVE"
            )
            try:
                process.daemon.run_once()
                channel.send("CYCLE")
            except Exception:  # noqa: BLE001
                channel.send("DEGRADED")
            stop.wait(config.daemon.interval_seconds)
    except Exception:  # noqa: BLE001 - never disclose exception contexts containing credentials
        try:
            channel.send("FAILED")
        except Exception:  # noqa: BLE001,S110 - closed IPC channel must not disclose context
            pass
    finally:
        if archive is not None:
            archive.close()
        if cache is not None:
            cache.close()
        channel.close()


class ChildWorker:
    def __init__(self, tenant, target, values):
        self.last_progress = time.monotonic()
        self.progress_timeout = int(
            values.get("V2_CONTROLLER_PROGRESS_TIMEOUT_SECONDS", "300")
        )
        if not 30 <= self.progress_timeout <= 3600:
            raise ValueError("BOUNDED_PROGRESS_TIMEOUT_REQUIRED")
        self.entries_enabled = (
            values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false") == "true"
        )
        ctx = multiprocessing.get_context("spawn")
        self.stop_event, self.release_event = ctx.Event(), ctx.Event()
        self.channel, child = ctx.Pipe(duplex=False)
        self.process = ctx.Process(
            target=child_main,
            args=(tenant, target, values, self.stop_event, self.release_event, child),
        )
        self.process.start()
        child.close()

    def ready(self):
        return (
            self.channel.poll(25)
            and self.channel.recv() == "READY"
            and self.process.is_alive()
        )

    def release(self):
        self.release_event.set()

    def alive(self):
        while self.channel.poll():
            try:
                message = self.channel.recv()
                if message == "FAILED":
                    return False
                if message in {"CYCLE", "DEGRADED"}:
                    self.last_progress = time.monotonic()
            except EOFError:
                return False
        return self.process.is_alive()

    def healthy(self):
        return time.monotonic() - self.last_progress <= self.progress_timeout

    def stop(self):
        self.stop_event.set()
        self.process.join(15)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(5)
        return not self.process.is_alive()
