"""Read-only operator dashboard for the isolated V2 Testnet PostgreSQL schema."""

import json
import re
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from v2_core.database import connection_factory

_ZONE = ZoneInfo("Asia/Shanghai")
_ASSETS = Path(__file__).resolve().parent.parent / "web" / "v2_dashboard"
_TRADE = re.compile(r"^/api/trades/([0-9a-fA-F-]{36})$")


def _value(value):
    if isinstance(value, datetime):
        return value.astimezone(_ZONE).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _rows(cursor):
    keys = [column.name for column in cursor.description]
    return [dict(zip(keys, map(_value, row), strict=True)) for row in cursor.fetchall()]


class DashboardData:
    def __init__(self, connect):
        self.connect = connect

    def overview(self):
        with self.connect() as conn:
            trades = _rows(
                conn.execute("""
                SELECT i.intent_id::text AS id,i.created_at,i.producer,i.status,
                  i.payload->>'symbol' AS symbol,i.payload->>'side' AS side,
                  i.payload->>'quantity' AS planned_quantity,
                  e.snapshot->>'rationale' AS rationale,
                  e.snapshot->>'source' AS signal_source,
                  e.snapshot->'features'->'evaluation'->'market_plan'->>'event_type' AS event_type,
                  ep.status AS episode_status,
                  COALESCE(f.open_qty,0) AS open_qty,
                  COALESCE(f.close_qty,0) AS close_qty,
                  f.entry_price,f.exit_price,f.first_open_ms,f.last_close_ms,
                  o.net_pnl,o.return_pct,
                  st.evidence->>'net_pnl' AS settled_net_pnl,st.settled_at
                FROM v2_trade_intents i
                JOIN v2_decision_evidence e ON e.evidence_ref=i.evidence_ref
                LEFT JOIN v2_episodes ep ON ep.episode_id=i.intent_id
                LEFT JOIN LATERAL (
                    SELECT SUM(x.quantity) FILTER (WHERE ord.leg='OPEN') AS open_qty,
                      SUM(x.quantity) FILTER (WHERE ord.leg='CLOSE') AS close_qty,
                      SUM(x.quantity*x.price) FILTER (WHERE ord.leg='OPEN') /
                        NULLIF(SUM(x.quantity) FILTER (WHERE ord.leg='OPEN'),0) AS entry_price,
                      SUM(x.quantity*x.price) FILTER (WHERE ord.leg='CLOSE') /
                        NULLIF(SUM(x.quantity) FILTER (WHERE ord.leg='CLOSE'),0) AS exit_price,
                      MIN(x.occurred_at_ms) FILTER (WHERE ord.leg='OPEN') AS first_open_ms,
                      MAX(x.occurred_at_ms) FILTER (WHERE ord.leg='CLOSE') AS last_close_ms
                    FROM v2_orders ord JOIN v2_fills x USING(order_id)
                    WHERE ord.episode_id=i.intent_id
                ) f ON TRUE
                LEFT JOIN LATERAL (
                    SELECT evidence,settled_at FROM v2_settlements
                    WHERE episode_id=i.intent_id ORDER BY revision DESC LIMIT 1
                ) st ON TRUE
                LEFT JOIN v2_directional_outcomes o ON o.episode_id=i.intent_id
                WHERE i.environment='SANDBOX' AND i.product='FUTURES'
                  AND i.producer IN ('s6','s8')
                ORDER BY i.created_at DESC LIMIT 100
            """)
            )
            signals = _rows(
                conn.execute("""
                SELECT signal_id::text AS id,source,received_at,
                  snapshot->>'symbol' AS symbol,snapshot->>'signal' AS signal,
                  snapshot->>'observed_at' AS observed_at_ms,
                  snapshot->'features'->>'strength' AS strength
                FROM v2_inbound_signals WHERE environment='SANDBOX'
                  AND source IN ('tv_bridge','s3')
                ORDER BY received_at DESC LIMIT 24
            """)
            )
            settlements = _rows(
                conn.execute("""
                SELECT s.episode_id::text AS id,s.settled_at,
                  s.evidence->>'net_pnl' AS net_pnl,i.payload->>'symbol' AS symbol
                FROM v2_settlements s JOIN v2_trade_intents i
                  ON i.intent_id=s.episode_id
                WHERE i.environment='SANDBOX' AND i.producer IN ('s6','s8')
                  AND s.revision=(SELECT max(x.revision) FROM v2_settlements x
                                  WHERE x.episode_id=s.episode_id)
                ORDER BY s.settled_at ASC LIMIT 250
            """)
            )
        net = sum((Decimal(s["net_pnl"] or "0") for s in settlements), Decimal(0))
        wins = sum(Decimal(s["net_pnl"] or "0") > 0 for s in settlements)
        opens = sum(
            Decimal(str(t["open_qty"])) > Decimal(str(t["close_qty"])) for t in trades
        )
        return {
            "as_of": datetime.now(_ZONE).isoformat(),
            "scope": "BINANCE · FUTURES · TESTNET",
            "summary": {
                "trade_intents": len(trades),
                "open_positions": opens,
                "settled_trades": len(settlements),
                "realized_pnl": format(net, "f"),
                "win_rate": round(wins / len(settlements) * 100, 1)
                if settlements
                else None,
            },
            "trades": trades,
            "signals": signals,
            "settlements": settlements,
        }

    def trade(self, trade_id):
        with self.connect() as conn:
            cursor = conn.execute(
                """
                SELECT i.intent_id::text AS id,i.created_at,i.status,i.producer,
                  i.payload,e.snapshot,s.request_key AS signal_event_id,
                  s.source AS signal_source
                FROM v2_trade_intents i
                JOIN v2_decision_evidence e ON e.evidence_ref=i.evidence_ref
                LEFT JOIN v2_inbound_signals s ON s.signal_id=i.signal_id
                WHERE i.intent_id=%s::uuid AND i.environment='SANDBOX'
                  AND i.product='FUTURES' AND i.producer IN ('s6','s8')
            """,
                (trade_id,),
            )
            records = _rows(cursor)
            if not records:
                return None
            item = records[0]
            payload, snapshot = item.pop("payload") or {}, item.pop("snapshot") or {}
            evaluation = (snapshot.get("features") or {}).get("evaluation") or {}
            item["decision"] = {
                "symbol": payload.get("symbol"),
                "side": payload.get("side"),
                "planned_quantity": payload.get("quantity"),
                "rationale": snapshot.get("rationale"),
                "observed_at_ms": snapshot.get("observed_at"),
                "market_plan": evaluation.get("market_plan"),
                "sizing": evaluation.get("sizing"),
            }
            item["orders"] = _rows(
                conn.execute(
                    """
                SELECT order_id::text AS id,leg,status,quantity,
                  exchange_order_id,updated_at FROM v2_orders
                WHERE episode_id=%s::uuid ORDER BY updated_at,order_id
            """,
                    (trade_id,),
                )
            )
            item["fills"] = _rows(
                conn.execute(
                    """
                SELECT f.fill_key,o.leg,f.quantity,f.price,f.fee,
                  f.fee_currency,f.occurred_at_ms
                FROM v2_fills f JOIN v2_orders o USING(order_id)
                WHERE o.episode_id=%s::uuid ORDER BY f.occurred_at_ms,f.fill_key
            """,
                    (trade_id,),
                )
            )
            settled = _rows(
                conn.execute(
                    """
                SELECT revision,currency,evidence->>'net_pnl' AS net_pnl,
                  settled_at FROM v2_settlements WHERE episode_id=%s::uuid
                ORDER BY revision DESC LIMIT 1
            """,
                    (trade_id,),
                )
            )
            item["settlement"] = settled[0] if settled else None
            outcome = _rows(
                conn.execute(
                    """
                SELECT net_pnl,return_pct,quality_score,closing_price,
                  event_type,closed_at_ms FROM v2_directional_outcomes
                WHERE episode_id=%s::uuid
            """,
                    (trade_id,),
                )
            )
            item["outcome"] = outcome[0] if outcome else None
        return item


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    _slots = BoundedSemaphore(8)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    data = None

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == "/api/overview":
                return self._json(200, self.data.overview())
            match = _TRADE.fullmatch(path)
            if match:
                trade = self.data.trade(match.group(1))
                return (
                    self._json(200, trade)
                    if trade
                    else self._json(404, {"error": "NOT_FOUND"})
                )
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }
            if path not in assets:
                return self._send(404, "text/plain", b"Not found")
            filename, mime = assets[path]
            return self._send(200, mime, (_ASSETS / filename).read_bytes())
        except Exception:  # noqa: BLE001 - hide database details from public UI
            return self._json(503, {"error": "DATA_UNAVAILABLE"})

    def _json(self, status, payload):
        return self._send(
            status,
            "application/json",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        )

    def _send(self, status, mime, body):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)


def main():
    connect = connection_factory(
        "dbname=trade_v2_testnet host=/var/run/postgresql port=55432 connect_timeout=2",
        schema="trade_v2",
        read_only=True,
    )
    Handler.data = DashboardData(connect)
    with _Server(("127.0.0.1", 19527), Handler) as server:
        server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
