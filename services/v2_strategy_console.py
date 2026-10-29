"""Authenticated account-bound analytics and reviewed Testnet configuration.

No credentials, venue calls, worker activation or automatic parameter tuning.
Writes use a narrow DB function; the web role cannot update generic state.
"""

import time
from collections import OrderedDict
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from threading import BoundedSemaphore

from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

from v2_core.account_registry import uid
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.runtime_policy import PolicyStore, resolve
from v2_core.strategy_controls import FIELDS, budget_examples, catalog, validate_changes


def preflight(connect):
    """Check actual service permissions and valid read indexes before listening."""
    with connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        for table in (
            "v2_business_state",
            "v2_state_history",
            "v2_policy_console_events",
            "v2_decision_evidence",
            "v2_strategy_decisions",
            "v2_signal_receipts",
            "v2_inbound_signals",
            "v2_orders",
            "v2_fills",
            "v2_exchange_income",
        ):
            if not conn.execute(
                "SELECT has_table_privilege(current_user,%s,'SELECT')", (table,)
            ).fetchone()[0]:
                raise ValueError("STRATEGY_CONSOLE_READ_GRANTS_REQUIRED")
        if not conn.execute(
            "SELECT has_function_privilege(current_user,'v2_web_policy_patch(uuid,uuid,bigint,uuid,jsonb)','EXECUTE')"
        ).fetchone()[0]:
            raise ValueError("STRATEGY_CONSOLE_WRITE_FUNCTION_REQUIRED")
        for name in ("v2_receipts_analysis_recent", "v2_decisions_analysis_recent"):
            row = conn.execute(
                "SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid=to_regclass(%s)",
                (name,),
            ).fetchone()
            if not row or row[0] is not True:
                raise ValueError("STRATEGY_CONSOLE_READ_INDEXES_REQUIRED")


class StrategyConsole:
    def __init__(self, connect, *, tenant_id, actor, clock_ms=None):
        if not isinstance(actor, str) or not 1 <= len(actor) <= 120:
            raise ValueError("AUTHENTICATED_ACTOR_REQUIRED")
        self.connect, self.tenant, self.actor = connect, uid(tenant_id), actor
        self.clock = clock_ms or (lambda: time.time_ns() // 1000000)
        self._review_slot = BoundedSemaphore(1)
        self._review_cache = OrderedDict()
        self._shadow_slot = BoundedSemaphore(1)

    def _scope(self, conn, registry):
        row = conn.execute(
            """SELECT a.exchange,a.account_id,a.environment,a.product FROM v2_tenant_accounts a
            WHERE a.tenant_id=%s AND a.registry_id=%s AND NOT EXISTS (
              SELECT 1 FROM v2_account_retirements r WHERE r.tenant_id=a.tenant_id AND r.registry_id=a.registry_id)""",
            (self.tenant, uid(registry)),
        ).fetchone()
        if not row:
            raise ValueError("POLICY_ACCOUNT_NOT_FOUND")
        return AccountScope(*row)

    def shadow(self, registry, *, days=14):
        from services.v2_reentry_observation_report import observation_report

        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='250ms'")
            scope = self._scope(conn, registry)
            if not self._shadow_slot.acquire(blocking=False):
                return {
                    "registry_id": uid(registry),
                    "environment": scope.environment,
                    "status": "BUSY",
                    "execution_authorized": False,
                }
            try:
                result = observation_report(
                    conn, uid(registry), scope, days=days, now=self.clock()
                )
            finally:
                self._shadow_slot.release()
        return {
            **result,
            "registry_id": uid(registry),
            "environment": scope.environment,
        }

    def inspect(self, registry):
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL statement_timeout='3s'")
            scope = self._scope(conn, registry)
            store = PolicyStore(lambda: nullcontext(conn), scope)
            current = store.read()
            rows = conn.execute(
                """SELECT version,created_at,payload->>'reason',deleted FROM v2_state_history
                WHERE state_id=%s ORDER BY version DESC LIMIT 30""",
                (store.key.identity,),
            ).fetchall()
        from services.v2_dashboard import _value

        editable = (
            scope.environment == "SANDBOX"
            and current.version > 0
            and current.values["capital.enabled"]
            and current.values["portfolio_risk.enabled"]
        )
        return {
            "registry_id": uid(registry),
            "environment": scope.environment,
            "version": current.version,
            "digest": current.digest,
            "values": current.values,
            "catalog": catalog(),
            "editable": editable,
            "execution_authorized": False,
            "history": [
                {"version": v, "at": _value(at), "reason": reason, "deleted": deleted}
                for v, at, reason, deleted in rows
            ],
            "illustration": budget_examples(current.values),
        }

    def _preview(
        self, conn, registry, expected_version, changes, restore_version, expires=None
    ):
        now = self.clock()
        expires = now + 300000 if expires is None else expires
        if type(expires) is not int or not now < expires <= now + 300000:
            raise ValueError("POLICY_PREVIEW_EXPIRED")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("POLICY_PROVISIONED_VERSION_REQUIRED")
        scope = self._scope(conn, registry)
        if scope.environment != "SANDBOX":
            raise ValueError("POLICY_SANDBOX_ONLY")
        store = PolicyStore(lambda: nullcontext(conn), scope)
        current = store.read()
        if current.version != expected_version:
            raise ValueError("POLICY_VERSION_CONFLICT")
        if restore_version is not None:
            if (
                type(restore_version) is not int
                or not 1 <= restore_version < expected_version
                or changes
            ):
                raise ValueError("POLICY_RESTORE_VERSION_INVALID")
            row = conn.execute(
                "SELECT payload,deleted FROM v2_state_history WHERE state_id=%s AND version=%s",
                (store.key.identity, restore_version),
            ).fetchone()
            if not row or row[1]:
                raise ValueError("POLICY_RESTORE_VERSION_INVALID")
            # Restore ONLY this console's editable fields, never enable flags,
            # capital anchors, old executable schema keys or existing exit rules.
            historical = resolve(row[0]["values"])
            changes = {
                k: historical[k] for k in FIELDS if historical[k] != current.values[k]
            }
        proposed = validate_changes(current.values, changes)
        normalized = {k: format(Decimal(proposed[k]), "f") for k in changes}
        diff = {
            k: {"before": current.values[k], "after": normalized[k]} for k in normalized
        }
        token = digest(
            canonical(
                {
                    "scope": asdict(scope),
                    "registry_id": uid(registry),
                    "expected_version": expected_version,
                    "before_digest": current.digest,
                    "changes": normalized,
                    "restore_version": restore_version,
                    "preview_expires_at_ms": expires,
                }
            )
        )
        warnings = [
            "预览是固定情景定仓，不是历史收益回测，也不构成开仓许可。",
            "发布只影响后续读取该参数的定仓和资金准入；不会强制平仓或重写历史。",
        ]
        if any(
            Decimal(v["after"]) > Decimal(v["before"])
            for k, v in diff.items()
            if k not in {"capital.margin_buffer", "sizing.cost_buffer_fraction"}
        ):
            warnings.append(
                "包含扩大仓位或风险预算的调整；目前没有自动提供收益改善证据。"
            )
        if any(
            Decimal(v["after"]) < Decimal(v["before"])
            for k, v in diff.items()
            if k in {"capital.margin_buffer", "sizing.cost_buffer_fraction"}
        ):
            warnings.append("保证金或成本缓冲降低；不代表实际费用或滑点会下降。")
        return {
            "registry_id": uid(registry),
            "environment": "SANDBOX",
            "version": current.version,
            "preview_digest": token,
            "preview_expires_at_ms": expires,
            "changes": normalized,
            "diff": diff,
            "restore_version": restore_version,
            "warnings": warnings,
            "before": budget_examples(current.values),
            "after": budget_examples(proposed),
            "execution_authorized": False,
        }

    def preview(self, registry, *, expected_version, changes, restore_version=None):
        from services.v2_strategy_analytics import sizing_impact

        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL statement_timeout='3s'")
            result = self._preview(
                conn, registry, expected_version, changes, restore_version
            )
            result["historical_sizing"] = sizing_impact(
                conn, self._scope(conn, registry), result["changes"]
            )
            return result

    def apply(
        self,
        registry,
        *,
        expected_version,
        changes,
        preview_digest,
        preview_expires_at_ms,
        reason,
        request_id,
        restore_version=None,
    ):
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("POLICY_PROVISIONED_VERSION_REQUIRED")
        if (
            not isinstance(reason, str)
            or not 1 <= len(reason.strip()) <= 240
            or any(ord(c) < 32 for c in reason)
        ):
            raise ValueError("POLICY_REASON_REQUIRED")
        if not isinstance(changes, dict) or not changes or set(changes) - FIELDS.keys():
            raise ValueError("POLICY_FIELD_NOT_EDITABLE")
        document = {
            "changes": changes,
            "reason": reason.strip(),
            "actor": self.actor,
            "preview_digest": preview_digest,
            "preview_expires_at_ms": preview_expires_at_ms,
            "restore_version": restore_version,
        }
        canonical(document)  # Reject non-exact JSON numbers and secret fields.
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='1s'")
            self._scope(conn, registry)
            prior = conn.execute(
                """SELECT registry_id::text,expected_version,request,result FROM v2_policy_console_events
                WHERE tenant_id=%s AND request_id=%s""",
                (self.tenant, uid(request_id)),
            ).fetchone()
            if prior:
                if prior[:3] != (uid(registry), expected_version, document):
                    raise ValueError("POLICY_REQUEST_CONFLICT")
                return prior[3]
            preview = self._preview(
                conn,
                registry,
                expected_version,
                {} if restore_version is not None else changes,
                restore_version,
                preview_expires_at_ms,
            )
            if (
                preview["preview_digest"] != preview_digest
                or preview["changes"] != changes
            ):
                raise ValueError("POLICY_PREVIEW_MISMATCH")
            try:
                return conn.execute(
                    "SELECT v2_web_policy_patch(%s,%s,%s,%s,%s)",
                    (
                        self.tenant,
                        uid(registry),
                        expected_version,
                        uid(request_id),
                        Jsonb(document),
                    ),
                ).fetchone()[0]
            except RaiseException as exc:
                message = exc.diag.message_primary
                if message in {
                    "POLICY_VERSION_CONFLICT",
                    "POLICY_REQUEST_CONFLICT",
                    "POLICY_NO_CHANGE",
                    "POLICY_SANDBOX_ONLY",
                    "POLICY_FIELD_NOT_EDITABLE",
                    "POLICY_ACCOUNT_NOT_FOUND",
                    "POLICY_PREVIEW_EXPIRED",
                }:
                    raise ValueError(message) from None
                raise ValueError("POLICY_DATABASE_REJECTED") from None

    def analytics(self, registry, *, days=14):
        from services.v2_strategy_analytics import account_analytics

        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL statement_timeout='3s'")
            scope = self._scope(conn, registry)
            result = account_analytics(conn, scope, days=days)
        return {
            "registry_id": uid(registry),
            "environment": scope.environment,
            **result,
        }

    def review(self, registry, *, days=14):
        from services.v2_trade_review import trade_review

        if type(days) is not int or days not in {1, 7, 14, 30, 90}:
            raise ValueError("ANALYSIS_WINDOW_INVALID")
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='250ms'")
            scope = self._scope(conn, registry)  # Re-authorize even a cached report.
            identity = {"registry_id": uid(registry), "environment": scope.environment}
            if not self._review_slot.acquire(blocking=False):
                return {**identity, "status": "BUSY", "execution_authorized": False}
            try:
                key, now = (scope.key, days), time.monotonic()
                previous = self._review_cache.get(key)
                if previous and 0 <= now - previous[0] < 60:
                    return {**identity, **deepcopy(previous[1]), "cached": True}
                result = trade_review(conn, scope, days=days, now_ms=self.clock())
                result.update(
                    status="AVAILABLE",
                    cached=False,
                    cache_seconds=60,
                    query_elapsed_ms=round((time.monotonic() - now) * 1000),
                )
                self._review_cache[key] = (time.monotonic(), deepcopy(result))
                self._review_cache.move_to_end(key)
                while len(self._review_cache) > 16:
                    self._review_cache.popitem(last=False)
                return {**identity, **result}
            finally:
                self._review_slot.release()
