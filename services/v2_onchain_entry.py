"""Optional on-chain research sidecar. No call from or dependency of trading.

Only confirmed asset mappings are eligible. Missing mappings and unavailable
providers are observations about research coverage, never trading permissions.
No network request starts without a healthy local circuit/registry read.
"""

import argparse
import json
import os
import shutil
import time
from collections import Counter
from contextlib import contextmanager

from v2_core.database import connection_factory

VERSION = "onchain-sidecar-job-v1"
PROVIDER = "DEXPAPRIKA"
GIB = 1024**3


def _bounded(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("ONCHAIN_JOB_BOUNDS")
    return value


def run_batch(
    store,
    market_factory,
    *,
    now_ms,
    batch_symbols=3,
    clock_ms=lambda: time.time_ns() // 1_000_000,
    free_disk_bytes=None,
    minimum_free_gib=10,
    research_symbols=(),
    describe=None,
):
    _bounded(now_ms, 0, 2**53 - 1)
    _bounded(batch_symbols, 1, 3)
    _bounded(minimum_free_gib, 4, 100)
    if describe is None:
        from v2_core.onchain_evidence import describe
    if (
        not isinstance(research_symbols, (tuple, list))
        or len(research_symbols) > batch_symbols
        or len(set(research_symbols)) != len(research_symbols)
        or any(
            not isinstance(s, str)
            or not s.endswith("USDT")
            or not s.isalnum()
            or not 5 <= len(s) <= 44
            for s in research_symbols
        )
    ):
        raise ValueError("ONCHAIN_RESEARCH_SYMBOL_INVALID")
    report = {
        "version": VERSION,
        "status": "COMPLETE",
        "selected": 0,
        "attempted": 0,
        "inserted": 0,
        "deduplicated": 0,
        "outcomes": [],
        "errors": {},
        "pruned": {},
        "scope": "EXPLICIT_RESEARCH_SMOKE"
        if research_symbols
        else "ACTIVE_SANDBOX_POSITION",
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "trading_dependency": False,
    }
    errors = Counter()
    selected = []
    try:
        report["pruned"] = store.prune(now_ms, limit=200)
        circuit = store.read_circuit(PROVIDER, now_ms)
        retry_at = circuit.get("retry_after_ms") or 0
        _bounded(retry_at, 0, 2**53 - 1)
        free = (
            shutil.disk_usage("/").free if free_disk_bytes is None else free_disk_bytes
        )
        if type(free) is not int or free < minimum_free_gib * GIB:
            report["status"] = "DEFERRED"
            errors["ONCHAIN_DISK_RESERVE"] += 1
        elif retry_at > now_ms:
            report.update(status="COOLDOWN", retry_after_ms=retry_at)
        else:
            selected = (
                [
                    {"symbol": s, "membership": ["EXPLICIT_RESEARCH_SMOKE"]}
                    for s in research_symbols
                ]
                if research_symbols
                else store.select_symbols(now_ms, limit=batch_symbols)
            )
            if (
                not isinstance(selected, list)
                or len(selected) > batch_symbols
                or len({i["symbol"] for i in selected}) != len(selected)
            ):
                raise ValueError("ONCHAIN_SELECTION_BOUND")
            report["selected"] = len(selected)
            if not selected:
                report["status"] = "IDLE"
    except Exception:  # noqa: BLE001 - no external calls when local isolation state fails
        report["status"] = "DEFERRED"
        errors["ONCHAIN_LOCAL_STATE_UNAVAILABLE"] += 1
        selected = []

    market = None
    for item in selected:
        target = item["symbol"]
        try:
            mapping = store.lookup_mapping(target, now_ms)
        except Exception:  # noqa: BLE001 - registry failure must not trigger fuzzy discovery
            errors["ONCHAIN_REGISTRY_UNAVAILABLE"] += 1
            report["status"] = "DEFERRED"
            break
        if mapping is None:
            report["outcomes"].append(
                {
                    "symbol": target,
                    "status": "UNKNOWN",
                    "reason": "UNVERIFIED_OR_EXPIRED_MAPPING",
                }
            )
            continue
        if market is None:
            try:
                market = market_factory()
            except Exception:  # noqa: BLE001 - no dependency on a research adapter
                report["status"] = "DEFERRED"
                errors["ONCHAIN_ADAPTER_UNAVAILABLE"] += 1
                break
        report["attempted"] += 1
        try:
            bundle = market.collect(
                mapping["network_id"], mapping["token_address"], now_ms=now_ms
            )
            bundle = {**bundle, "symbol": target, "mapping_id": mapping["mapping_id"]}
            features = describe(bundle, mapping, as_of_ms=bundle["finished_at_ms"])
            saved = store.save(
                bundle,
                slot_ms=bundle["started_at_ms"] // 300_000 * 300_000,
                membership=item["membership"],
                features=features,
            )
            report["inserted" if saved["inserted"] else "deduplicated"] += 1
            report["outcomes"].append(
                {
                    "symbol": target,
                    "status": features["status"],
                    "reason": None,
                    "scope": "PRIMARY_NETWORK_ONLY",
                }
            )
        except Exception:  # noqa: BLE001 - do not keep fetching after uncertain persistence
            report["outcomes"].append(
                {
                    "symbol": target,
                    "status": "UNKNOWN",
                    "reason": "ONCHAIN_OBSERVATION_UNAVAILABLE",
                }
            )
            errors["ONCHAIN_OBSERVATION_UNAVAILABLE"] += 1
            report["status"] = "DEFERRED"
            break

        halted = getattr(market, "halt_reason", None)
        try:
            if halted:
                seconds = getattr(market, "retry_after_seconds", None) or 60
                seconds = _bounded(seconds, 0, 86400)
                current = clock_ms()
                state = store.record_circuit(
                    PROVIDER,
                    now_ms=current,
                    outcome="FAILURE",
                    retry_after_ms=current + seconds * 1000,
                )
                report.update(status="DEFERRED", retry_after_ms=state["retry_after_ms"])
                errors["ONCHAIN_PROVIDER_DEFERRED"] += 1
                break
            if all(
                c.get("status") == "OBSERVED" for c in bundle["components"].values()
            ):
                store.record_circuit(PROVIDER, now_ms=clock_ms(), outcome="SUCCESS")
        except Exception:  # noqa: BLE001 - cannot safely continue without persistent cooldown
            report["status"] = "DEFERRED"
            errors["ONCHAIN_CIRCUIT_PERSISTENCE_UNAVAILABLE"] += 1
            break

    if report["status"] == "COMPLETE" and (
        errors or any(o["status"] != "OBSERVED_AGGREGATE" for o in report["outcomes"])
    ):
        report["status"] = "PARTIAL"
    report.update(
        errors=dict(errors),
        deferred_symbols=report["selected"] - len(report["outcomes"]),
        outcome_counts=dict(Counter(o["status"] for o in report["outcomes"])),
    )
    try:
        store.record_run(
            started_at_ms=now_ms,
            completed_at_ms=max(now_ms, clock_ms()),
            status=report["status"],
            coverage=report,
            error_code=next(iter(errors), None),
        )
    except Exception:  # noqa: BLE001 - fallback is a sanitized journal record, never trading state
        report["status"] = "DEFERRED"
        report["errors"]["ONCHAIN_RUN_RECORD_UNAVAILABLE"] = 1
    return report


def run(values=None, *, research_symbols=()):
    values = os.environ if values is None else values
    from services.v2_onchain_market import OnchainMarket
    from services.v2_onchain_store import OnchainStore

    base = connection_factory(values["V2_ONCHAIN_DSN"], schema="trade_v2")

    @contextmanager
    def connect():
        with base() as conn:
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout='5s'")
            yield conn

    with connect() as lock:
        acquired = lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('onchain-sidecar-job-v1',0))"
        ).fetchone()[0]
        lock.commit()
        if not acquired:
            return {
                "version": VERSION,
                "status": "ALREADY_RUNNING",
                "execution_authorized": False,
            }
        try:
            return run_batch(
                OnchainStore(connect),
                OnchainMarket,
                now_ms=time.time_ns() // 1_000_000,
                batch_symbols=_bounded(
                    int(values.get("V2_ONCHAIN_BATCH_SYMBOLS", "3")), 1, 3
                ),
                minimum_free_gib=_bounded(
                    int(values.get("V2_ONCHAIN_MIN_FREE_GIB", "10")), 4, 100
                ),
                research_symbols=research_symbols,
            )
        finally:
            lock.execute(
                "SELECT pg_advisory_unlock(hashtextextended('onchain-sidecar-job-v1',0))"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-symbol", action="append", default=[])
    args = parser.parse_args()
    try:
        report = run(research_symbols=args.research_symbol)
    except Exception:  # noqa: BLE001 - never print DSN, private fields or server bodies
        report = {
            "version": VERSION,
            "status": "DEFERRED",
            "reason": "ONCHAIN_JOB_UNAVAILABLE",
            "execution_authorized": False,
            "trading_dependency": False,
        }
    print(json.dumps(report, separators=(",", ":"), sort_keys=True), flush=True)
    return 1 if report["status"] == "DEFERRED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
