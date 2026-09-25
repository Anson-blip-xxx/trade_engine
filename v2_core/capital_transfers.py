"""Attested two-leg cash matching; never initiate transfers or infer ownership.

An evidence reference is an internal caller's attestation, not venue verification.
Do not auto-pair facts just because their amounts/timestamps happen to match.
"""

from contextlib import nullcontext
from uuid import uuid4

from v2_core.account_registry import uid
from v2_core.capital_journal import CapitalJournal, decimal_text


class CapitalTransfers:
    def __init__(self, connect):
        self.connect = connect

    def match(self, tenant_id, *, outgoing_id, incoming_id, evidence_ref):
        tenant, outgoing, incoming, ref = map(
            uid, (tenant_id, outgoing_id, incoming_id, evidence_ref)
        )
        with self.connect() as c:
            CapitalJournal._lock(c, tenant)
            rows = []
            for income in (outgoing, incoming):
                row = c.execute(
                    """SELECT a.registry_id::text,i.income_type,i.amount,i.currency,i.occurred_at_ms,
                    a.exchange,a.environment,a.product,b.through_ms,
                    EXISTS(SELECT 1 FROM v2_capital_journals undo WHERE undo.reverses=b.journal_id)
                    FROM v2_exchange_income i JOIN v2_tenant_accounts a
                    ON (a.exchange,a.account_id,a.environment,a.product)=(i.exchange,i.account_id,i.environment,i.product)
                    LEFT JOIN v2_capital_baselines b ON (b.tenant_id,b.registry_id,b.currency)=(a.tenant_id,a.registry_id,i.currency)
                    WHERE a.tenant_id=%s AND i.income_id=%s""",
                    (tenant, income),
                ).fetchone()
                if row is None:
                    raise ValueError("TRANSFER_FACT_NOT_FOUND")
                rows.append(row)
            a, b = rows
            if (
                a[0] == b[0]
                or a[1] != "TRANSFER"
                or b[1] != "TRANSFER"
                or a[2] >= 0
                or b[2] != a[2].copy_negate()
                or a[3] != b[3]
                or a[5:8] != b[5:8]
                or a[4] > b[4]
            ):
                raise ValueError("TRANSFER_PAIR_MISMATCH")
            prior = c.execute(
                "SELECT pair_id::text,outgoing_id::text,incoming_id::text,evidence_ref::text,outgoing_journal::text,incoming_journal::text "
                "FROM v2_capital_transfer_pairs WHERE tenant_id=%s AND (outgoing_id=%s OR incoming_id=%s OR evidence_ref=%s)",
                (tenant, outgoing, incoming, ref),
            ).fetchall()
            if prior:
                if len(prior) != 1 or prior[0][1:4] != (outgoing, incoming, ref):
                    raise ValueError("TRANSFER_PAIR_CONFLICT")
                return self._result(c, prior[0][0], prior[0][4], prior[0][5])
            for row in rows:
                if row[8] is None or row[4] <= row[8] or row[9]:
                    raise ValueError("TRANSFER_BASELINE_INVALID")
            journal = CapitalJournal(lambda: nullcontext(c))
            legs = []
            for row, fact, kind in (
                (a, outgoing, "TRANSFER_OUT"),
                (b, incoming, "TRANSFER_IN"),
            ):
                legs.append(
                    journal.post(
                        tenant,
                        row[0],
                        kind=kind,
                        currency=row[3],
                        cash_delta=decimal_text(row[2]),
                        source="binance-transfer-v1",
                        source_id=fact,
                        occurred_at_ms=row[4],
                    )
                )
            pair = str(uuid4())
            c.execute(
                "INSERT INTO v2_capital_transfer_pairs(pair_id,tenant_id,outgoing_id,incoming_id,outgoing_journal,incoming_journal,evidence_ref) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (pair, tenant, outgoing, incoming, *legs, ref),
            )
            return self._result(c, pair, *legs)

    @staticmethod
    def _result(c, pair, outgoing, incoming):
        reversed_leg = bool(
            c.execute(
                """SELECT 1 FROM v2_capital_journals original
                LEFT JOIN v2_capital_baselines b ON
                (b.tenant_id,b.registry_id,b.currency)=(original.tenant_id,original.registry_id,original.currency)
                JOIN v2_capital_journals undo ON undo.reverses IN (original.journal_id,b.journal_id)
                WHERE original.journal_id IN (%s,%s)""",
                (outgoing, incoming),
            ).fetchone()
        )
        return {
            "pair_id": pair,
            "outgoing_journal": outgoing,
            "incoming_journal": incoming,
            "status": "NEEDS_REVIEW" if reversed_leg else "ATTESTED_MATCH",
            "execution_authorized": False,
            "venue_identity_verified": False,
        }
