"""Read-only Binance closed-candle collector for directional T60 outcomes."""

from dataclasses import asdict

from v2_core.account_risk import AccountScope
from v2_core.directional_outcomes import DirectionalOutcomeJournal
from v2_core.ingress import milliseconds
from v2_core.ledger import amount


class DirectionalFollowupStage:
    def __init__(
        self,
        connect,
        transport,
        *,
        scope,
        clock_ms,
        limit=10,
        max_skew_ms=5000,
    ):
        if (
            not isinstance(scope, AccountScope)
            or scope.environment != "SANDBOX"
            or getattr(transport, "environment", scope.environment) != scope.environment
            or not callable(connect)
            or not callable(transport)
            or not callable(clock_ms)
            or type(limit) is not int
            or not 1 <= limit <= 20
            or type(max_skew_ms) is not int
            or not 1 <= max_skew_ms <= 10000
        ):
            raise ValueError("explicit bounded Testnet T60 collector required")
        self.connect, self.transport, self.scope = connect, transport, scope
        self.clock, self.limit, self.max_skew = clock_ms, limit, max_skew_ms
        self.journal = DirectionalOutcomeJournal(connect, scope=scope)

    def _exchange_time(self):
        before = milliseconds(self.clock())
        response = self.transport("/fapi/v1/time", {})
        after = milliseconds(self.clock())
        if (
            not isinstance(response, dict)
            or set(response) != {"serverTime"}
            or after < before
        ):
            raise ValueError("invalid T60 exchange clock")
        server = milliseconds(response["serverTime"])
        if not before - self.max_skew <= server <= after + self.max_skew:
            raise ValueError("T60 exchange clock outside accepted skew")
        return server, after

    def _candle(self, target, *, opened_at_ms):
        closed_at_ms = opened_at_ms + 59999
        rows = self.transport(
            "/fapi/v1/klines",
            {
                "symbol": target,
                "interval": "1m",
                "startTime": opened_at_ms,
                "endTime": closed_at_ms,
                "limit": 1,
            },
        )
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("incomplete T60 closed candle")
        row = rows[0]
        if (
            not isinstance(row, list)
            or len(row) != 12
            or milliseconds(row[0]) != opened_at_ms
            or milliseconds(row[6]) != closed_at_ms
            or not isinstance(row[4], str)
        ):
            raise ValueError("invalid T60 closed candle")
        return amount(row[4], positive=True), closed_at_ms

    def run_once(self):
        local_now = milliseconds(self.clock())
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT o.episode_id::text,o.symbol,o.closed_at_ms
                FROM v2_directional_outcomes o LEFT JOIN v2_directional_followups f
                ON f.episode_id=o.episode_id AND f.horizon_minutes=60
                WHERE (o.exchange,o.account_id,o.environment,o.product)=(%s,%s,%s,%s)
                AND f.episode_id IS NULL AND o.closed_at_ms+3600000<=%s
                ORDER BY o.closed_at_ms,o.episode_id LIMIT %s""",
                (*asdict(self.scope).values(), local_now, self.limit + 1),
            ).fetchall()
        if not rows:
            return {"status": "CLEAR", "followups": {}}
        backlog, rows = len(rows) > self.limit, rows[: self.limit]
        server, retrieved = self._exchange_time()
        results, waiting = {}, False
        for episode, target, closed in rows:
            due = closed + 3600000
            candle_open = ((due + 59999) // 60000) * 60000
            candle_close = candle_open + 59999
            if candle_close > server:
                waiting = True
                continue
            try:
                price, observed = self._candle(target, opened_at_ms=candle_open)
                results[episode] = self.journal.record_t60(
                    episode,
                    observed_at_ms=observed,
                    price=str(price),
                    evidence={
                        "source": "binance-public-closed-1m-v1",
                        "symbol": target,
                        "interval": "1m",
                        "candle_open_ms": candle_open,
                        "candle_close_ms": candle_close,
                        "exchange_server_time_ms": server,
                        "retrieved_at_ms": retrieved,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - isolate one corrupt observation
                results[episode] = {
                    "status": "BLOCKED",
                    "error_code": type(exc).__name__,
                }
        blocked = any(value.get("status") == "BLOCKED" for value in results.values())
        return {
            "status": (
                "BLOCKED" if blocked else "PENDING" if waiting or backlog else "CLEAR"
            ),
            "followups": results,
        }
