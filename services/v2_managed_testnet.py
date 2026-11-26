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
from pathlib import Path
from uuid import uuid4

from psycopg.errors import InsufficientPrivilege, UndefinedFunction

from services.v2_execution_guard import create_execution_guard
from services.v2_systemd_master_keys import SystemdMasterKeys
from services.v2_testnet_daemon_entry import (
    PrivateRatePermit,
    TestnetProcessConfig,
    create_testnet_process,
)
from v2_core.account_coverage import AccountCoverageAudit
from v2_core.account_inventory import AccountInventory, persist_inventory
from v2_core.account_risk import AccountPolicy, AccountRisk, AccountScope
from v2_core.bounded_job import cooperative_flag
from v2_core.cycle_cadence import cycle_delay
from v2_core.database import connection_factory
from v2_core.public_market import PublicRateBudget
from v2_core.runtime_credentials import resolve_runtime_credentials
from v2_core.runtime_policy import PolicyStore
from v2_core.telegram import TelegramOperationalSink
from v2_core.transport import BinanceSignedTransport


def database():
    return connection_factory(
        "host=/var/run/postgresql port=55432 dbname=trade_v2_testnet user=ubuntu",
        schema="trade_v2",
    )


def _safe_cycle_failure(exc):
    """Return code metadata only; never format exception arguments or locals."""
    name = type(exc).__name__
    error_type = (
        name
        if name.isascii() and name.isidentifier() and len(name) <= 80
        else "Exception"
    )
    root = os.path.dirname(os.path.dirname(__file__))
    frames = []
    cursor = exc.__traceback__
    while cursor is not None:
        code = cursor.tb_frame.f_code
        relative = os.path.relpath(code.co_filename, root).split(os.sep)
        if (
            len(relative) == 2
            and relative[0] in {"services", "v2_core"}
            and relative[1].endswith(".py")
            and relative[1][:-3].isascii()
            and relative[1][:-3].isidentifier()
            and code.co_name.isascii()
            and code.co_name.isidentifier()
        ):
            frames.append(
                {
                    "module": relative[0] + "." + relative[1][:-3],
                    "function": code.co_name,
                    "line": cursor.tb_lineno,
                }
            )
        cursor = cursor.tb_next
    return {"error_type": error_type, "frames": frames[-5:]}


class _CycleDiagnostics:
    """Bounded journal diagnostics; a logging failure never controls trading."""

    def __init__(self, interval_seconds=60, *, clock=None):
        if type(interval_seconds) is not int or not 5 <= interval_seconds <= 3600:
            raise ValueError("BOUNDED_CYCLE_DIAGNOSTIC_INTERVAL_REQUIRED")
        self.interval = interval_seconds
        self.clock = clock or time.monotonic
        self.failures = self.suppressed = 0
        self.last_emitted = None

    @staticmethod
    def _emit(payload):
        try:
            print(json.dumps(payload, separators=(",", ":")), flush=True)
        except Exception:  # noqa: BLE001,S110 - logging must not break protection
            pass

    def failed(self, exc):
        self.failures += 1
        now = self.clock()
        if self.last_emitted is not None and now - self.last_emitted < self.interval:
            self.suppressed += 1
            return
        self._emit(
            {
                "component": "execution-worker-cycle",
                "status": "DEGRADED",
                "error_code": "EXECUTION_CYCLE_FAILED",
                **_safe_cycle_failure(exc),
                "consecutive_failures": self.failures,
                "suppressed_since_last": self.suppressed,
            }
        )
        self.last_emitted = now
        self.suppressed = 0

    def succeeded(self):
        if self.failures:
            self._emit(
                {
                    "component": "execution-worker-cycle",
                    "status": "RECOVERED",
                    "error_code": "EXECUTION_CYCLE_RECOVERED",
                    "consecutive_failures": self.failures,
                    "suppressed_since_last": self.suppressed,
                }
            )
        self.failures = self.suppressed = 0
        self.last_emitted = None


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
    budget = PublicRateBudget(
        database(), scope="v2-testnet-private:" + scope.account_id, limit=2400
    )
    if os.environ.get("V2_SHARED_MARKET", "false") == "true":
        from v2_core.shared_market import HostBudget

        budget = HostBudget(database(), environment="SANDBOX")
    return BinanceSignedTransport(
        account_id=scope.account_id,
        environment="SANDBOX",
        api_key=private["api_key"],
        api_secret=private["api_secret"],
        clock_ms=lambda: time.time_ns() // 1000000,
        permit=PrivateRatePermit(
            budget,
            entries=False,
            protection=False,
            exits=False,
        ),
    )


def _initialize_fresh_capital(conn, scope, *, wallet, started_at_ms):
    """Honor an explicit fresh-ledger marker, never reset an accrued account.

    Called only after the controller's fresh venue inventory is clear. The
    retained policy is authoritative; a template must not overwrite its rules.
    """
    if not isinstance(scope, AccountScope) or scope.environment != "SANDBOX":
        raise ValueError("FRESH_CAPITAL_SANDBOX_REQUIRED")
    store = PolicyStore(lambda: nullcontext(conn), scope)
    current = store.read()
    if current.version == 0 or current.values["capital.started_at_ms"] != 0:
        return False
    if (
        not isinstance(wallet, Decimal)
        or not wallet.is_finite()
        or wallet < 1
        or type(started_at_ms) is not int
        or not 0 < started_at_ms <= 4133980800000
    ):
        raise ValueError("FRESH_CAPITAL_INVALID_BASELINE")
    # Only this one-time, explicitly requested initialization takes table locks.
    # Facts cannot appear between the empty-ledger check and the policy CAS.
    conn.execute("SET LOCAL lock_timeout='1s'")
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        ("testnet-protocol-account:" + scope.key,),
    )
    current = store.read()
    if current.version == 0 or current.values["capital.started_at_ms"] != 0:
        return False
    identity = (scope.exchange, scope.account_id, scope.environment, scope.product)
    clear = conn.execute(
        "SELECT v2_fresh_capital_scope_clear(%s,%s,%s,%s)", identity
    ).fetchone()
    if clear != (True,):
        raise ValueError("FRESH_CAPITAL_LEDGER_NOT_EMPTY")
    store.patch(
        {
            "capital.reference_wallet": format(wallet, "f"),
            "capital.started_at_ms": started_at_ms,
            "capital.initial_equity": format(
                min(wallet, Decimal(current.values["capital.initial_equity"])), "f"
            ),
        },
        expected_version=current.version,
        reason="MANAGED_TESTNET_FRESH_LEDGER_BASELINE",
    )
    return True


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

    def resume_preflight(self, target):
        """Same binding only (controller-enforced); never reset policy or capital."""
        try:
            scope = scope_for(self.tenant, target)
            connect = database()
            if PolicyStore(connect, scope).read().version == 0:
                return ["RESUME_EXISTING_POLICY_REQUIRED"]
            request = readonly_request(self.tenant, target, scope)
            if request("GET", "/fapi/v1/accountConfig", {}).get("canTrade") is not True:
                return ["TRADE_PERMISSION_UNVERIFIED"]
            coverage = AccountCoverageAudit(
                connect,
                request,
                scope=scope,
                clock_ms=lambda: time.time_ns() // 1000000,
            ).run_once()
            if coverage.get("status") != "ACCOUNT_COVERAGE_CLEAR":
                return coverage.get("blockers") or ["RESUME_COVERAGE_UNAVAILABLE"]
            return []
        except Exception:  # noqa: BLE001 - no raw credential/transport diagnostics
            return ["RESUME_READINESS_UNAVAILABLE"]

    def preflight(self, target):
        try:
            scope, observation, blockers = self.inventory(target)
            blockers += self.ledger_clear(scope)
            if blockers:
                return blockers
            wallet = Decimal(observation["summary"]["balance"]["totalWalletBalance"])
            connect = database()
            with connect() as c:
                nested = lambda: nullcontext(c)
                store = PolicyStore(nested, scope)
                if store.read().version == 0:
                    initial = min(
                        wallet,
                        Decimal(
                            self.values.get("V2_CONTROLLER_INITIAL_EQUITY", "1000")
                        ),
                    )
                    if initial <= 0:
                        return ["INVALID_INITIAL_EQUITY"]
                    # New accounts retain the original template initialization.
                    template_scope = AccountScope(
                        "BINANCE", self.values["V2_ACCOUNT_ID"], "SANDBOX", "FUTURES"
                    )
                    profile = {
                        **PolicyStore(nested, template_scope).read().values,
                        "capital.enabled": True,
                        "capital.model_enabled": True,
                        "capital.initial_equity": format(initial, "f"),
                        "capital.reference_wallet": format(wallet, "f"),
                        "capital.started_at_ms": time.time_ns() // 1000000,
                        "capital.pool_fraction": "0.70",
                    }
                    store.patch(
                        profile,
                        expected_version=0,
                        reason="MANAGED_TESTNET_INITIAL_PROFILE",
                    )
                else:
                    _initialize_fresh_capital(
                        c, scope, wallet=wallet, started_at_ms=time.time_ns() // 1000000
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
        except InsufficientPrivilege:
            return ["TARGET_PREFLIGHT_PERMISSION_DENIED"]
        except UndefinedFunction:
            return ["FRESH_CAPITAL_SCHEMA_NOT_READY"]
        except Exception:  # noqa: BLE001
            return ["TARGET_PREFLIGHT_FAILED"]

    def spawn(self, target):
        if _worker_mode(self.values) == "MANAGEMENT_WITH_SETTLEMENT":
            from services.v2_settlement_worker import SettlementWorker
            from services.v2_worker_group import WorkerGroup

            return WorkerGroup.create(
                (
                    lambda: ChildWorker(
                        self.tenant,
                        target,
                        {**self.values, "V2_CONTROLLER_WORKER_MODE": "MANAGEMENT_ONLY"},
                    ),
                    lambda: SettlementWorker(self.tenant, target, self.values),
                ),
                entries_enabled=False,
            )
        return ChildWorker(self.tenant, target, self.values)


def _worker_mode(values):
    mode = values.get("V2_CONTROLLER_WORKER_MODE", "LEGACY")
    if mode not in ("LEGACY", "MANAGEMENT_ONLY", "MANAGEMENT_WITH_SETTLEMENT"):
        raise ValueError("EXPLICIT_CONTROLLER_WORKER_MODE_REQUIRED")
    if (
        mode != "LEGACY"
        and values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false") != "false"
    ):
        raise ValueError("MANAGEMENT_ONLY_ENTRIES_DISABLED_REQUIRED")
    return mode


def _management_child(tenant, target, values, stop, release, channel, diagnostics):
    """One managed writer, without constructing any strategy-side services."""
    from services.v2_management_process import (
        ManagementProcessConfig,
        create_management_process,
    )
    from services.v2_management_runtime import ManagementBinding

    scope = scope_for(tenant, target)
    private = private_for(tenant, target, scope)
    config = ManagementProcessConfig.from_mapping(
        {
            "V2_ACCOUNT_ID": scope.account_id,
            "V2_ENVIRONMENT": "SANDBOX",
            "V2_INTERVAL_SECONDS": values.get("V2_INTERVAL_SECONDS"),
            "V2_EXIT_FEE_RATE": values.get("V2_EXIT_FEE_RATE"),
            "V2_SHARED_MARKET": values.get("V2_SHARED_MARKET", "false"),
            "V2_ENABLE_ENTRIES": "false",
            "V2_ENABLE_PROTECTION_WRITES": "true",
            "V2_ENABLE_REDUCE_ONLY_EXITS": "true",
            "V2_TESTNET_WRITE_ACK": "SANDBOX:" + scope.account_id,
        }
    )
    binding = ManagementBinding(
        scope=scope,
        tenant_id=tenant,
        registry_id=target["target_registry"],
        binding_version=target["binding_version"],
        epoch=target["epoch"],
        worker_token=target["worker_token"],
    )
    connect = database()
    guard = create_execution_guard(connect, **binding.guard_args)
    guard.preflight()
    supervisor = create_management_process(
        config,
        {
            "BINANCE_TESTNET_API_KEY": private["api_key"],
            "BINANCE_TESTNET_API_SECRET": private["api_secret"],
        },
        connect=connect,
        binding=binding,
        execution_guard=guard,
        clock_ms=lambda: time.time_ns() // 1000000,
    )
    channel.send("READY")
    while not release.wait(0.5):
        if stop.is_set():
            return
    while not stop.is_set():
        started = time.monotonic()
        with guard.connect() as c:
            row = c.execute(
                "SELECT phase,worker_token::text FROM v2_execution_routes WHERE tenant_id=%s AND environment='SANDBOX'",
                (tenant,),
            ).fetchone()
        if (
            row is None
            or row[1] != binding.worker_token
            or row[0] not in {"ACTIVE", "DRAINING"}
        ):
            return
        degraded = False
        try:
            result = supervisor.run_once(stop_requested=stop.is_set)
            if not isinstance(result, dict) or result.get("status") != "CLEAR":
                raise ValueError("MANAGEMENT_CYCLE_NOT_CLEAR")
            channel.send("CYCLE")
            diagnostics.succeeded()
        except Exception as exc:  # noqa: BLE001 - retain only redacted diagnostics
            degraded = True
            diagnostics.failed(exc)
            channel.send("DEGRADED")
        stop.wait(
            cycle_delay(
                config.daemon.interval_seconds,
                started=started,
                finished=time.monotonic(),
                degraded=degraded,
            )
        )


def child_main(tenant, target, values, stop, release, channel):
    maintenance = None
    cache = archive = process = None
    try:
        cycle_diagnostics = _CycleDiagnostics(
            int(values.get("V2_CONTROLLER_CYCLE_DIAGNOSTIC_INTERVAL_SECONDS", "60"))
        )
        mode = _worker_mode(values)
        if mode == "MANAGEMENT_WITH_SETTLEMENT":
            raise ValueError("DIRECT_WORKER_GROUP_OWNER_REQUIRED")
        if mode == "MANAGEMENT_ONLY":
            _management_child(
                tenant, target, values, stop, release, channel, cycle_diagnostics
            )
            return
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
        guard = create_execution_guard(
            connect,
            tenant_id=tenant,
            environment="SANDBOX",
            registry_id=target["target_registry"],
            binding_version=target["binding_version"],
            epoch=target["epoch"],
            worker_token=target["worker_token"],
        )
        # Verify real deployment ACLs using the exact fencing role and row locks.
        # STARTING is permitted here, but every exchange write still needs the
        # ordinary ACTIVE/MANAGE fence after the controller releases this child.
        guard.preflight()
        sink = TelegramOperationalSink(
            token=notifications["TG_NOTIFY_TOKEN"],
            chat_id=notifications["TG_NOTIFY_CHAT_ID"],
            alerts_chat_id=values.get("V2_TG_ALERT_CHAT_ID"),
            environment="SANDBOX",
        )
        from services.v2_reentry_archive import ReentryArchive

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
            reentry_archive=ReentryArchive.from_mapping(values),
            stop=stop,
            telegram_sink=sink,
            clock_ms=lambda: time.time_ns() // 1000000,
            monotonic_ms=lambda: time.monotonic_ns() // 1000000,
            execution_guard=guard,
            shared_market=values.get("V2_SHARED_MARKET", "false") == "true",
        )
        channel.send("READY")
        while not release.wait(0.5):
            if stop.is_set():
                return
        from services.v2_signal_maintenance import SignalMaintenance

        maintenance = SignalMaintenance(
            process.daemon.pipeline.schedulers,
            settings=lambda: PolicyStore(connect, scope).read().values,
            stop=stop,
        )
        process.daemon.pipeline.background_maintenance = maintenance
        maintenance.start()
        while not stop.is_set():
            cycle_started = time.monotonic()
            with guard.connect() as c:
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
            degraded = False
            try:
                process.daemon.run_once()
                channel.send("CYCLE")
                cycle_diagnostics.succeeded()
            except Exception as exc:  # noqa: BLE001 - retry without exception payloads
                degraded = True
                cycle_diagnostics.failed(exc)
                channel.send("DEGRADED")
            stop.wait(
                cycle_delay(
                    config.daemon.interval_seconds,
                    started=cycle_started,
                    finished=time.monotonic(),
                    degraded=degraded,
                )
            )
    except Exception as exc:  # noqa: BLE001 - only a fixed SQLSTATE classification crosses IPC
        # No exception message, locals, SQL, credentials or transport URL.
        frames = []
        cursor = exc.__traceback__
        while cursor is not None:
            frames.append(
                {
                    "function": cursor.tb_frame.f_code.co_name,
                    "line": cursor.tb_lineno,
                }
            )
            cursor = cursor.tb_next
        print(
            json.dumps(
                {
                    "component": "execution-worker-failure",
                    "error_type": type(exc).__name__,
                    "frames": frames[-5:],
                }
            ),
            flush=True,
        )
        try:
            channel.send(
                "FAILED:EXECUTION_GUARD_PERMISSION_DENIED"
                if getattr(exc, "sqlstate", None) == "42501"
                else "FAILED"
            )
        except Exception:  # noqa: BLE001,S110 - closed IPC channel must not disclose context
            pass
    finally:
        if maintenance is not None:
            maintenance.close()
        if (
            process is not None
            and getattr(process, "reentry_archive", None) is not None
        ):
            process.reentry_archive.close()
        if archive is not None:
            archive.close()
        if cache is not None:
            cache.close()
        channel.close()


class ChildWorker:
    def __init__(self, tenant, target, values):
        self.mode = _worker_mode(values)
        if self.mode == "MANAGEMENT_WITH_SETTLEMENT":
            raise ValueError("DIRECT_WORKER_GROUP_OWNER_REQUIRED")
        self.startup_failure = None
        self.last_progress = time.monotonic()
        self.last_contact = self.last_progress
        self.progress_timeout = int(
            values.get("V2_CONTROLLER_PROGRESS_TIMEOUT_SECONDS", "300")
        )
        if not 30 <= self.progress_timeout <= 3600:
            raise ValueError("BOUNDED_PROGRESS_TIMEOUT_REQUIRED")
        grace = values.get("V2_CONTROLLER_STOP_GRACE_SECONDS", "30")
        if (
            not isinstance(grace, str)
            or not grace.isascii()
            or not grace.isdecimal()
            or not 1 <= int(grace) <= 60
        ):
            raise ValueError("BOUNDED_STOP_GRACE_REQUIRED")
        self.stop_grace = int(grace)
        self.entries_enabled = (
            values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false") == "true"
        )
        ctx = multiprocessing.get_context("spawn")
        self.stop_event, self.release_event = (
            cooperative_flag(ctx),
            cooperative_flag(ctx),
        )
        self.channel, child = ctx.Pipe(duplex=False)
        self.process = ctx.Process(
            target=child_main,
            args=(tenant, target, values, self.stop_event, self.release_event, child),
        )
        try:
            self.process.start()
        except Exception:  # noqa: BLE001 - retain a possibly started child for its owner
            if self.process.pid is None:
                self.channel.close()
                self.process.close()
                raise RuntimeError("WORKER_SPAWN_FAILED") from None
            self.startup_failure = "WORKER_START_FAILED"
            self.request_stop()
        finally:
            child.close()

    def ready(self):
        if self.startup_failure is not None:
            return False
        if not self.channel.poll(25):
            return False
        try:
            message = self.channel.recv()
        except EOFError:
            return False
        if message == "FAILED:EXECUTION_GUARD_PERMISSION_DENIED":
            self.startup_failure = "EXECUTION_GUARD_PERMISSION_DENIED"
        return message == "READY" and self.process.is_alive()

    def release(self):
        self.release_event.set()

    def alive(self):
        while self.channel.poll():
            try:
                message = self.channel.recv()
                if message == "FAILED":
                    return False
                if message in {"CYCLE", "DEGRADED"}:
                    self.last_contact = time.monotonic()
                    # A responsive error loop is alive, but is not successful
                    # business progress. Existing timeout drains entries only;
                    # it does not restart or abandon the protection worker.
                    if message == "CYCLE":
                        self.last_progress = self.last_contact
            except EOFError:
                return False
        if (
            getattr(self, "mode", "LEGACY") == "MANAGEMENT_ONLY"
            and time.monotonic() - self.last_contact > self.progress_timeout
        ):
            # Only a completed loop response is contact. A stuck management
            # process must be fenced, stopped and recovered by the controller.
            # Responsive DEGRADED cycles retain their owned protection worker.
            return False
        return self.process.is_alive()

    def healthy(self):
        return time.monotonic() - self.last_progress <= self.progress_timeout

    def request_stop(self):
        # A hint must never wait for a semaphore held by a dead child.
        self.stop_event.set()

    def stop(self):
        self.request_stop()
        self.process.join(self.stop_grace)
        forced = False
        if self.process.is_alive():
            forced = True
            self.process.terminate()
            self.process.join(5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(5)
        stopped = not self.process.is_alive()
        print(
            json.dumps(
                {
                    "component": "execution-worker-stop",
                    "forced": forced,
                    "stopped": stopped,
                    "grace_seconds": self.stop_grace,
                }
            ),
            flush=True,
        )
        return stopped
