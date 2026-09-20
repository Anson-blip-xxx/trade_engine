"""Read-only collector and recovery-first bounded supervision. No order dispatch."""

from services.v2_s3_candles import VERSION, S3CandleRunner, build_frame
from v2_core.ingress import milliseconds, symbol


class BinanceCandleCollector:
    def __init__(
        self,
        transport,
        *,
        symbols,
        clock_ms,
        monotonic_ms,
        close_delay_ms=2000,
        max_skew_ms=5000,
        batch_budget_ms=30000,
    ):
        if (
            not isinstance(symbols, (list, tuple))
            or not 1 <= len(symbols) <= 20
            or len(set(symbols)) != len(symbols)
        ):
            raise ValueError("bounded unique symbol universe required")
        self.symbols = tuple(sorted(symbol(item) for item in symbols))
        if (
            any(
                type(v) is not int
                for v in (close_delay_ms, max_skew_ms, batch_budget_ms)
            )
            or not 0 <= close_delay_ms <= 10000
            or not 1 <= max_skew_ms <= 30000
            or not 1000 <= batch_budget_ms <= 60000
        ):
            raise ValueError("bounded collection timing policy required")
        self.transport, self.clock_ms, self.monotonic_ms = (
            transport,
            clock_ms,
            monotonic_ms,
        )
        self.environment = transport.environment
        self.close_delay_ms, self.max_skew_ms, self.batch_budget_ms = (
            close_delay_ms,
            max_skew_ms,
            batch_budget_ms,
        )

    def collect(self, *, skip_frame=None):
        started = self.monotonic_ms()
        before = milliseconds(self.clock_ms())
        response = self.transport("/fapi/v1/time", {})
        self._deadline(started)
        after = milliseconds(self.clock_ms())
        if not isinstance(response, dict) or set(response) != {"serverTime"}:
            raise ValueError("explicit exchange server time required")
        server = milliseconds(response["serverTime"])
        if (
            after < before
            or not before - self.max_skew_ms <= server <= after + self.max_skew_ms
        ):
            raise ValueError("exchange clock outside accepted skew")
        observed = (server - self.close_delay_ms) // 60000 * 60000
        if observed < 86400000:
            raise ValueError("insufficient closed history")
        if skip_frame is not None and skip_frame(f"{VERSION}:{observed}") is True:
            return None
        candles = {}
        for target in self.symbols:
            self._deadline(started)
            rows = self.transport(
                "/fapi/v1/klines",
                {
                    "symbol": target,
                    "interval": "1m",
                    "startTime": observed - 86400000,
                    "endTime": observed - 1,
                    "limit": 1440,
                },
            )
            self._deadline(started)
            if not isinstance(rows, list) or len(rows) != 1440:
                raise ValueError("incomplete exchange candle history")
            bars = []
            for index, row in enumerate(rows):
                if not isinstance(row, list) or len(row) != 12:
                    raise ValueError("invalid exchange candle tuple")
                opened = milliseconds(row[0])
                if (
                    opened != observed - 86400000 + index * 60000
                    or milliseconds(row[6]) != opened + 59999
                ):
                    raise ValueError("exchange candle time/order mismatch")
                if any(not isinstance(row[i], str) for i in (1, 2, 3, 4, 5, 9)):
                    raise ValueError("exchange candle decimals must be strings")
                bars.append(
                    {
                        "t": opened,
                        "o": row[1],
                        "h": row[2],
                        "l": row[3],
                        "c": row[4],
                        "v": row[5],
                        "tbv": row[9],
                    }
                )
            candles[target] = list(reversed(bars))
        batch = {
            "source": "BINANCE_FUTURES",
            "environment": self.environment,
            "interval": "1m",
            "closed_at": observed,
            "candles": candles,
        }
        build_frame(batch, environment=self.environment)
        self._deadline(started)
        return batch

    def _deadline(self, started):
        elapsed = self.monotonic_ms() - started
        if elapsed < 0 or elapsed >= self.batch_budget_ms:
            raise TimeoutError("market batch collection deadline")


class MarketSupervisor:
    def __init__(self, *, source, runtime, collector, notify, alerts=None):
        if (
            source.publisher is not runtime.processor.publisher
            or source.publisher.environment != collector.environment
        ):
            raise ValueError("single bound market pipeline required")
        if not callable(notify):
            raise TypeError("explicit alert sink required")
        if alerts is not None and alerts.environment != collector.environment:
            raise ValueError("alert environment mismatch")
        self.source, self.collector, self.notify = source, collector, notify
        self.alerts = alerts
        self.runner = S3CandleRunner(runtime, source=source)

    def run_once(self):
        result = self._run_once()
        if self.alerts is not None:
            try:
                result["alert_delivery"] = self.alerts.flush()
            except Exception as exc:  # noqa: BLE001 - PG outage needs external monitoring
                result["alert_delivery"] = {
                    "status": "UNAVAILABLE",
                    "error_code": type(exc).__name__,
                }
        return result

    def _run_once(self):
        stage = "RECOVER"
        try:
            result = self.runner.run_once()
            if result["status"] == "RETRY":
                return self._report(result)
            if self.source.has_pending():
                return {"status": "WAITING_SOURCE"}
            stage = "COLLECT"
            batch = self.collector.collect(skip_frame=self.source.has_frame)
            if batch is None:
                return {"status": "CURRENT"}
            stage = "ENQUEUE"
            frame_id = self.source.enqueue(batch)
            stage = "PROCESS"
            result = self.runner.run_once()
            return (
                self._report(result)
                if result["status"] == "RETRY"
                else {**result, "collected_frame_id": frame_id}
            )
        except Exception as exc:  # noqa: BLE001 - supervisor emits safe diagnostics only
            return self._report(
                {"status": "RETRY", "stage": stage, "error_code": type(exc).__name__}
            )

    def _report(self, result):
        try:
            acknowledged = (
                self.alerts.record(dict(result))
                if self.alerts is not None
                else self.notify(dict(result))
            ) is True
        except Exception:  # noqa: BLE001 - alert failure remains observable
            acknowledged = False
        status = "QUEUED" if self.alerts is not None else "SENT"
        return {**result, "alert_status": status if acknowledged else "UNAVAILABLE"}

    def serve(self, stop, *, interval_seconds=10):
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= 60:
            raise ValueError("bounded supervisor cadence required")
        while not stop.is_set():
            self.run_once()
            stop.wait(interval_seconds)


def create_market_pipeline(
    connect,
    *,
    clickhouse,
    redis_client,
    config,
    notify,
    clock_ms,
    monotonic_ms,
    http_connection_factory=None,
):
    """Explicit dependency wiring only. Does not create clients, start or trade."""
    from services.v2_s3_runtime import S3Runtime
    from services.v2_s3_source import DurableCandleSource
    from v2_core.chunked_archive import ClickHouseChunkedArchive
    from v2_core.operational import MarketAlerts
    from v2_core.producer import ProducerPublisher, RedisMarketContext
    from v2_core.public_market import BinancePublicMarket, PublicRateBudget

    if not isinstance(config, dict) or set(config) != {
        "environment",
        "symbols",
        "egress_scope",
        "weight_limit",
        "max_pending",
        "max_age_ms",
        "lifetime_ms",
        "enabled",
    }:
        raise ValueError("explicit market pipeline configuration required")
    environment = config["environment"]
    publisher = ProducerPublisher(
        connect,
        source="s3",
        environment=environment,
        market=RedisMarketContext(redis_client, environment=environment),
        clock_ms=clock_ms,
        max_age_ms=config["max_age_ms"],
        lifetime_ms=config["lifetime_ms"],
    )
    source = DurableCandleSource(
        publisher,
        archive=ClickHouseChunkedArchive(clickhouse),
        max_pending=config["max_pending"],
    )
    budget = PublicRateBudget(
        connect, scope=config["egress_scope"], limit=config["weight_limit"]
    )
    transport = BinancePublicMarket(
        environment=environment,
        budget=budget,
        enabled=config["enabled"],
        **(
            {"connection_factory": http_connection_factory}
            if http_connection_factory is not None
            else {}
        ),
    )
    collector = BinanceCandleCollector(
        transport,
        symbols=config["symbols"],
        clock_ms=clock_ms,
        monotonic_ms=monotonic_ms,
    )
    return MarketSupervisor(
        source=source,
        runtime=S3Runtime(publisher),
        collector=collector,
        notify=notify,
        alerts=MarketAlerts(connect, environment=environment, notify=notify),
    )
