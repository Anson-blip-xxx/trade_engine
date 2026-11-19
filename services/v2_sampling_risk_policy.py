"""Dry-run or CAS-apply the fixed SANDBOX sampling risk ceilings.

Only sizing.risk_fraction and portfolio_risk.max_fraction can change. All
identity arguments are explicit; no credentials or exchange I/O are used.
Execution requires the reviewed policy version/digest and exact ACTIVE route.
"""

import argparse
import json
import re
from contextlib import nullcontext
from dataclasses import asdict

from services.v2_testnet_inventory import deployment_database
from v2_core.account_registry import uid
from v2_core.account_risk import AccountScope
from v2_core.execution_routes import ExecutionRoutes
from v2_core.runtime_policy import PolicyStore
from v2_core.sampling_risk import build_sampling_risk_proof

DEPLOYMENT_ROUTE_ROLE = "trade_v2_execution_operator"


def _route_with_role(conn, scope, *, route_role, **binding):
    """Borrow existing route privileges without releasing transaction locks.

    The business role owns policy writes; the non-inherited controller role
    owns registry/route reads and row-lock privileges. Restore the exact prior
    role before policy CAS on this same connection. A failed route check rolls
    back the enclosing transaction, including its transaction-local role.
    """
    prior_role = None
    if route_role is not None:
        from psycopg import sql

        prior_role = conn.execute("SELECT current_user").fetchone()[0]
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(route_role)))
    result = _bound_route(conn, scope, **binding)
    if prior_role is not None:
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(prior_role)))
    return result


def _bound_route(conn, scope, *, tenant, registry, binding_version, epoch):
    # Controller and route requests take this lock before binding/route rows.
    ExecutionRoutes._lock(conn, tenant, scope.environment)
    ExecutionRoutes._binding(conn, tenant, registry, scope.environment, binding_version)
    binding = conn.execute(
        "SELECT exchange,account_id,environment,product FROM v2_tenant_accounts "
        "WHERE tenant_id=%s AND registry_id=%s FOR SHARE",
        (tenant, registry),
    ).fetchone()
    if binding != tuple(asdict(scope).values()):
        raise ValueError("SAMPLING_RISK_ACCOUNT_BINDING_CONFLICT")
    route = conn.execute(
        "SELECT target_registry::text,binding_version,epoch,phase "
        "FROM v2_execution_routes WHERE tenant_id=%s AND environment=%s FOR SHARE",
        (tenant, scope.environment),
    ).fetchone()
    if route != (registry, binding_version, epoch, "ACTIVE"):
        raise ValueError("SAMPLING_RISK_ACTIVE_ROUTE_CONFLICT")
    latest = conn.execute(
        "SELECT max(epoch) FROM v2_execution_route_events "
        "WHERE tenant_id=%s AND environment=%s",
        (tenant, scope.environment),
    ).fetchone()[0]
    if latest is not None and latest > epoch:
        raise ValueError("SAMPLING_RISK_PENDING_ROUTE_CHANGE")
    return {
        "tenant_id": tenant,
        "registry_id": registry,
        "binding_version": binding_version,
        "epoch": epoch,
        "phase": "ACTIVE",
    }


def main(argv=None, *, connect=None, route_role=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--expected-registry-id", required=True)
    parser.add_argument("--expected-binding-version", type=int, required=True)
    parser.add_argument("--expected-route-epoch", type=int, required=True)
    parser.add_argument("--expected-version", type=int)
    parser.add_argument("--expected-digest")
    parser.add_argument("--reason")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="default; never writes")
    args = parser.parse_args(argv)
    if args.expected_binding_version < 1 or args.expected_route_epoch < 1:
        parser.error("positive binding version and route epoch are required")
    if args.expected_version is not None and args.expected_version < 0:
        parser.error("--expected-version must be nonnegative")
    if args.expected_digest is not None and not re.fullmatch(
        r"[0-9a-f]{64}", args.expected_digest
    ):
        parser.error("--expected-digest must be the observed SHA-256 digest")
    if args.execute and (
        args.expected_version is None
        or args.expected_digest is None
        or not args.reason
        or not args.reason.strip()
    ):
        parser.error(
            "--execute requires --expected-version, --expected-digest and --reason"
        )
    tenant, registry = uid(args.tenant_id), uid(args.expected_registry_id)
    scope = AccountScope("BINANCE", args.account_id, "SANDBOX", "FUTURES")
    if connect is None:
        connect = deployment_database()
        route_role = DEPLOYMENT_ROUTE_ROLE
    if route_role is not None and (
        not isinstance(route_role, str)
        or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", route_role)
    ):
        raise ValueError("SAMPLING_RISK_ROUTE_ROLE_INVALID")
    with connect() as conn:
        conn.execute("SET LOCAL lock_timeout='250ms'")
        conn.execute("SET LOCAL statement_timeout='2s'")
        # Workers hold the protocol lock before acquiring their route guard.
        # Preserve that order and keep the proof, digest check and CAS atomic.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + scope.key,),
        )
        route = _route_with_role(
            conn,
            scope,
            route_role=route_role,
            tenant=tenant,
            registry=registry,
            binding_version=args.expected_binding_version,
            epoch=args.expected_route_epoch,
        )
        store = PolicyStore(lambda: nullcontext(conn), scope)
        before = store.read()
        if (
            args.expected_version is not None
            and before.version != args.expected_version
        ):
            raise ValueError("POLICY_VERSION_CONFLICT")
        if args.expected_digest is not None and before.digest != args.expected_digest:
            raise ValueError("POLICY_DIGEST_CONFLICT")
        proof = build_sampling_risk_proof(before, scope)
        after = before
        status = "DRY_RUN"
        if args.execute:
            status = "ALREADY_APPLIED"
            if proof["changes"]:
                after = store.patch(
                    proof["changes"],
                    expected_version=before.version,
                    reason=args.reason,
                )
                if after.digest != proof["after_digest"]:
                    raise ValueError("SAMPLING_RISK_RESULT_DIGEST_CONFLICT")
                status = "APPLIED"
        result = {
            **proof,
            "status": status,
            "route": route,
            "version": after.version,
            "digest": after.digest,
        }
    print(json.dumps(result, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
