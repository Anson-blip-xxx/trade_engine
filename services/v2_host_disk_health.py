"""Independent disk/market freshness telemetry and DM-only transitions.

No exchange client, no account switch and no trading or cleanup operations.
Only diagnostic snapshots and notification acknowledgements are written to PG.
"""

import json
import os
import re
import shutil
import threading
import time
from uuid import uuid4

from services.v2_testnet_watchdog import HealthWatchdog, WatchdogDeliveryState
from v2_core.scheduling import diagnostic_code
from v2_core.shared_market import MarketPolicy
from v2_core.state import BusinessState, StateKey
from v2_core.telegram import TelegramOperationalSink

GIB = 1024**3


def measure(
    *,
    free_bytes,
    total_bytes,
    minimum_gib,
    warning_gib,
    recovery_gib,
    now,
    market_at,
    max_age_ms,
    previous=(),
):
    numbers = (
        free_bytes,
        total_bytes,
        minimum_gib,
        warning_gib,
        recovery_gib,
        now,
        max_age_ms,
    )
    if (
        any(type(n) is not int or n < 0 for n in numbers)
        or not 0 <= free_bytes <= total_bytes
        or not 4 <= minimum_gib < warning_gib <= 1024
        or not 1 <= recovery_gib <= 16
        or (market_at is not None and type(market_at) is not int)
    ):
        raise ValueError("HOST_DISK_MEASUREMENT_INVALID")
    active = []
    if free_bytes < minimum_gib * GIB:
        active.append("HOST_DISK_CRITICAL")
    if free_bytes < warning_gib * GIB or (
        "HOST_DISK_LOW" in previous and free_bytes < (warning_gib + recovery_gib) * GIB
    ):
        active.append("HOST_DISK_LOW")
    if market_at is None or not 0 <= now - market_at < max_age_ms:
        active.append("HOST_MARKET_STALE")
    return {
        "observed_at_ms": now,
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "minimum_free_bytes": minimum_gib * GIB,
        "warning_free_bytes": warning_gib * GIB,
        "market_observed_at_ms": market_at,
        "active": active,
        "market_reason": "MARKET_DISK_RESERVE_REQUIRED"
        if "HOST_DISK_CRITICAL" in active
        else "SHARED_MARKET_HEARTBEAT_STALE"
        if "HOST_MARKET_STALE" in active
        else "CLEAR",
    }


class HostDiskAssessment:
    def __init__(
        self,
        connect,
        *,
        warning_gib=16,
        recovery_gib=2,
        disk_usage=shutil.disk_usage,
        clock_ms=lambda: time.time_ns() // 1000000,
        maintenance_status=None,
        queue_storage_assess=None,
    ):
        self.connect, self.warning, self.recovery = connect, warning_gib, recovery_gib
        self.disk_usage, self.clock = disk_usage, clock_ms
        self.maintenance_status = maintenance_status
        self.queue_storage_assess = queue_storage_assess
        self.store = BusinessState(connect)
        self.key = StateKey(
            "BINANCE",
            "host-resources",
            "SANDBOX",
            "FUTURES",
            "host-disk-health-v1",
            "latest",
        )
        self.snapshot = None

    def __call__(self):
        policy = MarketPolicy.read(self.connect, "SANDBOX")
        disk = self.disk_usage("/var/lib/trade-engine-v2")
        previous = self.store.read(self.key)
        old = (
            json.loads(previous.payload_json)
            if previous and not previous.deleted
            else {}
        )
        with self.connect() as conn:
            row = conn.execute(
                "SELECT observed_at_ms FROM v2_market_hub_status WHERE environment='SANDBOX'"
            ).fetchone()
        snapshot = measure(
            free_bytes=disk.free,
            total_bytes=disk.total,
            minimum_gib=policy.minimum_free_disk_gib,
            warning_gib=self.warning,
            recovery_gib=self.recovery,
            now=self.clock(),
            market_at=row[0] if row else None,
            max_age_ms=policy.context_max_age_ms,
            previous=old.get("active", []),
        )
        if self.maintenance_status is not None:
            maintenance = self.maintenance_status()
            snapshot["maintenance"] = maintenance
            snapshot["active"] = sorted(
                set(snapshot["active"]) | set(maintenance["active"])
            )
        if self.queue_storage_assess is not None:
            try:
                storage = self.queue_storage_assess(
                    self.connect,
                    now_ms=snapshot["observed_at_ms"],
                    previous=old.get("queue_storage"),
                )
            except Exception:  # noqa: BLE001 - telemetry cannot erase other checks
                storage = {
                    "status": "UNKNOWN",
                    "observed_at_ms": snapshot["observed_at_ms"],
                    "active": sorted(
                        {"QUEUE_STORAGE_UNAVAILABLE"}
                        | set(old.get("queue_storage", {}).get("active", []))
                    ),
                }
            snapshot["queue_storage"] = storage
            snapshot["active"] = sorted(
                set(snapshot["active"]) | set(storage["active"])
            )
        saved = self.store.change(
            self.key,
            expected_version=previous.version if previous else 0,
            request_key=str(uuid4()),
            payload=snapshot,
            reason="HOST_DISK_ASSESSMENT",
        )
        if saved.code != "APPLIED":
            raise ValueError("HOST_DISK_STATE_CONFLICT")
        self.snapshot = snapshot
        return frozenset(snapshot["active"])


def monitor_once(
    connect,
    sink,
    *,
    warning_gib=16,
    recovery_gib=2,
    disk_usage=shutil.disk_usage,
    clock_ms=lambda: time.time_ns() // 1000000,
    maintenance_status=None,
    queue_storage_assess=None,
):
    assessment = HostDiskAssessment(
        connect,
        warning_gib=warning_gib,
        recovery_gib=recovery_gib,
        disk_usage=disk_usage,
        clock_ms=clock_ms,
        maintenance_status=maintenance_status,
        queue_storage_assess=queue_storage_assess,
    )

    def notify(event):
        return sink(
            str(uuid4()),
            "SANDBOX",
            "HOST_RESOURCE_HEALTH",
            {
                **assessment.snapshot,
                "status": event["status"],
                "error_code": event["error_code"],
            },
        )

    watchdog = HealthWatchdog(
        assessment,
        notify=notify,
        stop=threading.Event(),
        interval_seconds=60,
        delivery_state=WatchdogDeliveryState(connect, "host-resources"),
    )
    active = watchdog.run_once()
    return {
        **assessment.snapshot,
        "delivery_pending": not watchdog.loaded
        or watchdog.reported != set(active)
        or watchdog.persisted != watchdog.reported,
    }


def main():
    from services.v2_managed_health import _notification_credentials
    from services.v2_queue_storage_health import assess as queue_storage_assess
    from services.v2_retention_health import configured_assess as maintenance_status
    from services.v2_testnet_inventory import deployment_database

    try:
        destination = os.environ.get("V2_TG_ALERT_CHAT_ID", "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", destination):
            raise ValueError("HOST_HEALTH_PRIVATE_DM_REQUIRED")
        credentials = _notification_credentials(os.environ["CREDENTIALS_DIRECTORY"])
        sink = TelegramOperationalSink(
            token=credentials["TG_NOTIFY_TOKEN"],
            chat_id=destination,
            environment="SANDBOX",
        )
        connect = deployment_database()
        with connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('v2-host-disk-health',0))"
            ).fetchone()[0]:
                print('{"status":"BUSY"}')
                return
            guard.commit()
            result = monitor_once(
                connect,
                sink,
                warning_gib=int(os.environ.get("V2_HOST_DISK_WARN_GIB", "16")),
                recovery_gib=int(os.environ.get("V2_HOST_DISK_RECOVERY_GIB", "2")),
                maintenance_status=maintenance_status,
                queue_storage_assess=queue_storage_assess,
            )
        print(json.dumps(result, sort_keys=True), flush=True)
        if result["delivery_pending"]:
            raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - never print credentials or transport text
        print(
            json.dumps({"status": "UNAVAILABLE", "error_code": diagnostic_code(exc)}),
            flush=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
