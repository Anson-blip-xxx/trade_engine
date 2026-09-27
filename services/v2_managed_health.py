"""Account-routed Testnet health and TG pin maintenance, outside the trader.

Only notification credentials are loaded. The monitor has no exchange client
and cannot submit, protect, cancel, close or switch an execution route.
"""

import json
import os
import stat
import time
from dataclasses import asdict
from pathlib import Path

from services.v2_dependency_preflight import clickhouse_ready, redis_ready
from services.v2_testnet_watchdog import (
    DaemonHeartbeat,
    DependencyAssessment,
    HealthWatchdog,
    WatchdogDeliveryState,
    WatchdogTelegramNotifier,
)
from services.v2_trading_health import TradingHealth
from v2_core.account_registry import uid
from v2_core.account_risk import AccountScope
from v2_core.telegram import TelegramOperationalSink
from v2_core.trade_notifications import TradeLifecycleNotifications


class ManagedHealthMonitor:
    """Rebind all observers atomically; never report an old account as recovered."""

    def __init__(self, tenant, *, scope_for, factory, stop):
        self.tenant = uid(tenant)
        if not all(callable(port) for port in (scope_for, factory, stop.is_set)):
            raise ValueError("MANAGED_HEALTH_PORTS_REQUIRED")
        self.scope_for, self.factory, self.stop = scope_for, factory, stop
        self.identity = self.scope = self.watchdog = None
        self.lifecycle = frozenset()
        self.last_failure = None

    def _clear(self):
        self.identity = self.scope = self.watchdog = None
        self.lifecycle = frozenset()

    def _failure(self, code):
        result = {"status": "UNAVAILABLE", "error_code": code}
        if self.last_failure != code:
            print(json.dumps({"component": "managed-health", **result}), flush=True)
        self.last_failure = code
        return result

    def step(self, actual):
        if self.stop.is_set() or actual is None:
            self._clear()
            return {"status": "IDLE"}
        if (
            not isinstance(actual, dict)
            or actual.get("environment", "SANDBOX") != "SANDBOX"
        ):
            self._clear()
            return self._failure("MANAGED_HEALTH_ROUTE_INVALID")
        phase = actual.get("phase")
        if phase == "STOPPED":
            self._clear()
            return {"status": "IDLE"}
        if phase not in {"STARTING", "ACTIVE", "DRAINING", "BLOCKED"}:
            self._clear()
            return self._failure("MANAGED_HEALTH_ROUTE_INVALID")
        try:
            registry = uid(actual["target_registry"])
            version = actual["binding_version"]
            if type(version) is not int or version < 1:
                raise ValueError("BINDING_VERSION_REQUIRED")
            identity = registry, version
        except Exception:  # noqa: BLE001 - route payload never enters diagnostic text
            self._clear()
            return self._failure("MANAGED_HEALTH_ROUTE_INVALID")
        if identity != self.identity:
            self._clear()
            try:
                scope = self.scope_for(
                    self.tenant,
                    {"target_registry": registry, "binding_version": version},
                )
                if not isinstance(scope, AccountScope) or (
                    scope.exchange,
                    scope.environment,
                    scope.product,
                ) != ("BINANCE", "SANDBOX", "FUTURES"):
                    raise ValueError("TESTNET_SCOPE_REQUIRED")
                watchdog = self.factory(scope, lambda: self.lifecycle)
                if not callable(getattr(watchdog, "run_once", None)):
                    raise TypeError("HEALTH_WATCHDOG_REQUIRED")
                self.scope, self.watchdog, self.identity = scope, watchdog, identity
            except Exception:  # noqa: BLE001 - no raw DB/credential/path details
                return self._failure("MANAGED_HEALTH_BINDING_UNAVAILABLE")
        self.lifecycle = (
            frozenset({"EXECUTION_CONTROLLER_BLOCKED"})
            if phase == "BLOCKED"
            else frozenset()
        )
        blockers = actual.get("blockers")
        if (
            phase == "BLOCKED"
            and isinstance(blockers, list)
            and "EXECUTION_GUARD_PERMISSION_DENIED" in blockers
        ):
            self.lifecycle |= frozenset({"EXECUTION_GUARD_PERMISSION_DENIED"})
        try:
            active = self.watchdog.run_once()
            result = {
                "status": "MONITORED",
                "account_scope": asdict(self.scope),
                "active": sorted(active),
            }
        except Exception:  # noqa: BLE001 - retry next supervisor tick, no trading mutation
            return self._failure("MANAGED_HEALTH_CHECK_UNAVAILABLE")
        if self.last_failure is not None:
            print('{"component":"managed-health","status":"RECOVERED"}', flush=True)
        self.last_failure = None
        return result


def _notification_credentials(directory):
    path = Path(directory) / "v2-notifications"
    details = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISREG(details.st_mode)
        or details.st_mode & 0o077
        or not 1 <= details.st_size <= 65536
    ):
        raise ValueError("PROTECTED_NOTIFICATION_CREDENTIAL_REQUIRED")
    result = json.loads(path.read_bytes())
    if (
        not isinstance(result, dict)
        or set(result) != {"TG_NOTIFY_TOKEN", "TG_NOTIFY_CHAT_ID"}
        or any(not isinstance(value, str) or not value for value in result.values())
    ):
        raise ValueError("NOTIFICATION_FIELDS_REQUIRED")
    return result


def build_managed_health(tenant, values, stop):
    """Compose lazily, so monitor failures are diagnosable and independently retryable."""
    from services.v2_managed_testnet import database, scope_for

    configuration = dict(values)

    def factory(scope, lifecycle):
        credentials = _notification_credentials(os.environ["CREDENTIALS_DIRECTORY"])
        # An explicit DM destination is mandatory here: alerts must not fall back
        # to the trade-only room when the managed configuration is incomplete.
        alerts = configuration.get("V2_TG_ALERT_CHAT_ID")
        if not isinstance(alerts, str) or not alerts:
            raise ValueError("MANAGED_HEALTH_DM_REQUIRED")
        max_age = int(configuration.get("V2_WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS", "180"))
        connect = database()
        heartbeat = DaemonHeartbeat(
            connect, account_id=scope.account_id, max_age_seconds=max_age
        )
        business = TradingHealth(
            connect,
            account_id=scope.account_id,
            tv_enabled=configuration.get("V2_ENABLE_TV_SIGNALS") == "true",
        )

        def postgres():
            with connect() as conn:
                return conn.execute("SELECT 1").fetchone() == (1,)

        assessment = DependencyAssessment(
            postgres=postgres,
            redis=redis_ready,
            clickhouse=clickhouse_ready,
            heartbeat=heartbeat,
            business=business,
        )
        sink = TelegramOperationalSink(
            token=credentials["TG_NOTIFY_TOKEN"],
            chat_id=credentials["TG_NOTIFY_CHAT_ID"],
            alerts_chat_id=alerts,
            environment="SANDBOX",
        )
        pins = TradeLifecycleNotifications(
            connect, sink, scope=scope, pin_messages=True
        )
        return HealthWatchdog(
            lambda: assessment() | lifecycle(),
            notify=WatchdogTelegramNotifier(
                sink,
                account_id=scope.account_id,
                clock_ms=lambda: time.time_ns() // 1000000,
            ),
            stop=stop,
            interval_seconds=10,
            delivery_state=WatchdogDeliveryState(connect, scope.account_id),
            maintenance=lambda: pins._retry_pins(1),
        )

    return ManagedHealthMonitor(tenant, scope_for=scope_for, factory=factory, stop=stop)
