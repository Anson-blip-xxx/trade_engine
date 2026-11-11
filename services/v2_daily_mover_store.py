"""PG-only research checkpoints/reports. Never writes trading business state."""

from dataclasses import asdict
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_risk import AccountScope
from v2_core.daily_movers import VERSION
from v2_core.evidence import canonical, digest


class DailyMoverStore:
    def __init__(self, connect):
        self.connect = connect

    def day(self, day):
        with self.connect() as c:
            row = c.execute(
                "SELECT catalog,catalog_observed_at_ms,ranking,status FROM v2_daily_mover_days WHERE day=%s AND method_version=%s",
                (day, VERSION),
            ).fetchone()
            return (
                None
                if row is None
                else dict(
                    zip(
                        ("catalog", "observed_at_ms", "ranking", "status"),
                        row,
                        strict=True,
                    )
                )
            )

    def start_day(self, day, catalog, observed_ms):
        with self.connect() as c:
            c.execute(
                "INSERT INTO v2_daily_mover_days(day,method_version,status,catalog,catalog_observed_at_ms) VALUES(%s,%s,'COLLECTING',%s,%s) ON CONFLICT DO NOTHING",
                (day, VERSION, Jsonb(catalog), observed_ms),
            )
        return self.day(day)

    def symbols(self, day):
        with self.connect() as c:
            rows = c.execute(
                "SELECT symbol,status,summary,hourly,minute_path,attempts,path_attempts FROM v2_daily_mover_symbols WHERE day=%s AND method_version=%s",
                (day, VERSION),
            ).fetchall()
            return {
                r[0]: dict(
                    zip(
                        (
                            "status",
                            "summary",
                            "hourly",
                            "minute_path",
                            "attempts",
                            "path_attempts",
                        ),
                        r[1:],
                        strict=True,
                    )
                )
                for r in rows
            }

    def save_symbol(self, day, symbol, summary, hourly, *, error=None):
        status = (
            "COMPLETE" if summary and summary.get("complete") is True else "UNKNOWN"
        )
        with self.connect() as c:
            c.execute(
                """INSERT INTO v2_daily_mover_symbols(day,method_version,symbol,status,summary,hourly,error_code)
             VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(day,method_version,symbol) DO UPDATE
             SET status=excluded.status,summary=excluded.summary,hourly=excluded.hourly,error_code=excluded.error_code,
                 attempts=v2_daily_mover_symbols.attempts+1,updated_at=clock_timestamp()
             WHERE v2_daily_mover_symbols.status<>'COMPLETE'""",
                (day, VERSION, symbol, status, Jsonb(summary), Jsonb(hourly), error),
            )

    def save_path(self, day, symbol, path):
        if path is None or not isinstance(path, (dict, list)):
            raise ValueError("MOVER_PATH_REQUIRED")
        with self.connect() as c:
            c.execute(
                "UPDATE v2_daily_mover_symbols SET minute_path=%s,path_attempts=path_attempts+1,updated_at=clock_timestamp() WHERE day=%s AND method_version=%s AND symbol=%s AND (minute_path IS NULL OR (minute_path->'analysis'->>'status'='UNKNOWN' AND path_attempts<3))",
                (Jsonb(path), day, VERSION, symbol),
            )

    def finish_market(self, day, ranking, *, complete):
        with self.connect() as c:
            c.execute(
                "UPDATE v2_daily_mover_days SET ranking=%s,status=%s,updated_at=clock_timestamp() WHERE day=%s AND method_version=%s AND status<>'COMPLETE'",
                (Jsonb(ranking), "COMPLETE" if complete else "PARTIAL", day, VERSION),
            )

    def scopes(self, start_ms, end_ms):
        with self.connect() as c:
            c.execute("SET TRANSACTION READ ONLY")
            rows = c.execute(
                """SELECT DISTINCT a.exchange,a.account_id,a.environment,a.product
             FROM v2_tenant_accounts a WHERE a.environment='SANDBOX' AND a.exchange='BINANCE' AND a.product='FUTURES'
             AND (EXISTS(SELECT 1 FROM v2_execution_routes r WHERE r.tenant_id=a.tenant_id AND r.target_registry=a.registry_id AND r.environment='SANDBOX' AND r.phase IN ('ACTIVE','DRAINING'))
              OR EXISTS(SELECT 1 FROM v2_trade_intents i WHERE (i.exchange,i.account_id,i.environment,i.product)=(a.exchange,a.account_id,a.environment,a.product)
                AND i.created_at>=to_timestamp(%s/1000.0) AND i.created_at<to_timestamp(%s/1000.0))
              OR EXISTS(SELECT 1 FROM v2_trade_intents i JOIN v2_episodes e ON e.episode_id=i.intent_id
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(a.exchange,a.account_id,a.environment,a.product)
                AND e.status='ACTIVE' AND i.created_at<to_timestamp(%s/1000.0))
              OR EXISTS(SELECT 1 FROM v2_trade_intents i JOIN v2_orders o ON o.episode_id=i.intent_id
                JOIN v2_fills f USING(order_id)
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(a.exchange,a.account_id,a.environment,a.product)
                AND o.leg='CLOSE' AND f.occurred_at_ms>=%s AND f.occurred_at_ms<%s)) LIMIT 21""",
                (start_ms, end_ms, end_ms, start_ms, end_ms),
            ).fetchall()
        if len(rows) > 20:
            raise ValueError("MOVER_ACCOUNT_BOUND")
        return [AccountScope(*row) for row in rows]

    @staticmethod
    def scope_key(scope):
        return digest(canonical(asdict(scope)))

    def alias(self, scope):
        with self.connect() as c:
            c.execute("SET TRANSACTION READ ONLY")
            row = c.execute(
                "SELECT display_name FROM v2_tenant_accounts WHERE (exchange,account_id,environment,product)=(%s,%s,%s,%s) ORDER BY created_at LIMIT 1",
                tuple(asdict(scope).values()),
            ).fetchone()
            return row[0] if row and isinstance(row[0], str) else None

    def prior(self, scope, day):
        with self.connect() as c:
            # Latest revision per completed day; bounded30day control window.
            rows = c.execute(
                """SELECT DISTINCT ON(day) report FROM v2_daily_mover_reports
              WHERE scope_key=%s AND method_version=%s AND day<%s::date AND day>=%s::date-30
              ORDER BY day DESC,created_at DESC LIMIT 30""",
                (self.scope_key(scope), VERSION, day, day),
            ).fetchall()
            return [row[0] for row in rows]

    def existing(self, scope, day):
        with self.connect() as c:
            row = c.execute(
                "SELECT report_id::text,report FROM v2_daily_mover_reports WHERE scope_key=%s AND day=%s AND method_version=%s ORDER BY created_at DESC LIMIT 1",
                (self.scope_key(scope), day, VERSION),
            ).fetchone()
            return row

    def pending_settlement_days(self, latest_day, now_ms):
        """At most three prior natural days; latest revision per account only.

        No trading-state scan and no date inferred from a moving retry counter.
        Legacy pending reviews get one component-upgrade refresh in this window.
        """
        if type(now_ms) is not int or not 0 <= now_ms < 2**53:
            raise ValueError("INVALID_REFRESH_CLOCK")
        with self.connect() as c:
            c.execute("SET TRANSACTION READ ONLY")
            rows = c.execute(
                """WITH latest AS MATERIALIZED (
                SELECT DISTINCT ON(day,scope_key) day,report
                FROM v2_daily_mover_reports
                WHERE method_version=%s AND day>=%s::date-3 AND day<%s::date
                ORDER BY day,scope_key,created_at DESC)
                SELECT DISTINCT day::text FROM latest
                WHERE (
                    report#>>'{settlement_refresh,version}'='daily-mover-settlement-refresh-v1'
                    AND report#>>'{settlement_refresh,status}' IN ('WAITING_SETTLEMENT','WAITING_EVIDENCE')
                    AND CASE WHEN jsonb_typeof(report#>'{settlement_refresh,retry_after_ms}')='number'
                        THEN (report#>>'{settlement_refresh,retry_after_ms}')::numeric BETWEEN 0 AND %s
                            AND (report#>>'{settlement_refresh,retry_after_ms}')::numeric<9007199254740992
                            AND trunc((report#>>'{settlement_refresh,retry_after_ms}')::numeric)
                                =(report#>>'{settlement_refresh,retry_after_ms}')::numeric
                        ELSE false END
                ) OR (
                    NOT report ? 'settlement_refresh'
                    AND EXISTS (
                        SELECT 1 FROM unnest(ARRAY['AWAITING_TERMINAL_ORDERS','AWAITING_SETTLEMENT',
                            'SETTLEMENT_EVIDENCE_INCOMPLETE','SETTLEMENT_REVISION_CHANGED']) AS pending(reason)
                        WHERE CASE WHEN jsonb_typeof(report#>ARRAY['account_day_review','coverage','excluded_reasons',reason])='number'
                            THEN (report#>>ARRAY['account_day_review','coverage','excluded_reasons',reason])::numeric BETWEEN 1 AND 500
                                AND trunc((report#>>ARRAY['account_day_review','coverage','excluded_reasons',reason])::numeric)
                                    =(report#>>ARRAY['account_day_review','coverage','excluded_reasons',reason])::numeric
                            ELSE false END
                    )
                ) ORDER BY day::text DESC LIMIT 3""",
                (VERSION, latest_day, latest_day, now_ms),
            ).fetchall()
        return [row[0] for row in rows]

    def save_report(self, scope, day, report):
        return self._save_report(self.scope_key(scope), day, report)

    def save_failure(self, day, code):
        return self._save_report(
            digest(canonical({"component": "daily-mover-review"})),
            day,
            {
                "kind": "DAILY_MOVER_JOB_FAILURE",
                "error_code": code,
                "date": day,
                "orders_submitted": False,
            },
        )

    def _save_report(self, scope_key, day, report):
        fingerprint = digest(canonical(report))
        identity = str(uuid4())
        with self.connect() as c:
            key = f"daily-mover-delivery:{day}:{VERSION}:{scope_key}"
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
            row = c.execute(
                """INSERT INTO v2_daily_mover_reports(report_id,day,method_version,scope_key,source_digest,report)
              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(day,method_version,scope_key,source_digest) DO NOTHING RETURNING report_id::text""",
                (identity, day, VERSION, scope_key, fingerprint, Jsonb(report)),
            ).fetchone()
            if row is None:
                identity = c.execute(
                    "SELECT report_id::text FROM v2_daily_mover_reports WHERE day=%s AND method_version=%s AND scope_key=%s AND source_digest=%s",
                    (day, VERSION, scope_key, fingerprint),
                ).fetchone()[0]
            c.execute(
                "INSERT INTO v2_daily_mover_delivery(report_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (identity,),
            )
        return identity

    def claim_delivery(self, identity):
        with self.connect() as c:
            row = c.execute(
                "SELECT day::text,method_version,scope_key FROM v2_daily_mover_reports WHERE report_id=%s",
                (identity,),
            ).fetchone()
            if row is None:
                return False
            key = f"daily-mover-delivery:{row[0]}:{row[1]}:{row[2]}"
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
            # A crash/timeout may already have delivered: never blindly resend.
            c.execute(
                "UPDATE v2_daily_mover_delivery SET status='UNKNOWN' WHERE report_id=%s AND status='SENDING' AND attempted_at<clock_timestamp()-interval '5 minutes'",
                (identity,),
            )
            prior = c.execute(
                """SELECT d.status FROM v2_daily_mover_delivery d
             JOIN v2_daily_mover_reports r USING(report_id)
             WHERE (r.day,r.method_version,r.scope_key)=(%s::date,%s,%s)
             AND r.report_id<>%s AND d.status IN ('SENDING','SENT','UNKNOWN') LIMIT 1""",
                (*row, identity),
            ).fetchone()
            if prior is not None:
                # Revised evidence is retained, not re-broadcast hourly.
                return False
            return (
                c.execute(
                    "UPDATE v2_daily_mover_delivery SET status='SENDING',attempts=attempts+1,attempted_at=clock_timestamp() WHERE report_id=%s AND status='PENDING' RETURNING report_id",
                    (identity,),
                ).fetchone()
                is not None
            )

    def delivered(self, identity, message_id=None):
        if message_id is not None and (type(message_id) is not int or message_id <= 0):
            message_id = None
        with self.connect() as c:
            c.execute(
                "UPDATE v2_daily_mover_delivery SET status=%s,message_id=%s,sent_at=CASE WHEN %s::bigint IS NOT NULL THEN clock_timestamp() ELSE NULL END WHERE report_id=%s AND status='SENDING'",
                ("SENT" if message_id else "UNKNOWN", message_id, message_id, identity),
            )

    def prune_raw(self, day):
        with self.connect() as c:
            return c.execute(
                """WITH old AS(SELECT day,method_version,symbol FROM v2_daily_mover_symbols WHERE day<%s::date-30 AND minute_path IS NOT NULL LIMIT 100)
               UPDATE v2_daily_mover_symbols s SET minute_path=NULL FROM old WHERE (s.day,s.method_version,s.symbol)=(old.day,old.method_version,old.symbol)""",
                (day,),
            ).rowcount
