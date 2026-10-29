"""Narrow income-lag isolation, not settlement and never an order permit.

Closed episodes retain their risk reservations and ledger state. Only the known
60-second cash publication delay may stop blocking unrelated fresh candidates.
Every actual order still needs the existing account and venue safety guards.
"""

import json
from dataclasses import asdict
from uuid import UUID

from v2_core.evidence import canonical, digest
from v2_core.state import BusinessState, StateKey


def policy_key(scope):
    return StateKey(
        **asdict(scope), namespace="settlement-entry-policy-v1", key="current"
    )


def entry_clearance(stage, result, protection_coverage):
    """Return None on any uncertainty; caller preserves the original blockage."""
    try:
        return _checked(stage, result, protection_coverage)
    except Exception:  # noqa: BLE001 - diagnostics must never weaken an error
        return None


def _checked(stage, result, protection_coverage):
    if stage.scope.environment != "SANDBOX" or not isinstance(result, dict):
        return None
    if (
        result.get("status") != "BLOCKED"
        or result.get("settlement_authorized") is not False
    ):
        return None
    if set(result) != {"status", "settlements", "outcomes", "settlement_authorized"}:
        return None
    outcomes, settlements = result["outcomes"], result["settlements"]
    if (
        not isinstance(outcomes, dict)
        or not isinstance(settlements, dict)
        or not settlements
    ):
        return None
    for record in outcomes.values():
        if not isinstance(record, dict) or "status" in record or "error_code" in record:
            return None
    lagging = set()
    for episode, record in settlements.items():
        if str(UUID(episode)) != episode or not isinstance(record, dict):
            return None
        if record.get("status") == "SETTLED":
            if (
                "error_code" in record
                or record.get("settlement_authorized") is not False
            ):
                return None
            continue
        if record != {
            "status": "BLOCKED",
            "reason": "WAITING_INCOME_LAG",
            "settlement_authorized": False,
        }:
            return None
        lagging.add(episode)
    if not lagging or len(lagging) > stage.limit:
        return None
    selected = BusinessState(stage.connect).read(policy_key(stage.scope))
    values = json.loads(selected.payload_json) if selected is not None else None
    if (
        selected is None
        or selected.deleted
        or values != {"enabled": True}
        or type(values["enabled"]) is not bool
    ):
        return None
    if stage.coverage.reuse_after_protection(protection_coverage) is None:
        return None
    with stage.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # All closed, unsettled directional episodes, not just a caller-selected
        # subset. LIMIT+1 makes a truncated batch a denial, never a clearance.
        rows = conn.execute(
            """SELECT i.intent_id::text,i.payload->>'symbol',i.data_revision,
            sum(f.quantity) FILTER (WHERE o.leg='OPEN'),
            sum(f.quantity) FILTER (WHERE o.leg='CLOSE'),max(f.occurred_at_ms),
            r.scope,r.status,r.notional::text,r.policy_version,
            EXISTS(SELECT 1 FROM v2_orders x WHERE x.episode_id=i.intent_id
                AND x.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED'))
            FROM v2_trade_intents i JOIN v2_episodes e ON e.episode_id=i.intent_id
            JOIN v2_orders o ON o.episode_id=i.intent_id JOIN v2_fills f USING(order_id)
            LEFT JOIN v2_risk_reservations r ON r.episode_id=i.intent_id
            WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
            AND i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1'
            AND e.status<>'SETTLED'
            GROUP BY i.intent_id,r.scope,r.status,r.notional,r.policy_version
            HAVING sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)=0
            AND sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE 0 END)>0
            ORDER BY i.intent_id LIMIT %s""",
            (*asdict(stage.scope).values(), stage.limit + 1),
        ).fetchall()
        now = stage.clock()
        if type(now) is not int or now < 0 or {row[0] for row in rows} != lagging:
            return None
        records = []
        for (
            episode,
            symbol,
            revision,
            opened,
            closed,
            last_fill,
            scope,
            status,
            notional,
            policy_version,
            nonterminal,
        ) in rows:
            if (
                opened is None
                or closed is None
                or not opened == closed > 0
                or type(last_fill) is not int
                or not last_fill <= now < last_fill + 60000
                or scope != stage.scope.key
                or status != "HELD"
                or nonterminal
            ):
                return None
            records.append(
                {
                    "episode": episode,
                    "symbol": symbol,
                    "revision": revision,
                    "opened_quantity": str(opened),
                    "closed_quantity": str(closed),
                    "latest_fill_ms": last_fill,
                    "risk_status": status,
                    "reserved_notional": notional,
                    "risk_policy_version": policy_version,
                }
            )
    # No facts or timestamps are refreshed. The second durable proof check also
    # fences fills/order state changes while the local checks were running.
    if stage.coverage.reuse_after_protection(protection_coverage) is None:
        return None
    now = stage.clock()
    if type(now) is not int or any(
        not r["latest_fill_ms"] <= now < r["latest_fill_ms"] + 60000 for r in records
    ):
        return None
    current_policy = BusinessState(stage.connect).read(policy_key(stage.scope))
    if current_policy != selected:
        return None
    proof = {
        "version": "pending-income-entry-isolation-v1",
        "account_scope": asdict(stage.scope),
        "observed_at_ms": now,
        "coverage_observation_id": protection_coverage["observation_id"],
        "policy_version": selected.version,
        "policy_digest": digest(selected.payload_json),
        "settlement_result_digest": digest(canonical(result)),
        "episodes": records,
        "retained_risk_reservations": True,
        "cash_complete": False,
        "execution_authorized": False,
        "settlement_authorized": False,
    }
    return {
        "status": "ENTRY_CLEAR_PENDING_INCOME",
        "risk_reservations_retained": True,
        "evidence": proof,
        "execution_authorized": False,
        "settlement_authorized": False,
    }
