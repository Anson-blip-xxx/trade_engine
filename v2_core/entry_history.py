"""Scoped, read-only execution history shared by entry evidence and submission.

The caller owns its transaction, account lock and query time budget. This module
does not read market archives, change policy or perform execution/state writes.
"""

from dataclasses import asdict


def prior_close(conn, scope, target, now):
    """Latest actually filled episode, scoped exactly, not an OPEN decision.

    A newer opposite direction is NOT an old same-direction opportunity. An
    unsettled or ambiguous close remains UNKNOWN rather than skipping backward.
    Terminal fills of one order use a quantity-weighted actual fill price.
    """
    row = conn.execute(
        """SELECT i.intent_id::text,i.payload->>'side',ep.status,f.oq,f.cq,
          f.closed_ms,f.known_ms,terminal.price,terminal.orders,
          CASE WHEN cl.request_evidence->>'origin'='BINANCE_ALGO_CHILD'
            THEN 'VENUE_PROTECTION_CHILD' ELSE cl.request_evidence->>'reason' END,
          EXISTS(SELECT 1 FROM v2_orders o WHERE o.episode_id=i.intent_id
            AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED')),
          settled.evidence->>'net_pnl'
        FROM v2_trade_intents i JOIN v2_episodes ep ON ep.episode_id=i.intent_id
        LEFT JOIN v2_settlements settled ON settled.episode_id=i.intent_id
          AND settled.revision=ep.accounting_revision AND settled.currency='USDT'
        JOIN LATERAL (
          SELECT sum(x.quantity) FILTER(WHERE o.leg='OPEN') oq,
            coalesce(sum(x.quantity) FILTER(WHERE o.leg='CLOSE'),0) cq,
            max(x.occurred_at_ms) FILTER(WHERE o.leg='CLOSE') closed_ms,
            (extract(epoch FROM max(greatest(x.created_at,o.updated_at)) FILTER(WHERE o.leg='CLOSE'))*1000)::bigint known_ms,
            max(x.occurred_at_ms) last_fill
          FROM v2_orders o JOIN v2_fills x USING(order_id) WHERE o.episode_id=i.intent_id
        ) f ON f.oq>0
        LEFT JOIN LATERAL (
          SELECT round(sum(x.price*x.quantity)/nullif(sum(x.quantity),0),18)::text price,
            count(DISTINCT o.order_id) orders
          FROM v2_orders o JOIN v2_fills x USING(order_id)
          WHERE o.episode_id=i.intent_id AND o.leg='CLOSE' AND x.occurred_at_ms=f.closed_ms
        ) terminal ON true
        LEFT JOIN LATERAL (
          SELECT request_evidence FROM v2_orders o
          JOIN LATERAL (SELECT max(x.occurred_at_ms) latest_fill FROM v2_fills x
            WHERE x.order_id=o.order_id) cf ON cf.latest_fill IS NOT NULL
          WHERE o.episode_id=i.intent_id AND leg='CLOSE'
          ORDER BY cf.latest_fill DESC,o.order_id DESC LIMIT 1
        ) cl ON true
        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
          AND i.payload->>'symbol'=%s AND i.producer IN ('s6','s8')
        ORDER BY f.last_fill DESC,i.intent_id DESC LIMIT 1""",
        (*asdict(scope).values(), target),
    ).fetchone()
    if row is None:
        return None, "NO_PREVIOUS_FILLED_EPISODE"
    (
        identity,
        side,
        status,
        oq,
        cq,
        closed,
        known,
        price,
        count,
        reason,
        pending,
        net,
    ) = row
    if (
        status != "SETTLED"
        or oq != cq
        or pending
        or not closed
        or not known
        or max(closed, known) > now
    ):
        return None, "PREVIOUS_EPISODE_UNRESOLVED"
    if count != 1 or not price:
        return None, "PREVIOUS_CLOSE_PRICE_AMBIGUOUS"
    return {
        "previous_episode": identity,
        "side": side,
        "exit_reason": reason,
        "exit_known_ms": max(closed, known),
        "exit_price": price,
        "exit_net_pnl": net,
    }, None
