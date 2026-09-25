"""Project persisted venue cash facts, never add episode PnL a second time.

Internal, unverified enrollment only. No network, public authorization, exchange
coverage certificate or spendable-balance decision is provided by this module.
"""

import re
from contextlib import nullcontext
from decimal import Decimal, localcontext
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_registry import uid
from v2_core.capital_journal import CapitalJournal, decimal_text, timestamp
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount

MAPPING = {
    "REALIZED_PNL": "REALIZED_PNL",
    "COMMISSION": "FEE",
    "FUNDING_FEE": "FUNDING",
}


class CapitalIncomeProjection:
    def __init__(self, connect):
        self.connect = connect

    def enroll_baseline(
        self,
        tenant_id,
        registry_id,
        *,
        currency,
        opening_amount,
        through_ms,
        evidence_ref,
    ):
        """Trusted operator-supplied cutoff, NOT verified venue identity/balance."""
        tenant, registry, ref = uid(tenant_id), uid(registry_id), uid(evidence_ref)
        timestamp(through_ms)
        opening = amount(opening_amount)
        if (
            opening < 0
            or not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9]{0,19}", currency)
        ):
            raise ValueError("invalid opening balance")
        with self.connect() as c:
            CapitalJournal._lock(c, tenant)
            CapitalJournal._account(c, tenant, registry)
            prior = c.execute(
                "SELECT opening_amount,through_ms,evidence_ref::text,journal_id::text "
                "FROM v2_capital_baselines WHERE tenant_id=%s AND registry_id=%s AND currency=%s",
                (tenant, registry, currency),
            ).fetchone()
            if prior:
                if prior[:3] != (opening, through_ms, ref):
                    raise ValueError("BASELINE_CONFLICT")
                return prior[3]
            journal = None
            if opening:
                journal = CapitalJournal(lambda: nullcontext(c)).post(
                    tenant,
                    registry,
                    kind="OPENING",
                    currency=currency,
                    cash_delta=decimal_text(opening),
                    source="capital-baseline-v1",
                    source_id=currency,
                    occurred_at_ms=through_ms,
                )
            c.execute(
                "INSERT INTO v2_capital_baselines(tenant_id,registry_id,currency,through_ms,opening_amount,evidence_ref,journal_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (tenant, registry, currency, through_ms, opening, ref, journal),
            )
            return journal

    def project_window(
        self, tenant_id, registry_id, *, start_ms, end_ms, limit=1000, after=None
    ):
        tenant, registry = uid(tenant_id), uid(registry_id)
        timestamp(start_ms)
        timestamp(end_ms)
        after_time, after_id = None, None
        if after is not None:
            if not isinstance(after, dict) or set(after) != {"at_ms", "income_id"}:
                raise ValueError("invalid projection cursor")
            after_time, after_id = timestamp(after["at_ms"]), uid(after["income_id"])
            if not start_ms <= after_time <= end_ms:
                raise ValueError("cursor outside projection window")
        if (
            start_ms > end_ms
            or end_ms - start_ms > 7 * 86400000
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("bounded projection window required")
        with self.connect() as c:
            CapitalJournal._lock(c, tenant)
            CapitalJournal._account(c, tenant, registry)
            rows = c.execute(
                """SELECT i.income_id::text,i.income_type,i.amount,i.currency,i.occurred_at_ms,
                b.through_ms,r.journal_id::text,EXISTS(SELECT 1 FROM v2_capital_journals undo WHERE undo.reverses=r.journal_id),
                EXISTS(SELECT 1 FROM v2_capital_journals undo WHERE undo.reverses=b.journal_id)
                FROM v2_exchange_income i JOIN v2_tenant_accounts a
                ON (a.exchange,a.account_id,a.environment,a.product)=(i.exchange,i.account_id,i.environment,i.product)
                LEFT JOIN v2_capital_baselines b ON (b.tenant_id,b.registry_id,b.currency)=(a.tenant_id,a.registry_id,i.currency)
                LEFT JOIN v2_capital_income_receipts r ON r.income_id=i.income_id
                WHERE a.tenant_id=%s AND a.registry_id=%s AND i.occurred_at_ms BETWEEN %s AND %s
                AND (%s::bigint IS NULL OR (i.occurred_at_ms,i.income_id)>(%s::bigint,%s::uuid))
                ORDER BY i.occurred_at_ms,i.income_id LIMIT %s""",
                (
                    tenant,
                    registry,
                    start_ms,
                    end_ms,
                    after_time,
                    after_time,
                    after_id,
                    limit + 1,
                ),
            ).fetchall()
            result = {
                "run_id": str(uuid4()),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "posted": 0,
                "replayed": 0,
                "zero": 0,
                "before_baseline": 0,
                "blocked": [],
                "truncated": len(rows) > limit,
                "coverage_status": "NOT_PROVEN",
                "execution_authorized": False,
                "after": after,
                "next_cursor": {
                    "at_ms": rows[limit - 1][4],
                    "income_id": rows[limit - 1][0],
                }
                if len(rows) > limit
                else None,
            }
            journal = CapitalJournal(lambda: nullcontext(c))
            for (
                income,
                income_type,
                value,
                currency,
                at,
                cutoff,
                existing,
                reversed_entry,
                baseline_reversed,
            ) in rows[:limit]:
                reason = None
                if cutoff is None:
                    reason = "BASELINE_MISSING"
                elif at <= cutoff:
                    result["before_baseline"] += 1
                    continue
                elif baseline_reversed:
                    reason = "BASELINE_REVERSED"
                elif existing:
                    if reversed_entry:
                        reason = "PROJECTED_EVENT_REVERSED"
                    else:
                        result["replayed"] += 1
                        continue
                elif income_type not in MAPPING:
                    reason = "UNSUPPORTED_INCOME_TYPE"
                elif income_type == "COMMISSION" and value > 0:
                    reason = "INVALID_COMMISSION_DIRECTION"
                if reason:
                    result["blocked"].append({"income_id": income, "reason": reason})
                    continue
                if not value:
                    result["zero"] += 1
                    continue
                posted = journal.post(
                    tenant,
                    registry,
                    kind=MAPPING[income_type],
                    currency=currency,
                    cash_delta=decimal_text(value),
                    source="binance-income-v1",
                    source_id=income,
                    occurred_at_ms=at,
                )
                c.execute(
                    "INSERT INTO v2_capital_income_receipts(income_id,tenant_id,registry_id,journal_id,mapping_version) "
                    "VALUES (%s,%s,%s,%s,'binance-cash-v1')",
                    (income, tenant, registry, posted),
                )
                result["posted"] += 1
            result["status"] = (
                "NEEDS_REVIEW"
                if result["blocked"] or result["truncated"]
                else "PROJECTED_LOCAL_FACTS"
            )
            c.execute(
                "INSERT INTO v2_capital_projection_runs(run_id,tenant_id,registry_id,result) VALUES (%s,%s,%s,%s)",
                (result["run_id"], tenant, registry, Jsonb(result)),
            )
            return result

    def compare_wallet(
        self,
        tenant_id,
        registry_id,
        *,
        currency,
        wallet_amount,
        as_of_ms,
        observation_ref,
    ):
        """Persist a caller-supplied comparison; equality is NOT venue reconciliation."""
        tenant, registry, ref = uid(tenant_id), uid(registry_id), uid(observation_ref)
        timestamp(as_of_ms)
        observed = amount(wallet_amount)
        if not isinstance(currency, str) or not re.fullmatch(
            r"[A-Z][A-Z0-9]{0,19}", currency
        ):
            raise ValueError("normalized currency required")
        request_hash = digest(
            canonical(
                {
                    "currency": currency,
                    "wallet_amount": decimal_text(observed),
                    "as_of_ms": as_of_ms,
                }
            )
        )
        with self.connect() as c:
            CapitalJournal._lock(c, tenant)
            CapitalJournal._account(c, tenant, registry)
            prior = c.execute(
                "SELECT request_digest,result FROM v2_capital_wallet_checks "
                "WHERE tenant_id=%s AND registry_id=%s AND observation_ref=%s",
                (tenant, registry, ref),
            ).fetchone()
            if prior:
                if prior[0] != request_hash:
                    raise ValueError("WALLET_OBSERVATION_CONFLICT")
                return prior[1]
            baseline = c.execute(
                "SELECT through_ms,journal_id FROM v2_capital_baselines "
                "WHERE tenant_id=%s AND registry_id=%s AND currency=%s",
                (tenant, registry, currency),
            ).fetchone()
            if baseline is None or as_of_ms < baseline[0]:
                raise ValueError("BASELINE_MISSING_OR_TOO_NEW")
            book = (
                CapitalJournal(lambda: nullcontext(c))
                .balances(tenant, registry, as_of_ms=as_of_ms)
                .get(currency, {})
                .get("book_cash", "0")
            )
            unresolved = c.execute(
                """SELECT count(*) FROM v2_exchange_income i JOIN v2_tenant_accounts a
                ON (a.exchange,a.account_id,a.environment,a.product)=(i.exchange,i.account_id,i.environment,i.product)
                LEFT JOIN v2_capital_income_receipts r ON r.income_id=i.income_id
                WHERE a.tenant_id=%s AND a.registry_id=%s AND i.currency=%s
                AND i.occurred_at_ms>%s AND i.occurred_at_ms<=%s AND
                ((r.income_id IS NULL AND NOT(i.amount=0 AND i.income_type IN ('REALIZED_PNL','COMMISSION','FUNDING_FEE')))
                 OR EXISTS(SELECT 1 FROM v2_capital_journals undo WHERE undo.reverses=r.journal_id AND undo.occurred_at_ms<=%s))""",
                (tenant, registry, currency, baseline[0], as_of_ms, as_of_ms),
            ).fetchone()[0]
            baseline_reversed = bool(
                c.execute(
                    "SELECT 1 FROM v2_capital_journals WHERE reverses=%s AND occurred_at_ms<=%s",
                    (baseline[1], as_of_ms),
                ).fetchone()
            )
            with localcontext() as ctx:
                ctx.prec = 100
                # Account sums may exceed the single-entry amount domain.
                difference = observed - Decimal(book)
            result = {
                "currency": currency,
                "as_of_ms": as_of_ms,
                "book_cash": book,
                "wallet_amount": decimal_text(observed),
                "difference": decimal_text(difference),
                "unresolved_local_facts": unresolved,
                "baseline_reversed": baseline_reversed,
                "status": "DIFFERENCE" if difference else "AMOUNT_MATCH_UNVERIFIED",
                "coverage_status": "NOT_PROVEN",
                "execution_authorized": False,
            }
            c.execute(
                "INSERT INTO v2_capital_wallet_checks(tenant_id,registry_id,observation_ref,request_digest,result) VALUES (%s,%s,%s,%s,%s)",
                (tenant, registry, ref, request_hash, Jsonb(result)),
            )
            return result
