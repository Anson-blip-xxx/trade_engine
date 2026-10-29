"""One bounded preparation batch. No Binance/TG/credential-decryption imports.

Requires explicit opt-in and a checkpoint-only DB role. Run outside the trading
loop. Each invocation resolves the currently ACTIVE tenant account afresh.
"""

import json
import os
import time
from dataclasses import asdict
from uuid import UUID

from psycopg.types.json import Jsonb

from services.v2_reentry_archive import ReentryArchive
from services.v2_reentry_checkpoints import ReentryCheckpoints
from services.v2_reentry_gate import prior_close, structure_policy
from v2_core.account_risk import AccountScope
from v2_core.database import connection_factory
from v2_core.runtime_policy import PolicyStore


def bound_scope(conn, tenant):
    tenant = str(UUID(tenant))
    row = conn.execute(
        """SELECT a.exchange,a.account_id,a.environment,a.product
        FROM v2_execution_routes r JOIN v2_tenant_accounts a
          ON a.registry_id=r.target_registry AND a.tenant_id=r.tenant_id
        WHERE r.tenant_id=%s AND r.environment='SANDBOX' AND r.phase='ACTIVE'
          AND a.environment='SANDBOX' AND a.version=r.binding_version
          AND NOT EXISTS(SELECT 1 FROM v2_account_retirements x
            WHERE x.tenant_id=a.tenant_id AND x.registry_id=a.registry_id)""",
        (tenant,),
    ).fetchone()
    return AccountScope(*row) if row else None


def prepare_batch(connect, archive, *, scope, batch=4, pages=2, clock_ms=None):
    if (
        type(batch) is not int
        or not 1 <= batch <= 10
        or type(pages) is not int
        or not 1 <= pages <= 4
    ):
        raise ValueError("REENTRY_PREPARE_BUDGET_INVALID")
    clock_ms = clock_ms or (lambda: time.time_ns() // 1000000)
    settings = PolicyStore(connect, scope).read().values
    with connect() as c:
        c.execute("SET LOCAL statement_timeout='2s'")
        targets = c.execute(
            """WITH traded AS (
              SELECT DISTINCT i.payload->>'symbol' symbol FROM v2_trade_intents i
              WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
              AND i.producer IN ('s6','s8') AND EXISTS(SELECT 1 FROM v2_orders o
                JOIN v2_fills f USING(order_id) WHERE o.episode_id=i.intent_id AND o.leg='OPEN')
            ) SELECT t.symbol FROM traded t LEFT JOIN v2_reentry_preparation_attempts a
              ON a.scope_key=%s AND a.symbol=t.symbol
            ORDER BY coalesce(a.checked_at,'-infinity'::timestamptz),t.symbol LIMIT %s""",
            (*asdict(scope).values(), scope.key, batch),
        ).fetchall()
    store = ReentryCheckpoints(connect, archive.read, scope=scope, clock_ms=clock_ms)
    counts = {}
    for (target,) in targets:
        try:
            now = clock_ms()
            with connect() as c:
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                c.execute("SET LOCAL statement_timeout='2s'")
                previous, missing = prior_close(c, scope, target, now)
            if missing:
                outcome = "UNRESOLVED"
            else:
                status, _ = store.prepare(
                    target=target,
                    previous=previous,
                    policy=structure_policy(settings),
                    now=now,
                    page_bars=settings["entry.reentry_page_bars"],
                    max_pages=pages,
                )
                outcome = status["status"]
        except Exception:  # noqa: BLE001 - bounded per-target failures must not stop other symbols
            outcome = "UNAVAILABLE"
        with connect() as c:
            c.execute("SET LOCAL statement_timeout='2s'")
            c.execute("SET LOCAL lock_timeout='500ms'")
            c.execute(
                """INSERT INTO v2_reentry_preparation_attempts(scope_key,symbol,scope,outcome)
                VALUES(%s,%s,%s,%s) ON CONFLICT(scope_key,symbol) DO UPDATE
                SET checked_at=clock_timestamp(),outcome=excluded.outcome
                WHERE v2_reentry_preparation_attempts.scope=excluded.scope""",
                (scope.key, target, Jsonb(asdict(scope)), outcome),
            )
        counts[outcome] = counts.get(outcome, 0) + 1
    return {
        "status": "BATCH_COMPLETE",
        "processed": len(targets),
        "outcomes": counts,
        "execution_authorized": False,
    }


def main():
    if os.environ.get("V2_REENTRY_PREPARE_ENABLED") != "true":
        raise ValueError("REENTRY_PREPARE_DISABLED")
    connect = connection_factory(
        os.environ["V2_REENTRY_PREPARE_DSN"], schema="trade_v2"
    )
    with connect() as c:
        c.execute("SET LOCAL statement_timeout='2s'")
        scope = bound_scope(c, os.environ["V2_REENTRY_PREPARE_TENANT"])
    if scope is None:
        print('{"status":"NO_ACTIVE_SANDBOX_ACCOUNT"}', flush=True)
        return
    archive = ReentryArchive.from_mapping(os.environ)
    try:
        print(
            json.dumps(
                prepare_batch(
                    connect,
                    archive,
                    scope=scope,
                    batch=int(os.environ.get("V2_REENTRY_PREPARE_BATCH", "4")),
                    pages=int(os.environ.get("V2_REENTRY_PREPARE_PAGES", "2")),
                )
            ),
            flush=True,
        )
    finally:
        archive.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - no DSNs, tenant IDs or raw transport diagnostics
        raise SystemExit("REENTRY_PREPARATION_FAILED") from None
