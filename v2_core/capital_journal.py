"""Internal account cash accounting, NOT authorization or available margin.

Caller-supplied tenant IDs require trusted orchestration. No public API, exchange
calls, implicit FX conversion, or automatic ingestion is provided here.
"""

import re
from decimal import localcontext
from uuid import uuid4

from v2_core.account_registry import uid
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount

BOOKS = {
    "OPENING": "EXTERNAL_CAPITAL",
    "DEPOSIT": "EXTERNAL_CAPITAL",
    "WITHDRAWAL": "EXTERNAL_CAPITAL",
    "REALIZED_PNL": "REALIZED_PNL",
    "FEE": "FEES",
    "FEE_REBATE": "FEES",
    "FUNDING": "FUNDING",
    "TRANSFER_OUT": "TRANSFER_CLEARING",
    "TRANSFER_IN": "TRANSFER_CLEARING",
}


def decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def identifier(value, limit):
    if not isinstance(value, str) or not re.fullmatch(
        rf"[A-Za-z0-9_.:-]{{1,{limit}}}", value
    ):
        raise ValueError("normalized source identifier required")
    return value


def timestamp(value):
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise ValueError("invalid event timestamp")
    return value


class CapitalJournal:
    def __init__(self, connect):
        self.connect = connect

    @staticmethod
    def _account(c, tenant, registry):
        row = c.execute(
            "SELECT exchange,environment,product FROM v2_tenant_accounts "
            "WHERE tenant_id=%s AND registry_id=%s",
            (tenant, registry),
        ).fetchone()
        if row is None:
            raise ValueError("ACCOUNT_NOT_FOUND")
        return row

    @staticmethod
    def _lock(c, tenant):
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("capital:" + tenant,),
        )

    def post(
        self,
        tenant_id,
        registry_id,
        *,
        kind,
        currency,
        cash_delta,
        source,
        source_id,
        occurred_at_ms,
    ):
        if kind not in BOOKS:
            raise ValueError("invalid cash event kind")
        delta = amount(cash_delta)
        if (
            not delta
            or (
                kind in ("OPENING", "DEPOSIT", "FEE_REBATE", "TRANSFER_IN")
                and delta < 0
            )
            or (kind in ("WITHDRAWAL", "FEE", "TRANSFER_OUT") and delta > 0)
        ):
            raise ValueError("invalid cash direction")
        tenant, registry = uid(tenant_id), uid(registry_id)
        with self.connect() as c:
            self._lock(c, tenant)
            return self._write(
                c,
                tenant,
                registry,
                kind,
                currency,
                source,
                source_id,
                occurred_at_ms,
                [
                    (registry, "CASH", delta),
                    (registry, BOOKS[kind], delta.copy_negate()),
                ],
            )

    def transfer(
        self,
        tenant_id,
        registry_id,
        target_id,
        *,
        currency,
        quantity,
        source,
        source_id,
        occurred_at_ms,
    ):
        """Record an already matched transfer; NEVER initiate an exchange transfer."""
        tenant, registry, target = uid(tenant_id), uid(registry_id), uid(target_id)
        delta = amount(quantity, positive=True)
        if registry == target:
            raise ValueError("distinct transfer accounts required")
        with self.connect() as c:
            self._lock(c, tenant)
            return self._write(
                c,
                tenant,
                registry,
                "TRANSFER",
                currency,
                source,
                source_id,
                occurred_at_ms,
                [(registry, "CASH", delta.copy_negate()), (target, "CASH", delta)],
            )

    def reverse(self, tenant_id, journal_id, *, source, source_id, occurred_at_ms):
        tenant, original = uid(tenant_id), uid(journal_id)
        with self.connect() as c:
            self._lock(c, tenant)
            row = c.execute(
                "SELECT registry_id::text,kind,currency,occurred_at_ms FROM v2_capital_journals "
                "WHERE tenant_id=%s AND journal_id=%s",
                (tenant, original),
            ).fetchone()
            if row is None:
                raise ValueError("JOURNAL_NOT_FOUND")
            timestamp(occurred_at_ms)
            if row[1] == "REVERSAL" or occurred_at_ms < row[3]:
                raise ValueError("invalid reversal")
            lines = c.execute(
                "SELECT registry_id::text,book,-amount FROM v2_capital_entries "
                "WHERE tenant_id=%s AND journal_id=%s",
                (tenant, original),
            ).fetchall()
            return self._write(
                c,
                tenant,
                row[0],
                "REVERSAL",
                row[2],
                source,
                source_id,
                occurred_at_ms,
                lines,
                reverses=original,
            )

    def _write(
        self,
        c,
        tenant,
        registry,
        kind,
        currency,
        source,
        source_id,
        at,
        lines,
        reverses=None,
    ):
        if not isinstance(currency, str) or not re.fullmatch(
            r"[A-Z][A-Z0-9]{0,19}", currency
        ):
            raise ValueError("normalized currency required")
        identifier(source, 120)
        identifier(source_id, 160)
        timestamp(at)
        scope = self._account(c, tenant, registry)
        for account, _, _ in lines:
            if self._account(c, tenant, account) != scope:
                raise ValueError("CAPITAL_SCOPE_MISMATCH")
        encoded = canonical(
            {
                "tenant": tenant,
                "registry": registry,
                "kind": kind,
                "currency": currency,
                "source": source,
                "source_id": source_id,
                "occurred_at_ms": at,
                "reverses": reverses,
                "entries": [
                    {"account": a, "book": b, "amount": decimal_text(v)}
                    for a, b, v in sorted(lines)
                ],
            }
        )
        hashed = digest(encoded)
        prior = c.execute(
            "SELECT journal_id::text,request_digest FROM v2_capital_journals "
            "WHERE tenant_id=%s AND registry_id=%s AND kind=%s AND source=%s AND source_id=%s",
            (tenant, registry, kind, source, source_id),
        ).fetchone()
        if prior:
            if prior[1] != hashed:
                raise ValueError("CAPITAL_EVENT_CONFLICT")
            return prior[0]
        if (
            reverses
            and c.execute(
                "SELECT 1 FROM v2_capital_journals WHERE reverses=%s", (reverses,)
            ).fetchone()
        ):
            raise ValueError("ALREADY_REVERSED")
        journal = str(uuid4())
        c.execute(
            "INSERT INTO v2_capital_journals "
            "(journal_id,tenant_id,registry_id,kind,currency,source,source_id,request_digest,occurred_at_ms,reverses) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                journal,
                tenant,
                registry,
                kind,
                currency,
                source,
                source_id,
                hashed,
                at,
                reverses,
            ),
        )
        for account, book, value in lines:
            c.execute(
                "INSERT INTO v2_capital_entries(journal_id,tenant_id,registry_id,book,amount) "
                "VALUES (%s,%s,%s,%s,%s)",
                (journal, tenant, account, book, value),
            )
        return journal

    def balances(self, tenant_id, registry_id, *, as_of_ms=None):
        return self._balances(uid(tenant_id), uid(registry_id), None, as_of_ms)

    def consolidated(self, tenant_id, *, environment, as_of_ms=None):
        if environment not in ("SANDBOX", "LIVE"):
            raise ValueError("explicit environment required")
        return self._balances(uid(tenant_id), None, environment, as_of_ms)

    def _balances(self, tenant, registry, environment, at):
        if at is not None:
            timestamp(at)
        with self.connect() as c:
            if registry:
                self._account(c, tenant, registry)
            rows = c.execute(
                """SELECT j.currency,e.book,sum(e.amount) FROM v2_capital_entries e
                JOIN v2_capital_journals j USING(tenant_id,journal_id)
                JOIN v2_tenant_accounts a ON a.tenant_id=e.tenant_id AND a.registry_id=e.registry_id
                WHERE e.tenant_id=%s AND (%s::uuid IS NULL OR e.registry_id=%s::uuid)
                AND (%s::text IS NULL OR a.environment=%s)
                AND (%s::bigint IS NULL OR j.occurred_at_ms<=%s::bigint)
                GROUP BY j.currency,e.book""",
                (tenant, registry, registry, environment, environment, at, at),
            ).fetchall()
        grouped = {}
        for currency, book, value in rows:
            grouped.setdefault(currency, {})[book] = value
        result = {}
        with localcontext() as ctx:
            ctx.prec = 100
            for currency, books in grouped.items():
                cash = books.get("CASH", 0)
                capital = -books.get("EXTERNAL_CAPITAL", 0)
                pnl = -books.get("REALIZED_PNL", 0)
                fees = books.get("FEES", 0)
                funding = -books.get("FUNDING", 0)
                net = pnl - fees + funding
                result[currency] = {
                    k: decimal_text(v)
                    for k, v in {
                        "book_cash": cash,
                        "net_contributed_capital": capital,
                        "realized_pnl": pnl,
                        "fees_paid": fees,
                        "funding_net": funding,
                        "net_trading_pnl": net,
                        "transfers_net": cash - capital - net,
                    }.items()
                }
        return result
