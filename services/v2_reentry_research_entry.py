"""Explicit operator-run, read-only structural re-entry study (no daemon).

DSN comes from V2_REENTRY_RESEARCH_DSN, never source code or CLI arguments.
Outputs an engineering study, not business position state or order authority.
"""

import argparse
import json
import os
from dataclasses import asdict
from uuid import UUID

from services.v2_reentry_research import review_reentries
from services.v2_shared_market import IncrementalArchive
from v2_core.account_risk import AccountScope
from v2_core.reentry import ReentryPolicy


def select_scope(conn, *, registry=None, active_sandbox=False):
    if bool(registry) == bool(active_sandbox):
        raise ValueError("REENTRY_EXPLICIT_ACCOUNT_SELECTION_REQUIRED")
    if active_sandbox:
        rows = conn.execute("""SELECT a.exchange,a.account_id,a.environment,a.product
          FROM v2_tenant_accounts a JOIN v2_execution_routes r ON r.target_registry=a.registry_id
          WHERE r.environment='SANDBOX' AND r.phase='ACTIVE' AND NOT EXISTS
          (SELECT 1 FROM v2_account_retirements x WHERE x.tenant_id=a.tenant_id AND x.registry_id=a.registry_id)""").fetchall()
    else:
        rows = conn.execute(
            """SELECT a.exchange,a.account_id,a.environment,a.product FROM v2_tenant_accounts a
          WHERE a.registry_id=%s AND NOT EXISTS
          (SELECT 1 FROM v2_account_retirements x WHERE x.tenant_id=a.tenant_id AND x.registry_id=a.registry_id)""",
            (str(UUID(registry)),),
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("REENTRY_ACCOUNT_NOT_UNIQUE_OR_ACTIVE")
    scope = AccountScope(*rows[0])
    if scope.environment != "SANDBOX":
        raise ValueError("REENTRY_RESEARCH_SANDBOX_ONLY")
    return scope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    account = parser.add_mutually_exclusive_group(required=True)
    account.add_argument("--registry")
    account.add_argument("--active-sandbox", action="store_true")
    parser.add_argument("--days", type=int, choices=(1, 7, 14, 30, 90), default=7)
    parser.add_argument("--max-pairs", type=int, choices=range(1, 31), default=20)
    parser.add_argument("--archive-database", required=True)
    parser.add_argument("--archive-port", type=int, default=18123)
    parser.add_argument(
        "--policy-json",
        default="{}",
        help="Research parameters only; never changes running policy",
    )
    args = parser.parse_args()
    dsn = os.environ.get("V2_REENTRY_RESEARCH_DSN")
    if not dsn:
        raise ValueError("REENTRY_READ_DSN_REQUIRED")
    parameters = json.loads(args.policy_json)
    if not isinstance(parameters, dict):
        raise TypeError("REENTRY_POLICY_OBJECT_REQUIRED")
    policy = ReentryPolicy(**parameters)
    import clickhouse_connect
    import psycopg

    client = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=args.archive_port,
        database=args.archive_database,
        connect_timeout=3,
        send_receive_timeout=5,
        settings={
            "readonly": 1,
            "max_threads": 1,
            "max_execution_time": 3,
            "max_result_rows": 20000,
        },
    )
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL statement_timeout='4s'")
            conn.execute("SET LOCAL lock_timeout='250ms'")
            conn.execute("SET LOCAL search_path TO trade_v2,pg_catalog")
            scope = select_scope(
                conn, registry=args.registry, active_sandbox=args.active_sandbox
            )
            result = review_reentries(
                conn,
                scope,
                read_bars=IncrementalArchive(client, scope.environment).read,
                days=args.days,
                max_pairs=args.max_pairs,
                policy=policy,
            )
            result.update(account_scope=asdict(scope), policy=asdict(policy))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        client.close()


if __name__ == "__main__":
    main()
