"""External cash facts are distinct from allocated episode PnL.

Commission and realized PnL must never be added to the fill-derived ledger a
second time. Only proven funding attribution is supported here; other types
remain available for account-level reconciliation.
"""

import json
from contextlib import nullcontext
from dataclasses import dataclass
from uuid import UUID, uuid5

from v2_core.evidence import canonical
from v2_core.ledger import Ledger, amount
from v2_core.state import normalized

_NAMESPACE = UUID("c5849272-0ed5-41cd-9c07-858522d721ad")


@dataclass(frozen=True)
class IncomeScope:
    exchange: str
    account_id: str
    environment: str
    product: str

    def __post_init__(self):
        for value in self.values:
            normalized(value)

    @property
    def values(self):
        return self.exchange, self.account_id, self.environment, self.product


class IncomeJournal:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def ingest(
        self,
        scope,
        *,
        income_type,
        source_id,
        symbol,
        amount_text,
        currency,
        occurred_at_ms,
        evidence,
    ):
        if not isinstance(scope, IncomeScope):
            raise TypeError("typed income scope required")
        for value in (income_type, source_id, currency):
            normalized(value)
        if not isinstance(symbol, str):
            raise TypeError("income symbol must be text")
        if symbol:
            normalized(symbol)  # Empty symbol is legitimate for account transfers.
        if type(occurred_at_ms) is not int or occurred_at_ms < 0:
            raise ValueError("nonnegative income timestamp required")
        value = amount(amount_text)
        encoded = canonical(evidence)
        identity = str(
            uuid5(
                _NAMESPACE,
                json.dumps(
                    [*scope.values, income_type, source_id], separators=(",", ":")
                ),
            )
        )
        values = (
            *scope.values,
            income_type,
            source_id,
            symbol,
            value,
            currency,
            occurred_at_ms,
            json.loads(encoded),
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO v2_exchange_income(income_id,exchange,account_id,environment,product,
                income_type,source_id,symbol,amount,currency,occurred_at_ms,evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING""",
                (identity, *values[:-1], encoded),
            )
            stored = conn.execute(
                """SELECT exchange,account_id,environment,product,income_type,source_id,
                symbol,amount,currency,occurred_at_ms,evidence FROM v2_exchange_income WHERE income_id=%s""",
                (identity,),
            ).fetchone()
            if stored != values:
                raise ValueError("income identity content conflict")
        return identity

    def pending(self, scope, *, limit=100, income_type=None):
        if not isinstance(scope, IncomeScope):
            raise TypeError("typed income scope required")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("bounded pending query required")
        if income_type is not None:
            normalized(income_type)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT i.income_id::text,i.income_type,i.source_id,i.symbol,i.amount::text,
                i.currency,i.occurred_at_ms FROM v2_exchange_income i
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND (%s::text IS NULL OR i.income_type=%s)
                AND NOT EXISTS (SELECT 1 FROM v2_income_allocations a WHERE a.income_id=i.income_id)
                ORDER BY i.occurred_at_ms,i.income_id LIMIT %s""",
                (*scope.values, income_type, income_type, limit),
            ).fetchall()
        return [
            dict(
                zip(
                    (
                        "income_id",
                        "income_type",
                        "source_id",
                        "symbol",
                        "amount",
                        "currency",
                        "occurred_at_ms",
                    ),
                    r,
                    strict=True,
                )
            )
            for r in rows
        ]

    def assign_funding(
        self,
        income_id,
        episode_id,
        *,
        expected_revision,
        evidence,
        connection=None,
    ):
        income_id, episode_id = str(UUID(income_id)), str(UUID(episode_id))
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("explicit accounting revision required")
        if not isinstance(evidence, dict) or evidence.get("fills_complete") is not True:
            raise ValueError("complete reconciliation evidence required")
        normalized(evidence.get("source"))
        encoded = canonical({"ledger_revision": expected_revision, "proof": evidence})
        with (
            nullcontext(connection) if connection is not None else self._connect()
        ) as conn:
            scope = conn.execute(
                """SELECT exchange,account_id,environment,product,payload->>'symbol'
                FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE""",
                (episode_id,),
            ).fetchone()
            income = conn.execute(
                """SELECT exchange,account_id,environment,product,symbol,
                income_type,amount::text,currency,occurred_at_ms FROM v2_exchange_income
                WHERE income_id=%s FOR UPDATE""",
                (income_id,),
            ).fetchone()
            if scope is None or income is None or scope != income[:5] or not income[4]:
                raise ValueError("income must match episode account and symbol")
            if income[5] != "FUNDING_FEE":
                raise ValueError(
                    "only funding can be allocated; avoid double-counting fill economics"
                )
            previous = conn.execute(
                "SELECT episode_id::text,evidence FROM v2_income_allocations WHERE income_id=%s",
                (income_id,),
            ).fetchone()
            if previous is not None:
                if previous != (episode_id, json.loads(encoded)):
                    raise ValueError("income allocation conflict")
                return False
            revision = conn.execute(
                "SELECT accounting_revision FROM v2_episodes WHERE episode_id=%s FOR UPDATE",
                (episode_id,),
            ).fetchone()
            if revision != (expected_revision,):
                raise ValueError("stale accounting revision")
            if conn.execute(
                "SELECT 1 FROM v2_orders WHERE episode_id=%s AND status NOT IN ('FILLED','CANCELLED','REJECTED') LIMIT 1",
                (episode_id,),
            ).fetchone():
                raise ValueError("order reconciliation still pending")
            totals = conn.execute(
                """SELECT
                COALESCE(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0),
                COALESCE(sum(CASE WHEN f.occurred_at_ms<%s THEN CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END ELSE 0 END),0),
                count(*) FILTER(WHERE f.occurred_at_ms=%s)
                FROM v2_fills f JOIN v2_orders o USING(order_id) WHERE o.episode_id=%s""",
                (income[8], income[8], episode_id),
            ).fetchone()
            if totals[0] != 0 or totals[1] <= 0 or totals[2] != 0:
                raise ValueError(
                    "funding ownership lacks unambiguous closed-position evidence"
                )
            ledger = Ledger(lambda: nullcontext(conn))
            source_id = "income:" + income_id
            ledger.adjustment(
                episode_id=episode_id,
                source_id=source_id,
                amount_text=income[6],
                currency=income[7],
                kind="FUNDING",
                occurred_at_ms=income[8],
                evidence={"income_id": income_id, "allocation": json.loads(encoded)},
            )
            key = json.dumps([*scope[:4], "FUNDING", source_id], separators=(",", ":"))
            conn.execute(
                "INSERT INTO v2_income_allocations(income_id,episode_id,adjustment_key,evidence) VALUES (%s,%s,%s,%s::jsonb)",
                (income_id, episode_id, key, encoded),
            )
        return True
