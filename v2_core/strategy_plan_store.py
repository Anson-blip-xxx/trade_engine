"""Read an immutable plan through its committed intent, never current policy.

This is not an entry permit. Historical plans remain readable after expiry or
deployment withdrawal so a future management consumer can use the original
exit contract. A present but malformed plan never falls back to legacy fields.
"""

from uuid import UUID

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.strategy_plan import TradePlan


def read_trade_plan(conn, episode):
    row = conn.execute(
        "SELECT i.exchange,i.account_id,i.environment,i.product,i.producer,"
        "i.strategy_version,i.config_digest,i.payload,e.strategy_version,"
        "e.config_digest,e.config,e.snapshot FROM v2_trade_intents i "
        "JOIN v2_decision_evidence e USING(evidence_ref) WHERE i.intent_id=%s",
        (str(UUID(str(episode))),),
    ).fetchone()
    if row is None:
        raise ValueError("STRATEGY_PLAN_INTENT_REQUIRED")
    snapshot = row[11]
    if type(snapshot) is not dict:
        raise ValueError("STRATEGY_PLAN_EVIDENCE_INVALID")
    if "strategy_plan" not in snapshot:
        return None
    try:
        plan = TradePlan.from_payload(snapshot["strategy_plan"])
        payload = row[7]
        if not (
            plan.scope == AccountScope(*row[:4])
            and plan.instance_id == row[4]
            and plan.code_version == row[5] == row[8] == payload["strategy_version"]
            and plan.config_digest
            == row[6]
            == row[9]
            == payload["config_digest"]
            == digest(canonical(row[10]))
            and plan.symbol == payload["symbol"] == snapshot["symbol"]
            and plan.side == payload["side"]
            and amount(plan.quantity, positive=True)
            == amount(payload["quantity"], positive=True)
        ):
            raise ValueError("plan differs from its intent")
        for key, expected in (
            ("observed_at", plan.observed_at_ms),
            ("decided_at", plan.decided_at_ms),
            ("expires_at_ms", plan.expires_at_ms),
        ):
            if type(snapshot.get(key)) is not int or snapshot[key] != expected:
                raise ValueError("plan time differs from its evidence")
        return plan
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError("STRATEGY_PLAN_INTENT_MISMATCH") from None
