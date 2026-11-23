"""Prove a legacy lag window and seed exact IDs; never clear an alarm by hand.

Read-only unless --execute. Requires the existing SANDBOX controller credential
environment. Only the diagnostics of one health lane may be CAS-updated; no
orders, risk settings, receipts, alarm flags or recovery timers are modified.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
RUNTIME = Path("/opt/trade-engine-v2/releases/queue-health-controller-20261123-v1")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-version", type=int, required=True)
    parser.add_argument(
        "--lane",
        choices=("s6:s3", "s8:s3", "s6:tv_bridge", "s8:tv_bridge"),
        required=True,
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if (
        args.legacy_version < 1
        or Path("/opt/trade-engine-v2/execution-controller").resolve() != RUNTIME
    ):
        raise ValueError("EXPECTED_RUNTIME_AND_LEGACY_VERSION_REQUIRED")
    if (
        os.environ.get("CREDENTIALS_DIRECTORY")
        != "/run/credentials/trade-v2-execution-controller.service"
    ):
        raise ValueError("CONTROLLER_CREDENTIAL_CONTEXT_REQUIRED")
    sys.path.insert(0, str(RUNTIME))
    from services import v2_managed_testnet as managed
    from services.v2_signal_queue_health import _READY_VALID, read_signal_queue
    from services.v2_trading_health import (
        _CANDIDATE_DEPENDENCY_CODES,
        _CAPACITY_WAIT_CODES,
        _QUEUE_REASON_CODES,
    )
    from v2_core.directional import directional_events
    from v2_core.runtime_policy import PolicyStore
    from v2_core.scheduling import ADMISSION_READ_QUOTA_CODES, EXPECTED_ADMISSION_WAITS
    from v2_core.state import BusinessState, StateKey
    from v2_core.strategy import StrategyScope

    protocol = load(
        "_legacy_migration_route", REPO / "scripts/v2_profitability_release_restart.py"
    )
    protocol.CANDIDATE = RUNTIME
    route = protocol.database("capture")
    if route["phase"] != "ACTIVE":
        raise ValueError("ACTIVE_SAME_BINDING_REQUIRED")
    target = {
        "target_registry": route["registry"],
        "binding_version": route["binding"],
        "epoch": route["epoch"],
    }
    scope = managed.scope_for(route["tenant"], target)
    if scope.environment != "SANDBOX":
        raise ValueError("SANDBOX_REQUIRED")
    base = managed.database()

    @contextmanager
    def connect():
        with base() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            yield conn

    @contextmanager
    def readonly():
        with connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            yield conn

    key = StateKey(**asdict(scope), namespace="trading-health-v1", key="latest")
    with readonly() as conn:
        row = conn.execute(
            "SELECT payload FROM v2_state_history WHERE state_id=%s AND version=%s",
            (key.identity, args.legacy_version),
        ).fetchone()
    if row is None:
        raise ValueError("EXACT_LEGACY_HISTORY_REQUIRED")
    legacy = row[0]
    policy = PolicyStore(readonly, scope).read()
    current = BusinessState(readonly).read(key)
    if current is None or current.deleted or current.version <= args.legacy_version:
        raise ValueError("CURRENT_LATER_HEALTH_REQUIRED")
    old_payload = json.loads(current.payload_json)
    lane = args.lane
    if old_payload.get("account_scope") != asdict(scope) or legacy.get(
        "account_scope"
    ) != asdict(scope):
        raise ValueError("EXACT_SCOPE_REQUIRED")
    cutoff = legacy["observed_at_ms"]
    oldest = legacy["findings"]["SIGNAL_CONSUMPTION_LAG"][lane]["oldest_received_at_ms"]
    queue_seconds = policy.values["health.queue_seconds"]
    selector_clock = cutoff + queue_seconds * 1000
    lookback = math.ceil((selector_clock - oldest) / 1000) + 1
    if not 1 <= lookback <= 3600:
        raise ValueError("LEGACY_WINDOW_OUTSIDE_BOUND")
    producer, source = lane.split(":", 1)
    result = read_signal_queue(
        readonly,
        environment=scope.environment,
        source=source,
        consumer=StrategyScope(**asdict(scope), producer=producer).consumer,
        now_ms=selector_clock,
        queue_seconds=queue_seconds,
        reconcile_seconds=lookback,
        actionable_events=directional_events(producer.upper()),
        expected_waits=EXPECTED_ADMISSION_WAITS,
        admission_quota_codes=ADMISSION_READ_QUOTA_CODES,
        capacity_codes=_CAPACITY_WAIT_CODES,
        dependency_codes=_CANDIDATE_DEPENDENCY_CODES,
        reason_codes=_QUEUE_REASON_CODES,
        limit=1000,
        budget_ms=1000,
    )
    if result["row"] is None or any(result["row"][index] for index in (0, 2, 3)):
        raise ValueError("LEGACY_WINDOW_STILL_HAS_PENDING_INPUTS")
    migration = load(
        "_legacy_health_migration",
        REPO / "services/v2_queue_health_legacy_migration.py",
    )
    payload = migration.migrate_legacy_queue_gap(
        old_payload,
        legacy,
        lane=lane,
        proof_result=result,
        proof_from_ms=result["diagnostics"]["recent_from_ms"],
        proof_cutoff_ms=cutoff,
        now_ms=time.time_ns() // 1000000,
    )
    proof = payload["queue_diagnostics"][lane]["legacy_coverage_proof"]
    proof["source_health_version"] = args.legacy_version
    proof["source_health_sha256"] = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        protocol.database("capture") != route
        or PolicyStore(readonly, scope).read() != policy
    ):
        raise ValueError("CONCURRENT_ROUTE_OR_POLICY_CHANGE")
    if args.execute:

        @contextmanager
        def validated_write():
            with connect() as conn:
                if conn.execute(_READY_VALID).fetchone() != (True,):
                    raise ValueError("READY_VALIDATION_CHANGED")
                yield conn

        outcome = BusinessState(validated_write).change(
            key,
            expected_version=current.version,
            request_key=str(uuid4()),
            reason="LEGACY_QUEUE_WINDOW_PROVEN",
            payload=payload,
        )
        if outcome.code != "APPLIED":
            raise ValueError("HEALTH_CAS_CONFLICT_NOT_OVERWRITTEN")
    print(
        json.dumps(
            {
                "event": "LEGACY_QUEUE_WINDOW_PROVEN"
                if args.execute
                else "LEGACY_QUEUE_PROOF_DRY_RUN",
                "lane": lane,
                "source_health_version": args.legacy_version,
                "window_received_count": result["diagnostics"].get("recent_keys"),
                "remaining_exact_ids": len(result["diagnostics"]["lag_signal_ids"]),
                "query_ms": result["diagnostics"].get("query_ms"),
                "alarm_flags_changed": False,
                "orders_or_policy_changed": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
