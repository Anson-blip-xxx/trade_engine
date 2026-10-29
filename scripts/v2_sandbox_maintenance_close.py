"""Explicit, evidence-preserving BR Testnet reduction; never clears old orders.

Run as the existing service user in a new systemd credential-bearing unit. The
controller must already be stopped. No env-file credentials, route mutations,
order cancellation, leverage changes, OPEN or automatic write retries exist.
"""

import argparse
import json
import re
import subprocess
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from services.v2_execution_guard import execution_guard_connection
from services.v2_managed_testnet import database, private_for, scope_for
from services.v2_testnet_daemon_entry import PrivateRatePermit
from services.v2_testnet_scoped_cleanup import ScopedCleanup
from v2_core.execution_routes import ExecutionRoutes
from v2_core.ledger import amount
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.shared_market import HostBudget
from v2_core.state import BusinessState
from v2_core.transport import BinanceSignedTransport, ExchangeTransportError

CONTROLLER = "trade-v2-execution-controller.service"
RUNNERS = (
    CONTROLLER,
    "trade-v2-testnet-daemon.service",
    "trade-v2-testnet-watchdog.service",
)
TERMINAL_PROTECTION = frozenset({"CANCELED", "EXPIRED", "REJECTED", "FINISHED"})
PROTECTION_STATUSES = TERMINAL_PROTECTION | {"NEW", "TRIGGERING", "TRIGGERED"}
READS = frozenset(
    {
        "/fapi/v3/positionRisk",
        "/fapi/v1/openOrders",
        "/fapi/v1/openAlgoOrders",
        "/fapi/v1/positionSide/dual",
        "/fapi/v1/order",
        "/fapi/v1/userTrades",
        "/fapi/v1/algoOrder",
    }
)
SAFE_FAILURES = frozenset(
    {
        "UNIQUE_SANDBOX_ROUTE_REQUIRED",
        "DRAINING_ROUTE_BINDING_REQUIRED",
        "TRADING_RUNNER_NOT_STOPPED",
        "CONTROLLER_ALREADY_RUNNING",
        "ACCOUNT_BUSY",
        "ROUTE_BINDING_REJECTED",
        "EXACT_MAINTENANCE_PLAN_REQUIRED",
        "EXACT_MAINTENANCE_WRITE_DENIED",
        "MAINTENANCE_PREFLIGHT_FAILED",
        "OUTSIDE_APPROVED_SCOPE",
        "INCOMPLETE_ACCOUNT_READ",
        "ORDINARY_ORDERS_PRESENT",
        "UNAPPROVED_ALGO_SET",
        "MAINTENANCE_OBSERVATION_CONFLICT",
        "PROTECTION_AUDIT_LIMIT",
        "EXPLICIT_SCOPED_APPROVAL_REQUIRED",
        "ACCOUNT_NOT_FLAT",
        "ONE_WAY_REQUIRED",
        "POSITION_CHANGED",
        "REDUCTION_ONLY",
        "APPROVAL_CONFLICT",
        "PLAN_CONFLICT",
        "ONE_APPROVED_POSITION_REQUIRED",
        "CLOSE_UNCONFIRMED",
        "INCOMPLETE_CLEANUP_FILLS",
        "FILL_IDENTITY_MISMATCH",
        "FILL_QUANTITY_MISMATCH",
        "TARGET_NOT_FLAT",
        "CONDITIONAL_ORDERS_REMAIN",
        "VERIFICATION_CONFLICT",
        "ACTION_CONFLICT",
        "CLEANUP_CONFIRMATION_CONFLICT",
        "MAINTENANCE_PLAN_REQUIRED",
    }
)


def runners_stopped(*, run=subprocess.run, cgroup_root=Path("/sys/fs/cgroup")):
    """Check both systemd state and every descendant cgroup's actual processes."""
    try:
        for unit in RUNNERS:
            result = run(
                [
                    "systemctl",
                    "show",
                    "--no-pager",
                    unit,
                    "-p",
                    "LoadState",
                    "-p",
                    "ActiveState",
                    "-p",
                    "SubState",
                    "-p",
                    "MainPID",
                    "-p",
                    "ControlGroup",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            fields = dict(line.split("=", 1) for line in result.stdout.splitlines())
            if any(
                fields.get(key) != value
                for key, value in {
                    "LoadState": "loaded",
                    "ActiveState": "inactive",
                    "SubState": "dead",
                    "MainPID": "0",
                }.items()
            ):
                return False
            expected = "/system.slice/" + unit
            if fields.get("ControlGroup") not in {"", expected}:
                return False
            group = cgroup_root / expected.lstrip("/")
            if group.is_symlink():
                return False
            if group.exists():
                files = [group / "cgroup.procs", *group.glob("**/*/cgroup.procs")]
                if any(path.is_symlink() or path.read_text().strip() for path in files):
                    return False
        return True
    except Exception:  # noqa: BLE001 - a failed system check never grants authority
        return False


def checked_route(rows, expected_epoch):
    if type(expected_epoch) is not int or expected_epoch < 1 or len(rows) != 1:
        raise ValueError("UNIQUE_SANDBOX_ROUTE_REQUIRED")
    tenant, registry, version, phase, epoch = rows[0]
    if (
        phase != "DRAINING"
        or epoch != expected_epoch
        or type(version) is not int
        or version < 1
    ):
        raise ValueError("DRAINING_ROUTE_BINDING_REQUIRED")
    return str(UUID(tenant)), {
        "target_registry": str(UUID(registry)),
        "binding_version": version,
        "phase": phase,
        "epoch": epoch,
    }


def route_rows(connection, *, locked=False):
    return connection.execute(
        "SELECT tenant_id::text,target_registry::text,binding_version,phase,epoch "
        "FROM v2_execution_routes WHERE environment='SANDBOX'"
        + (" FOR SHARE" if locked else "")
    ).fetchall()


@contextmanager
def maintenance_binding(connect, expected_epoch, stopped):
    """Same controller singleton lock plus held route and binding row locks."""
    if not stopped():
        raise ValueError("TRADING_RUNNER_NOT_STOPPED")
    with execution_guard_connection(connect)() as initial:
        tenant, target = checked_route(route_rows(initial), expected_epoch)
    with connect() as lease:
        if not lease.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
            ("execution-controller:" + tenant + ":SANDBOX",),
        ).fetchone()[0]:
            raise ValueError("CONTROLLER_ALREADY_RUNNING")
        lease.commit()
        with execution_guard_connection(connect)() as guarded:
            binding = checked_route(route_rows(guarded, locked=True), expected_epoch)
            if binding != (tenant, target) or not stopped():
                raise ValueError("DRAINING_ROUTE_BINDING_REQUIRED")
            ExecutionRoutes._binding(
                guarded,
                tenant,
                target["target_registry"],
                "SANDBOX",
                target["binding_version"],
            )

            def unchanged():
                return checked_route(route_rows(guarded), expected_epoch) == binding

            yield tenant, target, unchanged


class ExactCloseRequest:
    """Only one immutable maintenance POST; even DELETE is unavailable."""

    def __init__(self, request, *, stopped, route_unchanged):
        self.account_id, self.environment = request.account_id, request.environment
        if self.environment != "SANDBOX":
            raise ValueError("TESTNET_FUTURES_ONLY")
        self._request, self._stopped, self._route = request, stopped, route_unchanged
        self._expected, self._preflight, self._attempted = None, None, False
        self._diagnostic = None

    def bind(self, plan, preflight, *, diagnostic=None):
        if self._expected is not None or plan.get("symbol") != "BRUSDT":
            raise ValueError("EXACT_MAINTENANCE_PLAN_REQUIRED")
        actions = plan.get("actions", [])
        if len(actions) != 1 or actions[0].get("kind") != "CLOSE":
            raise ValueError("EXACT_MAINTENANCE_PLAN_REQUIRED")
        params = actions[0]["params"]
        if (
            set(params)
            != {
                "symbol",
                "side",
                "positionSide",
                "type",
                "quantity",
                "reduceOnly",
                "newClientOrderId",
                "newOrderRespType",
            }
            or params["symbol"] != "BRUSDT"
            or params["side"] not in {"BUY", "SELL"}
            or params["positionSide"] != "BOTH"
            or params["type"] != "MARKET"
            or params["reduceOnly"] != "true"
            or params["newOrderRespType"] != "RESULT"
            or not isinstance(params["newClientOrderId"], str)
            or not re.fullmatch(r"v2[0-9a-f]{32}", params["newClientOrderId"])
            or amount(params["quantity"], positive=True) <= 0
            or not callable(preflight)
            or (diagnostic is not None and not callable(diagnostic))
        ):
            raise ValueError("EXACT_MAINTENANCE_PLAN_REQUIRED")
        self._expected, self._preflight = deepcopy(params), preflight
        self._diagnostic = diagnostic

    def __call__(self, method, path, params):
        if method == "GET" and path in READS:
            if params.get("symbol", "BRUSDT") != "BRUSDT":
                raise ValueError("OUTSIDE_APPROVED_SCOPE")
            return self._request(method, path, params)
        if (
            method != "POST"
            or path != "/fapi/v1/order"
            or self._expected is None
            or params != self._expected
            or self._attempted
        ):
            raise ValueError("EXACT_MAINTENANCE_WRITE_DENIED")
        stage, transport_entered = "RUNNER_STOP_CHECK", False
        try:
            if self._stopped() is not True:
                raise ValueError("MAINTENANCE_PREFLIGHT_FAILED")
            stage = "ROUTE_BINDING_CHECK"
            if self._route() is not True:
                raise ValueError("MAINTENANCE_PREFLIGHT_FAILED")
            stage = "POSITION_PREFLIGHT"
            if self._preflight() is not True:
                raise ValueError("MAINTENANCE_PREFLIGHT_FAILED")
            stage = "PERSIST_PRE_SEND"
            if self._diagnostic is not None:
                # PG failure here is before the transport and prevents dispatch.
                self._diagnostic(
                    {
                        "event": "DISPATCH_READY",
                        "stage": stage,
                        "observed_at_ms": time.time_ns() // 1000000,
                        "transport_entered": False,
                    }
                )
            stage, transport_entered = "TRANSPORT", True
            self._attempted = True
            return self._request(method, path, deepcopy(params))
        except Exception as exc:  # Record only fixed diagnostics, then re-raise.
            detail = safe_error(exc)
            outcome = "UNKNOWN" if transport_entered else "NOT_DISPATCHED"
            if transport_entered and detail.get("category") in {
                "QUOTA_DENIED",
                "ENDPOINT_OR_WRITE_DISABLED",
            }:
                outcome = "PRE_NETWORK_DENIED"
            event = {
                "event": "DISPATCH_FAILED",
                "stage": stage,
                "observed_at_ms": time.time_ns() // 1000000,
                "transport_entered": transport_entered,
                "dispatch_certainty": outcome,
                "error": detail,
            }
            if self._diagnostic is not None:
                try:
                    self._diagnostic(event)
                except Exception:  # noqa: BLE001, S110 - never replace the first cause or retry
                    pass
            raise


def save_observation(cleanup, name, payload):
    key = cleanup.key({"key": name})
    current = cleanup.store.read(key)
    version = current.version if current else 0
    result = cleanup.store.change(
        key,
        expected_version=version,
        request_key=f"observation:{version + 1}",
        payload=payload,
        reason="APPROVED_SANDBOX_MAINTENANCE_OBSERVATION",
    )
    if result.code != "APPLIED":
        raise ValueError("MAINTENANCE_OBSERVATION_CONFLICT")


def account_baseline(request):
    rows = request("GET", "/fapi/v3/positionRisk", {})
    if not isinstance(rows, list):
        raise TypeError("INCOMPLETE_ACCOUNT_READ")
    active = [row for row in rows if amount(row["positionAmt"])]
    if len(active) > 1 or any(
        row["symbol"] != "BRUSDT" or row["positionSide"] != "BOTH" for row in active
    ):
        raise ValueError("OUTSIDE_APPROVED_SCOPE")
    if request("GET", "/fapi/v1/openOrders", {}) != []:
        raise ValueError("ORDINARY_ORDERS_PRESENT")
    if request("GET", "/fapi/v1/openAlgoOrders", {}) != []:
        raise ValueError("UNAPPROVED_ALGO_SET")
    return {
        "observed_at_ms": time.time_ns() // 1000000,
        "positions": [
            {
                key: row[key]
                for key in (
                    "symbol",
                    "positionSide",
                    "positionAmt",
                    "entryPrice",
                    "updateTime",
                )
            }
            for row in active
        ],
        "ordinary_orders_empty": True,
        "conditional_orders_empty": True,
    }


def parent_observations(connect, request, cleanup):
    """Known historical parents are queried separately, never canceled/resolved."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT payload FROM v2_business_state WHERE NOT deleted "
            "AND (scope->>'exchange',scope->>'account_id',scope->>'environment',scope->>'product') "
            "=(%s,%s,%s,%s) AND scope->>'namespace'='testnet-protection-v1' "
            "AND payload->'spec'->>'symbol'='BRUSDT' "
            "AND payload->>'status' NOT IN ('CANCELED','EXPIRED','REJECTED','FINISHED') LIMIT 101",
            tuple(asdict(cleanup.scope).values()),
        ).fetchall()
    if len(rows) > 100:
        raise ValueError("PROTECTION_AUDIT_LIMIT")
    reader = TestnetProtection(BusinessState(connect), request, scope=cleanup.scope)
    observations = []
    for (payload,) in rows:
        spec = ProtectionSpec(**payload["spec"])
        expected = reader.params(spec)
        result = {
            "local_status": payload["status"],
            "venue_status": "UNKNOWN",
            "episode": spec.episode,
            "client_algo_id": expected["clientAlgoId"],
        }
        try:
            raw = request(
                "GET",
                "/fapi/v1/algoOrder",
                {
                    "clientAlgoId": expected["clientAlgoId"],
                },
            )
            matches = all(
                raw.get(key) == expected[key]
                for key in (
                    "symbol",
                    "side",
                    "positionSide",
                    "clientAlgoId",
                )
            )
            if matches and raw.get("algoStatus") in PROTECTION_STATUSES:
                result.update(
                    venue_status=raw["algoStatus"],
                    identity_matches=True,
                    actual_child_id_present=bool(raw.get("actualOrderId")),
                )
        except Exception as exc:  # noqa: BLE001 - no exception text or raw response
            result["read_error"] = safe_error(exc)
        observations.append(result)
    return {
        "observed_at_ms": time.time_ns() // 1000000,
        "parents": observations,
        "unresolved_count": sum(
            row["venue_status"] not in TERMINAL_PROTECTION for row in observations
        ),
        "historical_orders_resolved": False,
    }


def execute(
    *, case_id, symbol, expected_epoch, approved, connect=None, stopped=runners_stopped
):
    case_id = str(UUID(case_id))
    if approved is not True or symbol != "BRUSDT":
        raise ValueError("EXPLICIT_SCOPED_APPROVAL_REQUIRED")
    connect = connect or database()
    with maintenance_binding(connect, expected_epoch, stopped) as (
        tenant,
        target,
        unchanged,
    ):
        scope = scope_for(tenant, target)
        private = private_for(tenant, target, scope)
        raw = BinanceSignedTransport(
            account_id=scope.account_id,
            environment="SANDBOX",
            api_key=private["api_key"],
            api_secret=private["api_secret"],
            clock_ms=lambda: time.time_ns() // 1000000,
            permit=PrivateRatePermit(
                HostBudget(connect, environment="SANDBOX"),
                entries=False,
                protection=False,
                exits=True,
            ),
            enable_trading=True,
        )
        request = ExactCloseRequest(raw, stopped=stopped, route_unchanged=unchanged)
        cleanup = ScopedCleanup(
            connect,
            request,
            scope=scope,
            case_id=case_id,
            symbol=symbol,
            approved_algos=(),
            runner_stopped=stopped,
            pause=time.sleep,
        )
        save_observation(cleanup, "account-before", account_baseline(request))
        before = parent_observations(connect, request, cleanup)
        save_observation(cleanup, "parents-before", before)
        plan = cleanup.plan()
        request.bind(
            plan,
            lambda: cleanup.preflight(plan["actions"][0]),
            diagnostic=lambda event: save_observation(
                cleanup, "dispatch-diagnostic", event
            ),
        )
        result = cleanup.run()
        after = account_baseline(request)
        if after["positions"]:
            raise ValueError("ACCOUNT_NOT_FLAT")
        save_observation(cleanup, "account-after", after)
        parents = parent_observations(connect, request, cleanup)
        save_observation(cleanup, "parents-after", parents)
        return {
            "status": result["status"],
            "closed_quantity": plan["actions"][0]["params"]["quantity"],
            "unresolved_protection_count": parents["unresolved_count"],
            "historical_orders_resolved": False,
            "new_entries_authorized": False,
        }


def safe_error(exc):
    if isinstance(exc, ExchangeTransportError):
        return exc.diagnostic_evidence()
    if type(exc) in {ValueError, TypeError} and str(exc) in SAFE_FAILURES:
        return {"category": str(exc)}
    return {"category": "MAINTENANCE_CHECK_FAILED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--symbol", required=True, choices=("BRUSDT",))
    parser.add_argument("--expected-route-epoch", required=True, type=int)
    parser.add_argument(
        "--execute-approved-cleanup", required=True, action="store_true"
    )
    args = parser.parse_args(argv)
    try:
        result = execute(
            case_id=args.case_id,
            symbol=args.symbol,
            expected_epoch=args.expected_route_epoch,
            approved=args.execute_approved_cleanup,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - never emit credential-bearing exceptions
        print(
            json.dumps(
                {"status": "CLEANUP_STOPPED", "error": safe_error(exc)}, sort_keys=True
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
