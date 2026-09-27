"""Durable desired routing and transaction-held submit fencing.

This store never starts/stops workers or infers that an account is flat. Only a
trusted controller may acknowledge activation after independently reconciling
the old process, positions, orders, accounting and target configuration.
"""

from contextlib import contextmanager
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_registry import uid
from v2_core.evidence import canonical, digest


class ExecutionRoutes:
    def __init__(self, connect):
        self.connect = connect

    @staticmethod
    def _environment(value):
        if value not in {"SANDBOX", "LIVE"}:
            raise ValueError("EXPLICIT_ENVIRONMENT_REQUIRED")
        return value

    @staticmethod
    def _result(row):
        if row is None:
            return {
                "epoch": 0,
                "phase": "STOPPED",
                "target_registry": None,
                "binding_version": None,
                "blockers": [],
                "execution_authorized": False,
            }
        return dict(
            zip(
                ("epoch", "target_registry", "binding_version", "phase", "blockers"),
                row,
                strict=True,
            ),
            execution_authorized=row[3] == "ACTIVE",
        )

    @staticmethod
    def _read(c, tenant, environment):
        actual = c.execute(
            "SELECT epoch,target_registry::text,binding_version,phase,blockers "
            "FROM v2_execution_routes WHERE tenant_id=%s AND environment=%s",
            (tenant, environment),
        ).fetchone()
        if actual and actual[3] in {"ACTIVE", "DRAINING"}:
            return actual
        pending = c.execute(
            "SELECT result FROM v2_execution_route_events WHERE tenant_id=%s AND environment=%s ORDER BY epoch DESC LIMIT 1",
            (tenant, environment),
        ).fetchone()
        if pending:
            r = pending[0]
            if actual and actual[0] >= r["epoch"]:
                return actual
            return (
                r["epoch"],
                r["target_registry"],
                r["binding_version"],
                r["phase"],
                r["blockers"],
            )
        return actual

    @staticmethod
    def _lock(c, tenant, environment):
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("execution-route:" + tenant + ":" + environment,),
        )

    @staticmethod
    def _binding(c, tenant, registry, environment, version):
        row = c.execute(
            "SELECT environment,version FROM v2_tenant_accounts "
            "WHERE tenant_id=%s AND registry_id=%s FOR SHARE",
            (tenant, registry),
        ).fetchone()
        if (
            row != (environment, version)
            or c.execute(
                "SELECT 1 FROM v2_account_retirements WHERE tenant_id=%s AND registry_id=%s",
                (tenant, registry),
            ).fetchone()
        ):
            raise ValueError("ROUTE_BINDING_REJECTED")

    def inspect(self, tenant_id, environment):
        tenant, environment = uid(tenant_id), self._environment(environment)
        with self.connect() as c:
            result = self._result(self._read(c, tenant, environment))
            latest = c.execute(
                "SELECT result FROM v2_execution_route_events WHERE tenant_id=%s AND environment=%s ORDER BY epoch DESC LIMIT 1",
                (tenant, environment),
            ).fetchone()
            result["request_epoch"] = max(
                result["epoch"], latest[0]["epoch"] if latest else 0
            )
            result["pending_request"] = (
                latest[0] if latest and latest[0]["epoch"] > result["epoch"] else None
            )
            if (
                result["pending_request"]
                and c.execute(
                    "SELECT to_regclass('v2_execution_controller_events')"
                ).fetchone()[0]
            ):
                rejection = c.execute(
                    "SELECT blockers FROM v2_execution_controller_events WHERE tenant_id=%s AND environment=%s AND epoch=%s AND phase='REJECTED' ORDER BY created_at DESC LIMIT 1",
                    (tenant, environment, result["pending_request"]["epoch"]),
                ).fetchone()
                if rejection:
                    result["pending_request"] = {
                        **result["pending_request"],
                        "phase": "REJECTED",
                        "blockers": rejection[0],
                    }
            return result

    def request(
        self,
        tenant_id,
        environment,
        *,
        target_registry,
        binding_version,
        expected_epoch,
        request_id,
    ):
        tenant, registry, request = map(uid, (tenant_id, target_registry, request_id))
        environment = self._environment(environment)
        if (
            type(expected_epoch) is not int
            or expected_epoch < 0
            or type(binding_version) is not int
            or binding_version < 1
        ):
            raise ValueError("ROUTE_VERSION_REQUIRED")
        fingerprint = digest(
            canonical(
                {
                    "environment": environment,
                    "target": registry,
                    "binding": binding_version,
                    "epoch": expected_epoch,
                }
            )
        )
        with self.connect() as c:
            self._lock(c, tenant, environment)
            prior = c.execute(
                "SELECT request_digest,result FROM v2_execution_route_events WHERE tenant_id=%s AND request_id=%s",
                (tenant, request),
            ).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ValueError("ROUTE_REQUEST_CONFLICT")
                return prior[1]
            self._binding(c, tenant, registry, environment, binding_version)
            current = self._read(c, tenant, environment)
            latest = (
                c.execute(
                    "SELECT max(epoch) FROM v2_execution_route_events WHERE tenant_id=%s AND environment=%s",
                    (tenant, environment),
                ).fetchone()[0]
                or 0
            )
            if max(current[0] if current else 0, latest) != expected_epoch:
                raise ValueError("ROUTE_EPOCH_CONFLICT")
            # Do not revoke a live worker's exit/protection capability here.
            # Active handover needs a controller-managed drain, not a DB pointer flip.
            # Queue the desired target; never modify ACTIVE/DRAINING route here.
            blockers = ["EXECUTION_CONTROLLER_NOT_ATTACHED"]
            if environment == "LIVE":
                blockers.append("LIVE_DEPLOYMENT_NOT_APPROVED")
            else:
                check = c.execute(
                    "SELECT outcome FROM v2_credential_checks WHERE tenant_id=%s AND registry_id=%s "
                    "AND binding_version=%s ORDER BY created_at DESC,check_id DESC LIMIT 1",
                    (tenant, registry, binding_version),
                ).fetchone()
                if check != ("SIGNED_READ_ACCEPTED",):
                    blockers.append("TARGET_CREDENTIAL_NOT_VERIFIED")
            # Web appends a desired request only. It has NO writes on actual routes.
            result = self._result(
                (expected_epoch + 1, registry, binding_version, "BLOCKED", blockers)
            )
            c.execute(
                "INSERT INTO v2_execution_route_events(event_id,tenant_id,environment,epoch,request_id,operation,request_digest,result) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(uuid4()),
                    tenant,
                    environment,
                    result["epoch"],
                    request,
                    "REQUEST",
                    fingerprint,
                    Jsonb(result),
                ),
            )
            return result

    @contextmanager
    def submit_guard(
        self,
        *,
        tenant_id,
        environment,
        registry_id,
        binding_version,
        epoch,
        worker_token,
        operation="OPEN",
    ):
        """Hold the route through actual bounded I/O; never use a cached boolean."""
        tenant, registry, token = map(uid, (tenant_id, registry_id, worker_token))
        if (
            type(binding_version) is not int
            or binding_version < 1
            or type(epoch) is not int
            or epoch < 1
            or operation not in {"OPEN", "MANAGE"}
        ):
            raise ValueError("EXECUTION_FENCED")
        environment = self._environment(environment)
        with self.connect() as c:
            row = c.execute(
                "SELECT target_registry::text,binding_version,epoch,worker_token::text,phase "
                "FROM v2_execution_routes WHERE tenant_id=%s AND environment=%s FOR SHARE",
                (tenant, environment),
            ).fetchone()
            if (
                row is None
                or row[:4] != (registry, binding_version, epoch, token)
                or (
                    row[4] != "ACTIVE"
                    and not (row[4] == "DRAINING" and operation == "MANAGE")
                )
            ):
                raise ValueError("EXECUTION_FENCED")
            self._binding(c, tenant, registry, environment, binding_version)
            yield
