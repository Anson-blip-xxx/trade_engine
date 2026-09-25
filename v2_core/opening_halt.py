"""Irreversible per-episode opening halt, serialized with prepare/dispatch.

Does not cancel orders already submitted or prohibit risk-reducing exits.
There is deliberately no automatic unhalt or timeout-based reset.
"""

from contextlib import nullcontext
from uuid import UUID

from v2_core.account_draining import require_not_draining
from v2_core.account_risk import AccountRiskDenied
from v2_core.state import BusinessState, StateKey


def key_for(conn, episode):
    row = conn.execute(
        "SELECT exchange,account_id,environment,product FROM v2_trade_intents WHERE intent_id=%s",
        (episode,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown episode")
    return StateKey(
        *row, namespace="episode-opening-halt-v1", key=str(UUID(str(episode)))
    )


def require_opening_allowed(conn, episode):
    # Caller holds the intent root lock, matching the halt writer's lock order.
    key = key_for(conn, episode)
    require_not_draining(conn, key)
    symbol = conn.execute(
        "SELECT payload->>'symbol' FROM v2_trade_intents WHERE intent_id=%s", (episode,)
    ).fetchone()[0]
    quarantine = StateKey(
        key.exchange,
        key.account_id,
        key.environment,
        key.product,
        namespace="symbol-opening-quarantine-v1",
        key=symbol,
    )
    if BusinessState(lambda: nullcontext(conn)).read(quarantine) is not None:
        raise AccountRiskDenied(
            "SYMBOL_OPENING_QUARANTINED", {"quarantine_id": quarantine.identity}
        )
    if BusinessState(lambda: nullcontext(conn)).read(key) is not None:
        # Even an unexpected tombstone fails closed; no silent unhalt.
        raise AccountRiskDenied("EPISODE_OPENING_HALTED", {"halt_id": key.identity})


def halt_opening(connect, episode, *, scope):
    with connect() as conn:
        conn.execute(
            "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
            (episode,),
        )
        key = key_for(conn, episode)
        if (key.exchange, key.account_id, key.environment, key.product) != (
            scope.exchange,
            scope.account_id,
            scope.environment,
            scope.product,
        ):
            raise ValueError("halt outside bound account")
        return BusinessState(lambda: nullcontext(conn)).change(
            key,
            expected_version=0,
            request_key="halt",
            payload={"halted": True, "reason": "PARTIAL_OPEN_PROTECTION_RECOVERY"},
            reason="EPISODE_OPENING_HALTED",
        )
