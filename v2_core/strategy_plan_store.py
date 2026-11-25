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


def trade_plan_record(row):
    """Give a joined intent/evidence row the shared batch-reader record shape."""
    return {
        "scope": dict(
            zip(
                ("exchange", "account_id", "environment", "product"),
                row[:4],
                strict=True,
            )
        ),
        "producer": row[4],
        "strategy_version": row[5],
        "config_digest": row[6],
        "payload": row[7],
        "evidence_strategy_version": row[8],
        "evidence_config_digest": row[9],
        "config": row[10],
        "snapshot": row[11],
    }


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
    return read_trade_plan_record(trade_plan_record(row))


def read_trade_plan_record(record):
    """Validate a fetched plan against its original intent, without new I/O.

    Batch consumers use the same immutable binding checks as the episode reader.
    The record is a projection of committed rows, not an execution permit and
    not proof that a caller-provided object has ever been persisted.
    """
    if type(record) is not dict:
        raise ValueError("STRATEGY_PLAN_EVIDENCE_INVALID")
    snapshot = record.get("snapshot")
    if type(snapshot) is not dict:
        raise ValueError("STRATEGY_PLAN_EVIDENCE_INVALID")
    if "strategy_plan" not in snapshot:
        return None
    try:
        plan = TradePlan.from_payload(snapshot["strategy_plan"])
        payload = record["payload"]
        if not (
            plan.scope == AccountScope(**record["scope"])
            and plan.instance_id == record["producer"]
            and plan.code_version
            == record["strategy_version"]
            == record["evidence_strategy_version"]
            == payload["strategy_version"]
            and plan.config_digest
            == record["config_digest"]
            == record["evidence_config_digest"]
            == payload["config_digest"]
            == digest(canonical(record["config"]))
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
