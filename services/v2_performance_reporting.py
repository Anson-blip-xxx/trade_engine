"""Read-only event-time reports. Cash and closed-trade PnL are NEVER added.

Only owned V2 directional episodes are included. All days are UTC+8. Missing
venue realized PnL or non-USDT fees make cash PnL unknown rather than zero.
"""


def performance(conn):
    scope = "i.exchange='BINANCE' AND i.environment='SANDBOX' AND i.product='FUTURES' AND i.producer IN ('s6','s8')"
    latest = f"""SELECT s.*, i.account_id FROM v2_settlements s
        JOIN v2_trade_intents i ON i.intent_id=s.episode_id
        WHERE {scope} AND s.currency='USDT'
        AND s.revision=(SELECT max(x.revision) FROM v2_settlements x WHERE x.episode_id=s.episode_id)"""
    total, pnl, wins = conn.execute(f"""SELECT count(*),
        coalesce(sum((evidence->>'net_pnl')::numeric),0),
        count(*) FILTER (WHERE (evidence->>'net_pnl')::numeric>0)
        FROM ({latest}) s""").fetchone()
    intents, opened = conn.execute(f"""SELECT count(*),count(*) FILTER (WHERE
        (SELECT coalesce(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0)
         FROM v2_orders o JOIN v2_fills f USING(order_id) WHERE o.episode_id=i.intent_id)>0)
        FROM v2_trade_intents i WHERE {scope}""").fetchone()
    cash = conn.execute(f"""WITH events AS (
        SELECT i.account_id,f.occurred_at_ms,
          CASE WHEN f.fee_currency='USDT' AND f.payload->>'venue_realized_pnl' IS NOT NULL
            THEN (f.payload->>'venue_realized_pnl')::numeric-f.fee END AS pnl
        FROM v2_fills f JOIN v2_orders o USING(order_id)
        JOIN v2_trade_intents i ON i.intent_id=o.episode_id WHERE {scope}
        UNION ALL
        SELECT i.account_id,c.occurred_at_ms,CASE WHEN c.currency='USDT' THEN c.amount END
        FROM v2_cash_adjustments c JOIN v2_trade_intents i ON i.intent_id=c.episode_id WHERE {scope}
        ) SELECT account_id,(to_timestamp(occurred_at_ms/1000.0) AT TIME ZONE 'Asia/Shanghai')::date::text,
          CASE WHEN count(*) FILTER (WHERE pnl IS NULL)=0 THEN sum(pnl) END,
          count(*) FILTER (WHERE pnl IS NULL)
        FROM events GROUP BY 1,2 ORDER BY 2 DESC,1 LIMIT 180""").fetchall()
    closed = conn.execute(f"""WITH settled AS ({latest})
        SELECT s.account_id,(to_timestamp(f.closed_ms/1000.0) AT TIME ZONE 'Asia/Shanghai')::date::text,
          sum((s.evidence->>'net_pnl')::numeric),count(*)
        FROM settled s JOIN LATERAL (
          SELECT max(x.occurred_at_ms) AS closed_ms FROM v2_orders o JOIN v2_fills x USING(order_id)
          WHERE o.episode_id=s.episode_id AND o.leg='CLOSE'
        ) f ON f.closed_ms IS NOT NULL GROUP BY 1,2 ORDER BY 2 DESC,1 LIMIT 180""").fetchall()
    return {
        "summary": {
            "trade_intents": intents,
            "open_positions": opened,
            "settled_trades": total,
            "realized_pnl": str(pnl),
            "win_rate": round(wins / total * 100, 1) if total else None,
        },
        "daily": {
            "timezone": "Asia/Shanghai",
            "scope": "owned V2 directional trades",
            "cash": [
                {
                    "account_id": a,
                    "day": d,
                    "net_pnl": str(p) if p is not None else None,
                    "unvalued_events": n,
                }
                for a, d, p, n in cash
            ],
            "closed_trades": [
                {"account_id": a, "day": d, "net_pnl": str(p), "trades": n}
                for a, d, p, n in closed
            ],
            "mark_to_market": None,
            "mark_to_market_status": "DAILY_EQUITY_SNAPSHOTS_NOT_AVAILABLE",
            "note": "收支按事件时间；整笔业绩按最终平仓时间；不可相加。每日净值尚无完整快照，不推算。",
        },
    }


def signal_funnel(conn):
    """Distinct signals at every stage, not S6/S8 receipt counts as signal counts."""
    return conn.execute("""WITH received AS MATERIALIZED (
        SELECT signal_id,source FROM v2_inbound_signals
        WHERE environment='SANDBOX' AND source IN ('tv_bridge','s3')
          AND received_at>=date_trunc('day',statement_timestamp() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'
        ), processed AS (
        SELECT DISTINCT r.signal_id FROM v2_signal_receipts r JOIN received s USING(signal_id)
        ), intents AS (
        SELECT DISTINCT i.signal_id FROM v2_trade_intents i JOIN received s USING(signal_id)
        ), filled AS (
        SELECT DISTINCT i.signal_id FROM v2_trade_intents i JOIN received s USING(signal_id)
          JOIN v2_orders o ON o.episode_id=i.intent_id JOIN v2_fills f USING(order_id) WHERE o.leg='OPEN'
        ) SELECT s.source,count(*),count(p.signal_id),count(i.signal_id),count(f.signal_id)
        FROM received s LEFT JOIN processed p USING(signal_id)
          LEFT JOIN intents i USING(signal_id) LEFT JOIN filled f USING(signal_id)
        GROUP BY s.source ORDER BY s.source""").fetchall()
