"""Least-privilege, transaction-held execution fencing for managed workers.

The guard role can inspect/lock routing and binding metadata. It cannot activate
a route, read a credential reference, or decrypt credentials. Startup preflight
uses the same connector and row locks as submission, but does not authorize an
exchange write: STARTING workers still cannot submit until the controller has
acknowledged ACTIVE. Every mutation must reacquire the real submission guard.
"""

from contextlib import contextmanager

from psycopg import sql

from v2_core.account_registry import uid
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

    def preflight(self):
        """Fail before READY if the exact route/binding cannot be locked/read.

        DRAINING is accepted for restart recovery, STARTING for first startup.
        Neither allows OPEN here or changes the regular submit_guard semantics.
        No database state is changed and no network request is made.
        """
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


def create_execution_guard(connect, **binding):
    return BoundExecutionGuard(connect, **binding)
