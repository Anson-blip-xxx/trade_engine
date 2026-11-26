"""Least-privilege, transaction-held execution fencing for managed workers.

The guard role can inspect/lock routing and binding metadata. It cannot activate
a route, read a credential reference, or decrypt credentials. Startup preflight
uses the same connector and row locks as submission, but does not authorize an
exchange write: STARTING workers still cannot submit until the controller has
acknowledged ACTIVE. Every mutation must reacquire the real submission guard.
"""

from contextlib import contextmanager
from dataclasses import asdict

from psycopg import sql

from v2_core.account_registry import uid
from v2_core.account_risk import AccountScope
from v2_core.execution_routes import ExecutionRoutes

EXECUTION_GUARD_ROLE = "trade_v2_execution_guard"


def execution_guard_connection(connect):
    """Enter the dedicated role only for this connection's transaction."""
    if not callable(connect):
        raise TypeError("EXECUTION_GUARD_CONNECTION_REQUIRED")

    @contextmanager
    def guarded_connection():
        with connect() as connection:
            connection.execute(
                sql.SQL("SET LOCAL ROLE {}").format(
                    sql.Identifier(EXECUTION_GUARD_ROLE)
                )
            )
            yield connection

    return guarded_connection


class BoundExecutionGuard:
    """Immutable binding checked at startup and at every signed mutation."""

    def __init__(
        self,
        connect,
        *,
        tenant_id,
        environment,
        registry_id,
        binding_version,
        epoch,
        worker_token,
    ):
        if (
            type(binding_version) is not int
            or binding_version < 1
            or type(epoch) is not int
            or epoch < 1
        ):
            raise ValueError("EXECUTION_FENCED")
        self.connect = execution_guard_connection(connect)
        self._args = {
            "tenant_id": uid(tenant_id),
            "environment": ExecutionRoutes._environment(environment),
            "registry_id": uid(registry_id),
            "binding_version": binding_version,
            "epoch": epoch,
            "worker_token": uid(worker_token),
        }
        self._routes = ExecutionRoutes(self.connect)

    def __call__(self, *, operation="OPEN"):
        return self._routes.submit_guard(**self._args, operation=operation)

    def require_transaction(self, connection, *, scope, operation="MANAGE"):
        """Fence one financial transaction, restoring its exact business role.

        The caller must set isolation first and call before intent/account locks.
        Route/registry row locks remain on this connection through the enclosing
        commit. Do not call across network work or catch failure and continue the
        transaction: a failed check requires the caller's complete rollback.
        The returned phase belongs only to this fenced transaction.
        """
        if (
            type(scope) is not AccountScope
            or scope.environment != self._args["environment"]
            or connection.autocommit
        ):
            raise ValueError("EXECUTION_TRANSACTION_BINDING_REQUIRED")
        prior_role = connection.execute("SELECT current_user").fetchone()[0]
        connection.execute(
            sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(EXECUTION_GUARD_ROLE))
        )
        phase = self._routes.require_transaction(
            connection, **self._args, operation=operation
        )
        self._require_account_scope(connection, scope)
        connection.execute(
            sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(prior_role))
        )
        return phase

    def _require_account_scope(self, connection, scope):
        bound_scope = connection.execute(
            "SELECT exchange,account_id,environment,product FROM v2_tenant_accounts "
            "WHERE tenant_id=%s AND registry_id=%s FOR SHARE",
            (self._args["tenant_id"], self._args["registry_id"]),
        ).fetchone()
        if bound_scope != tuple(asdict(scope).values()):
            raise ValueError("EXECUTION_ACCOUNT_SCOPE_MISMATCH")

    def preflight(self, *, scope=None):
        """Fail before READY if the exact route/binding cannot be locked/read.

        DRAINING is accepted for restart recovery, STARTING for first startup.
        Neither allows OPEN here or changes the regular submit_guard semantics.
        An optional exact scope also verifies the financial fence's metadata
        grants before READY. No database state or execution permit is changed.
        """
        if scope is not None and (
            type(scope) is not AccountScope
            or scope.environment != self._args["environment"]
        ):
            raise ValueError("EXECUTION_PREFLIGHT_SCOPE_REQUIRED")
        args = self._args
        with self.connect() as connection:
            row = connection.execute(
                "SELECT target_registry::text,binding_version,epoch,worker_token::text,phase "
                "FROM v2_execution_routes WHERE tenant_id=%s AND environment=%s FOR SHARE",
                (args["tenant_id"], args["environment"]),
            ).fetchone()
            if (
                row is None
                or row[:4]
                != (
                    args["registry_id"],
                    args["binding_version"],
                    args["epoch"],
                    args["worker_token"],
                )
                or row[4] not in {"STARTING", "ACTIVE", "DRAINING"}
            ):
                raise ValueError("EXECUTION_FENCED")
            self._routes._binding(
                connection,
                args["tenant_id"],
                args["registry_id"],
                args["environment"],
                args["binding_version"],
            )
            if scope is not None:
                self._require_account_scope(connection, scope)


def create_execution_guard(connect, **binding):
    return BoundExecutionGuard(connect, **binding)
