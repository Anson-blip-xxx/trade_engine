"""Opt-in SANDBOX diagnostic runner, no trades/network/credential loading.

Transaction-owned fencing rather than expiring leases: disable waits for an
in-flight scan; process/connection death rolls back all of that scan's effects.
This is an internal control plane, not public tenant authentication.
"""

from contextlib import nullcontext
from dataclasses import asdict
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_registry import uid
from v2_core.capital_journal import CapitalJournal, timestamp
from v2_core.capital_monitor import CapitalTransferMonitor, TransferMonitorPolicy
from v2_core.evidence import canonical, digest


class CapitalMonitorRunner:
    def __init__(self, connect):
        self.connect = connect

    @staticmethod
    def _scope(c, tenant, registry):
        if CapitalJournal._account(c, tenant, registry)[1] != "SANDBOX":
            raise ValueError("SANDBOX_MONITOR_ONLY")

    @staticmethod
    def _key(tenant, registry):
        return "capital-monitor-runner:" + tenant + ":" + registry

    def configure(
        self, tenant_id, registry_id, *, enabled, policy, expected_version, request_id
    ):
        tenant, registry, request = uid(tenant_id), uid(registry_id), uid(request_id)
        if (
            type(enabled) is not bool
            or not isinstance(policy, TransferMonitorPolicy)
            or type(expected_version) is not int
            or expected_version < 0
        ):
            raise ValueError("explicit versioned monitor configuration required")
        encoded = digest(
            canonical(
                {
                    "registry_id": registry,
                    "enabled": enabled,
                    "policy": asdict(policy),
                    "expected_version": expected_version,
                }
            )
        )
        with self.connect() as c:
            self._scope(c, tenant, registry)
            # Shared request namespace serializes conflicting account requests.
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("capital-monitor-config:" + tenant,),
            )
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self._key(tenant, registry),),
            )
            prior = c.execute(
                "SELECT request_digest,result FROM v2_capital_monitor_control_events WHERE tenant_id=%s AND request_id=%s",
                (tenant, request),
            ).fetchone()
            if prior:
                if prior[0] != encoded:
                    raise ValueError("MONITOR_CONFIG_REQUEST_CONFLICT")
                return prior[1]
            current = c.execute(
                "SELECT version FROM v2_capital_monitor_controls WHERE tenant_id=%s AND registry_id=%s",
                (tenant, registry),
            ).fetchone()
            version = current[0] if current else 0
            if version != expected_version:
                raise ValueError("MONITOR_CONFIG_VERSION_CONFLICT")
            result = {
                "tenant_id": tenant,
                "registry_id": registry,
                "version": version + 1,
                "enabled": enabled,
                "policy": asdict(policy),
                "execution_authorized": False,
            }
            c.execute(
                "INSERT INTO v2_capital_monitor_controls(tenant_id,registry_id,version,enabled,policy) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(tenant_id,registry_id) DO UPDATE SET version=EXCLUDED.version,enabled=EXCLUDED.enabled,policy=EXCLUDED.policy",
                (tenant, registry, version + 1, enabled, Jsonb(asdict(policy))),
            )
            c.execute(
                "INSERT INTO v2_capital_monitor_control_events(tenant_id,request_id,request_digest,result) VALUES (%s,%s,%s,%s)",
                (tenant, request, encoded, Jsonb(result)),
            )
            return result

    def run_one(self, tenant_id, registry_id, *, now_ms):
        tenant, registry = uid(tenant_id), uid(registry_id)
        timestamp(now_ms)
        with self.connect() as c:
            self._scope(c, tenant, registry)
            if not c.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                (self._key(tenant, registry),),
            ).fetchone()[0]:
                return {"status": "BUSY", "execution_authorized": False}
            config = c.execute(
                "SELECT version,enabled,policy FROM v2_capital_monitor_controls WHERE tenant_id=%s AND registry_id=%s",
                (tenant, registry),
            ).fetchone()
            if not config or not config[1]:
                return {"status": "DISABLED", "execution_authorized": False}
            policy = TransferMonitorPolicy(**config[2])
            schedule = c.execute(
                "SELECT control_version,observed_at_ms,next_run_ms FROM v2_capital_monitor_schedule WHERE tenant_id=%s AND registry_id=%s",
                (tenant, registry),
            ).fetchone()
            if schedule and now_ms < schedule[1]:
                raise ValueError("MONITOR_CLOCK_REGRESSION")
            if schedule and schedule[0] == config[0] and now_ms < schedule[2]:
                return {"status": "NOT_DUE", "execution_authorized": False}
            try:
                # Savepoint ensures a broken scan cannot commit partial health state.
                with c.transaction():
                    observed = CapitalTransferMonitor(lambda: nullcontext(c)).scan(
                        tenant, registry, now_ms=now_ms, policy=policy
                    )
                result = {
                    "status": "SCANNED",
                    "scan": observed,
                    "execution_authorized": False,
                }
                next_run = observed["next_scan_ms"]
            except Exception:  # noqa: BLE001 - retry metadata only, never raw SQL or secret-bearing errors
                result = {
                    "status": "SCAN_FAILED",
                    "error_code": "CAPITAL_MONITOR_SCAN_FAILED",
                    "execution_authorized": False,
                }
                next_run = now_ms + policy.retry_ms
            c.execute(
                "INSERT INTO v2_capital_monitor_runs(run_id,tenant_id,registry_id,control_version,observed_at_ms,result) VALUES (%s,%s,%s,%s,%s,%s)",
                (str(uuid4()), tenant, registry, config[0], now_ms, Jsonb(result)),
            )
            c.execute(
                "INSERT INTO v2_capital_monitor_schedule(tenant_id,registry_id,control_version,observed_at_ms,next_run_ms) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(tenant_id,registry_id) DO UPDATE SET control_version=EXCLUDED.control_version,observed_at_ms=EXCLUDED.observed_at_ms,next_run_ms=EXCLUDED.next_run_ms",
                (tenant, registry, config[0], now_ms, next_run),
            )
            return result

    def run_due(self, tenant_id, *, now_ms, limit=20):
        tenant = uid(tenant_id)
        timestamp(now_ms)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("bounded tenant scan batch required")
        with self.connect() as c:
            rows = c.execute(
                """SELECT a.registry_id::text FROM v2_capital_monitor_controls a
                JOIN v2_tenant_accounts r USING(tenant_id,registry_id)
                LEFT JOIN v2_capital_monitor_schedule s USING(tenant_id,registry_id)
                WHERE a.tenant_id=%s AND a.enabled AND r.environment='SANDBOX'
                AND (s.registry_id IS NULL OR s.control_version<>a.version OR s.next_run_ms<=%s)
                ORDER BY COALESCE(s.next_run_ms,0),a.registry_id LIMIT %s""",
                (tenant, now_ms, limit),
            ).fetchall()
        return [
            {"registry_id": row[0], **self.run_one(tenant, row[0], now_ms=now_ms)}
            for row in rows
        ]
