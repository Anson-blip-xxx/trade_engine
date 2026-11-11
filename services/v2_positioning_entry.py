"""Independent public positioning snapshots. No order or credential authority."""

import json
import os
import shutil
import time
from collections import Counter
from contextlib import contextmanager

from v2_core.database import connection_factory

VERSION = "positioning-collection-job-v1"
GIB = 1024**3


def _bounded(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("POSITIONING_JOB_BOUNDS")
    return value


def run_batch(
    store,
    market,
    *,
    now_ms,
    batch_symbols=8,
    clock_ms=lambda: time.time_ns() // 1_000_000,
    free_disk_bytes=None,
    minimum_free_gib=10,
    describe=None,
):
    """One bounded research batch; unavailable symbols remain visible."""
    _bounded(now_ms, 0, 2**53 - 1)
    _bounded(batch_symbols, 1, 12)
    _bounded(minimum_free_gib, 4, 100)
    if describe is None:
        from v2_core.positioning_evidence import describe
    report = {
        "version": VERSION,
        "status": "COMPLETE",
        "selected": 0,
        "attempted": 0,
        "inserted": 0,
        "deduplicated": 0,
        "failed": 0,
        "pruned": 0,
        "errors": {},
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
    }
    errors = Counter()
    observed, unknown, unknown_windows = Counter(), Counter(), Counter()
    try:
        report["pruned"] = store.prune(now_ms, limit=500)
    except Exception:  # noqa: BLE001 - research cleanup never touches execution
        errors["POSITIONING_RETENTION_FAILED"] += 1
    free = shutil.disk_usage("/").free if free_disk_bytes is None else free_disk_bytes
    if type(free) is not int or free < minimum_free_gib * GIB:
        report["status"] = "DEFERRED"
        errors["POSITIONING_DISK_RESERVE"] += 1
        selected = []
    else:
        try:
            selected = store.select_symbols(now_ms, limit=batch_symbols)
            if not isinstance(selected, list) or len(selected) > batch_symbols:
                raise ValueError("POSITIONING_SELECTION_BOUND")
            symbols = [x["symbol"] for x in selected]
            if len(set(symbols)) != len(symbols):
                raise ValueError("POSITIONING_SELECTION_DUPLICATE")
            report["selected"] = len(selected)
            if not selected:
                report["status"] = "DEFERRED"
                errors["POSITIONING_NO_ELIGIBLE_SELECTION"] += 1
        except Exception:  # noqa: BLE001 - sanitized research boundary
            selected = []
            report["status"] = "DEFERRED"
            errors["POSITIONING_SELECTION_UNAVAILABLE"] += 1
    for item in selected:
        report["attempted"] += 1
        try:
            # All symbols share the batch event cutoff and network budget.
            bundle = market.collect(item["symbol"], now_ms=now_ms)
            finished = bundle["finished_at_ms"]
            features = describe(bundle, cutoff_ms=finished, as_of_ms=finished)
            saved = store.save(
                bundle,
                slot_ms=bundle["started_at_ms"] // 300_000 * 300_000,
                membership=item["membership"],
                features=features,
            )
            report["inserted" if saved["inserted"] else "deduplicated"] += 1
            for name, component in features.get("components", {}).items():
                if component.get("status") == "OBSERVED":
                    observed[name] += 1
                else:
                    unknown[name] += 1
                for window, value in (component.get("features") or {}).items():
                    if (
                        window.startswith("window_")
                        and isinstance(value, dict)
                        and value.get("status") != "OBSERVED"
                    ):
                        unknown_windows[f"{name}.{window}"] += 1
        except Exception:  # noqa: BLE001 - one bad symbol cannot poison its peers
            report["failed"] += 1
            errors["POSITIONING_SYMBOL_UNAVAILABLE"] += 1
        if getattr(market, "halt_reason", None):
            errors["POSITIONING_PUBLIC_BUDGET_DEFERRED"] += 1
            report["status"] = "DEFERRED"
            break
    if report["status"] == "COMPLETE" and (errors or unknown or unknown_windows):
        report["status"] = "PARTIAL"
    report.update(
        errors=dict(errors),
        observed_components=dict(observed),
        unknown_components=dict(unknown),
        unknown_windows=dict(unknown_windows),
        deferred_symbols=report["selected"] - report["attempted"],
    )
    completed = clock_ms()
    try:
        store.record_run(
            started_at_ms=now_ms,
            completed_at_ms=max(now_ms, completed),
            status=report["status"],
            coverage=report,
            error_code=next(iter(errors), None),
        )
    except Exception:  # noqa: BLE001 - leave an observable journal diagnostic
        report["status"] = "DEFERRED"
        report["errors"]["POSITIONING_RUN_RECORD_UNAVAILABLE"] = 1
    return report


def run(values=None):
    values = os.environ if values is None else values
    from services.v2_positioning_market import PositioningMarket
    from services.v2_positioning_store import PositioningStore
    from v2_core.shared_market import HostBudget

    base = connection_factory(values["V2_POSITIONING_DSN"], schema="trade_v2")

    @contextmanager
    def connect():
        with base() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='200ms'")
            yield conn

    with connect() as lock:
        acquired = lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('positioning-collection-job-v1',0))"
        ).fetchone()[0]
        lock.commit()
        if not acquired:
            return {
                "version": VERSION,
                "status": "ALREADY_RUNNING",
                "execution_authorized": False,
            }
        try:
            batch = _bounded(
                int(values.get("V2_POSITIONING_BATCH_SYMBOLS", "8")), 1, 12
            )
            seconds = _bounded(
                int(values.get("V2_POSITIONING_MAX_SECONDS", "90")), 30, 180
            )
            requests = _bounded(
                int(values.get("V2_POSITIONING_MAX_REQUESTS", "64")), 1, 100
            )
            reserve = _bounded(
                int(values.get("V2_POSITIONING_MIN_FREE_GIB", "10")), 4, 100
            )
            market = PositioningMarket(
                HostBudget(connect, environment="LIVE", lane="market"),
                max_seconds=seconds,
                max_requests=requests,
            )
            return run_batch(
                PositioningStore(connect),
                market,
                now_ms=time.time_ns() // 1_000_000,
                batch_symbols=batch,
                minimum_free_gib=reserve,
            )
        finally:
            # Session close also releases the lock after a timeout or crash.
            lock.execute(
                "SELECT pg_advisory_unlock(hashtextextended('positioning-collection-job-v1',0))"
            )


def main():
    try:
        report = run()
    except Exception:  # noqa: BLE001 - never log DSNs or raw server diagnostics
        report = {
            "version": VERSION,
            "status": "DEFERRED",
            "reason": "POSITIONING_JOB_UNAVAILABLE",
            "execution_authorized": False,
        }
    print(json.dumps(report, separators=(",", ":"), sort_keys=True), flush=True)
    return 1 if report["status"] == "DEFERRED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
