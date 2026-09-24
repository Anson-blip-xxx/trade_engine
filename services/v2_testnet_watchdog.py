"""Independent health supervision for the V2 Testnet stack.

The watchdog deliberately has no exchange client and no trading permissions.
Trading records are read-only. Diagnostic timers and notification confirmations
are PostgreSQL state; no local file is used as a fallback.
"""

import argparse
import json
import os
import re
import signal
import stat
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from services.v2_dependency_preflight import clickhouse_ready, redis_ready
from services.v2_testnet_inventory import config_fields, deployment_database
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds
from v2_core.state import BusinessState, StateKey
from v2_core.telegram import BUSINESS_HEALTH_MESSAGES, TelegramOperationalSink


@dataclass(frozen=True)
class WatchdogConfig:
    account_id: str
    interval_seconds: int
    heartbeat_max_age_seconds: int

    @classmethod
    def from_mapping(cls, values):
        if not hasattr(values, "get") or values.get("V2_ENVIRONMENT") != "SANDBOX":
            raise ValueError("explicit SANDBOX watchdog configuration required")
        account = values.get("V2_ACCOUNT_ID")
        if not isinstance(account, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:@/-]{1,128}", account
        ):
            raise ValueError("invalid Testnet account identity")

        def bounded_integer(name, minimum, maximum):
            raw = values.get(name)
            if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
                raise ValueError(name + " must be a bounded integer")
            value = int(raw)
            if not minimum <= value <= maximum:
                raise ValueError(name + " must be a bounded integer")
            return value

        interval = bounded_integer("V2_WATCHDOG_INTERVAL_SECONDS", 5, 60)
        max_age = bounded_integer("V2_WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS", 30, 900)
        if max_age < interval * 2:
            raise ValueError("watchdog heartbeat window is too short")
        return cls(account, interval, max_age)


def watchdog_credentials(path):
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
    return config_fields(location, {"TG_NOTIFY_TOKEN", "TG_NOTIFY_CHAT_ID"})


class DaemonHeartbeat:
    """Read the current checkpoint together with its immutable commit time."""

    def __init__(self, connect, *, account_id, max_age_seconds):
        if (
            not callable(connect)
            or not isinstance(account_id, str)
            or not account_id
            or type(max_age_seconds) is not int
            or not 30 <= max_age_seconds <= 900
        ):
            raise ValueError("explicit bounded daemon heartbeat required")
        self.connect = connect
        self.scope = AccountScope("BINANCE", account_id, "SANDBOX", "FUTURES")
        self.key = StateKey(
            **asdict(self.scope), namespace="testnet-trading-daemon-v1", key="latest"
        )
        self.max_age = max_age_seconds

    def __call__(self):
        with self.connect() as conn:
            row = conn.execute(
                """SELECT b.scope,b.payload,b.deleted,
                    extract(epoch FROM clock_timestamp()-h.created_at)
                FROM v2_business_state b
                JOIN v2_state_history h ON h.state_id=b.state_id AND h.version=b.version
                WHERE b.state_id=%s""",
                (self.key.identity,),
            ).fetchone()
        if row is None:
            return "DAEMON_HEARTBEAT_MISSING"
        scope, payload, deleted, age = row
        if deleted or scope != asdict(self.key):
            return "DAEMON_HEARTBEAT_INVALID"
        if age is None or age < 0 or age > self.max_age:
            return "DAEMON_HEARTBEAT_STALE"
        if not isinstance(payload, dict) or payload.get("account_scope") != asdict(
            self.scope
        ):
            return "DAEMON_HEARTBEAT_INVALID"
        status = payload.get("status")
        if status == "DEGRADED":
            return "DAEMON_DEGRADED"
        if status not in {"STARTING", "RUNNING"}:
            return "DAEMON_HEARTBEAT_INVALID"
        return None


class DependencyAssessment:
    """Return only fixed diagnostic codes; never endpoint or exception text."""

    def __init__(self, *, postgres, redis, clickhouse, heartbeat, business=None):
        if not all(callable(item) for item in (postgres, redis, clickhouse, heartbeat)):
            raise ValueError("explicit watchdog probes required")
        self.postgres = postgres
        self.redis = redis
        self.clickhouse = clickhouse
        self.heartbeat = heartbeat
        self.business = business

    @staticmethod
    def _ready(probe):
        try:
            return probe() is True
        except Exception:  # noqa: BLE001 - diagnostics are intentionally fixed
            return False

    def __call__(self):
        failures = set()
        postgres_ok = self._ready(self.postgres)
        if not postgres_ok:
            failures.add("POSTGRES_UNAVAILABLE")
        else:
            try:
                heartbeat = self.heartbeat()
            except Exception:  # noqa: BLE001 - fixed diagnostic only
                heartbeat = "DAEMON_HEARTBEAT_UNAVAILABLE"
            if heartbeat is not None:
                if not isinstance(heartbeat, str) or not re.fullmatch(
                    r"[A-Z][A-Z0-9_]{0,127}", heartbeat
                ):
                    heartbeat = "DAEMON_HEARTBEAT_INVALID"
                failures.add(heartbeat)
            if self.business is not None:
                try:
                    failures.update(self.business())
                except Exception:  # noqa: BLE001 - never leak SQL or credentials
                    failures.add("BUSINESS_HEALTH_UNAVAILABLE")
        if not self._ready(self.redis):
            failures.add("REDIS_UNAVAILABLE")
        if not self._ready(self.clickhouse):
            failures.add("CLICKHOUSE_UNAVAILABLE")
        return frozenset(failures)


class WatchdogTelegramNotifier:
    def __init__(self, sink, *, account_id, clock_ms):
        if (
            not isinstance(sink, TelegramOperationalSink)
            or sink.environment != "SANDBOX"
            or not isinstance(account_id, str)
            or not account_id
            or not callable(clock_ms)
        ):
            raise ValueError("explicit watchdog notification ports required")
        self.sink, self.account_id, self.clock_ms = sink, account_id, clock_ms

    def __call__(self, event):
        if (
            not isinstance(event, dict)
            or set(event) != {"status", "error_code", "account_scope"}
            or event["status"] not in {"UNAVAILABLE", "RECOVERED"}
            or not isinstance(event["error_code"], str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", event["error_code"])
            or event["account_scope"]
            != {
                "exchange": "BINANCE",
                "account_id": self.account_id,
                "environment": "SANDBOX",
                "product": "FUTURES",
            }
        ):
            return False
        observed = milliseconds(self.clock_ms())
        payload = {
            "status": event["status"],
            "error_code": event["error_code"],
            "account_id": self.account_id,
            "observed_at_ms": observed,
        }
        identity = "watchdog:" + digest(canonical(payload))
        kind = (
            "TRADING_HEALTH"
            if event["error_code"] in BUSINESS_HEALTH_MESSAGES
            else "TRADING_DAEMON"
        )
        return self.sink(identity, "SANDBOX", kind, payload)


class WatchdogDeliveryState:
    """Persist acknowledged transitions; send-before-CAS is at-least-once."""

    def __init__(self, connect, account_id):
        self.store = BusinessState(connect)
        self.key = StateKey(
            "BINANCE",
            account_id,
            "SANDBOX",
            "FUTURES",
            "watchdog-delivery-v1",
            "latest",
        )

    def load(self):
        value = self.store.read(self.key)
        return set(json.loads(value.payload_json)["reported"]) if value else set()

    def save(self, reported):
        value = self.store.read(self.key)
        version = value.version if value else 0
        result = self.store.change(
            self.key,
            expected_version=version,
            request_key=f"delivery:{version + 1}",
            payload={"reported": sorted(reported)},
            reason="WATCHDOG_DELIVERY_ACKNOWLEDGED",
        )
        if result.code != "APPLIED":
            raise ValueError("WATCHDOG_DELIVERY_CONFLICT")


class HealthWatchdog:
    def __init__(self, assess, *, notify, stop, interval_seconds, delivery_state=None):
        if (
            not callable(assess)
            or not callable(notify)
            or not callable(getattr(stop, "is_set", None))
            or not callable(getattr(stop, "wait", None))
            or type(interval_seconds) is not int
            or not 5 <= interval_seconds <= 60
        ):
            raise ValueError("explicit bounded health watchdog required")
        self.assess, self.notify, self.stop = assess, notify, stop
        self.interval = interval_seconds
        self.reported = set()
        self.delivery_state = delivery_state
        self.loaded = delivery_state is None
        self.persisted = set()

    def run_once(self):
        current = self.assess()
        if not self.loaded:
            try:
                self.persisted = self.delivery_state.load()
                self.reported.update(self.persisted)
                self.loaded = True
            except Exception:  # noqa: BLE001, S110 - dependency alarm still works without PG
                pass
        if not isinstance(current, frozenset) or any(
            not isinstance(code, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", code)
            for code in current
        ):
            current = frozenset({"WATCHDOG_ASSESSMENT_INVALID"})
        scope = {
            "exchange": "BINANCE",
            "account_id": getattr(self.notify, "account_id", ""),
            "environment": "SANDBOX",
            "product": "FUTURES",
        }
        for code in sorted(current - self.reported):
            try:
                if (
                    self.notify(
                        {
                            "status": "UNAVAILABLE",
                            "error_code": code,
                            "account_scope": scope,
                        }
                    )
                    is True
                ):
                    self.reported.add(code)
            except Exception:  # noqa: BLE001, S110 - retry next cycle
                pass
        for code in sorted(self.reported - current):
            # Missing diagnostic evidence is not evidence of recovery.
            if code.startswith("DAEMON_") and "POSTGRES_UNAVAILABLE" in current:
                continue
            if code in BUSINESS_HEALTH_MESSAGES and current & {
                "POSTGRES_UNAVAILABLE",
                "BUSINESS_HEALTH_UNAVAILABLE",
                "WATCHDOG_ASSESSMENT_INVALID",
            }:
                continue
            try:
                if (
                    self.notify(
                        {
                            "status": "RECOVERED",
                            "error_code": code,
                            "account_scope": scope,
                        }
                    )
                    is True
                ):
                    self.reported.remove(code)
            except Exception:  # noqa: BLE001, S110 - retry next cycle
                pass
        if (
            self.delivery_state is not None
            and self.loaded
            and self.reported != self.persisted
        ):
            try:
                self.delivery_state.save(self.reported)
                self.persisted = set(self.reported)
            except Exception:  # noqa: BLE001, S110 - retry persistence next cycle
                pass
        return current

    def serve(self):
        while not self.stop.is_set():
            self.run_once()
            self.stop.wait(self.interval)


def main(environ=None):
    from services.v2_trading_health import TradingHealth

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True)
    args = parser.parse_args()
    config = WatchdogConfig.from_mapping(os.environ if environ is None else environ)
    secrets = watchdog_credentials(args.credentials)
    connect = deployment_database()
    heartbeat = DaemonHeartbeat(
        connect,
        account_id=config.account_id,
        max_age_seconds=config.heartbeat_max_age_seconds,
    )

    def postgres_probe():
        with connect() as conn:
            return conn.execute("SELECT 1").fetchone() == (1,)

    assessment = DependencyAssessment(
        postgres=postgres_probe,
        redis=redis_ready,
        clickhouse=clickhouse_ready,
        heartbeat=heartbeat,
        business=TradingHealth(
            connect,
            account_id=config.account_id,
            tv_enabled=(os.environ if environ is None else environ).get(
                "V2_ENABLE_TV_SIGNALS"
            )
            == "true",
        ),
    )
    sink = TelegramOperationalSink(
        token=secrets["TG_NOTIFY_TOKEN"],
        chat_id=secrets["TG_NOTIFY_CHAT_ID"],
        environment="SANDBOX",
    )
    notifier = WatchdogTelegramNotifier(
        sink,
        account_id=config.account_id,
        clock_ms=lambda: time.time_ns() // 1000000,
    )
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    HealthWatchdog(
        assessment,
        notify=notifier,
        stop=stop,
        interval_seconds=config.interval_seconds,
        delivery_state=WatchdogDeliveryState(connect, config.account_id),
    ).serve()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never expose credentials or endpoints
        print('{"status":"TESTNET_WATCHDOG_FAILED"}', flush=True)
        raise SystemExit(1) from None
