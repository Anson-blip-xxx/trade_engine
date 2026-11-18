"""Credential-free shared market hub. No order port and no local business files.

All listed USDT perpetuals are eligible; batching controls work, not admission.
Closed candles are archived before publication. Redis is reconstructible cache.
The process owner must hold the environment advisory lock for its lifetime.
"""

import json
from decimal import Decimal

from services.v2_market_publishers import S0Publisher, feature_values
from services.v2_s0_regime import build_s0_state
from services.v2_s3_candles import (
    BREAKOUT_VERSION,
    CORRECTED_VERSION,
    PRICE_VERSION,
    ROLLING_VERSION,
    STRUCTURE_VERSION,
    build_frame,
)
from services.v2_s3_runtime import S3Runtime
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds, symbol
from v2_core.producer import ProducerPublisher, RedisMarketContext, market_envelope
from v2_core.projections import RedisProjection
from v2_core.scheduling import diagnostic_code
from v2_core.shared_market import Catalog, MarketPolicy
from v2_core.symbols import shared_frame_id


def normalize_rows(rows, start, end):
    count = (end - start) // 60000
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError("CANDLE_HISTORY_INCOMPLETE")
    bars = []
    for i, row in enumerate(rows):
        if (
            not isinstance(row, list)
            or len(row) != 12
            or row[0] != start + i * 60000
            or row[6] != row[0] + 59999
            or any(not isinstance(row[j], str) for j in (1, 2, 3, 4, 5, 9))
        ):
            raise ValueError("CANDLE_SHAPE_INVALID")
        bars.append(
            dict(
                zip(
                    ("t", "o", "h", "l", "c", "v", "tbv"),
                    (row[0], row[1], row[2], row[3], row[4], row[5], row[9]),
                    strict=True,
                )
            )
        )
    return list(reversed(bars))


class IncrementalArchive:
    def __init__(self, client, environment):
        if environment not in {"SANDBOX", "LIVE"}:
            raise ValueError("MARKET_ENVIRONMENT_REQUIRED")
        self.client, self.environment = client, environment

    def read(self, target, start, end):
        symbol(target)
        rows = self.client.query(
            "SELECT open_ms,payload,content_digest FROM v2_market_candles_incremental "
            "WHERE environment={environment:String} AND symbol={symbol:String} "
            "AND open_ms>={start:UInt64} AND open_ms<{end:UInt64}",
            parameters={
                "environment": self.environment,
                "symbol": target,
                "start": start,
                "end": end,
            },
        ).result_rows
        found = {}
        for opened, payload, fingerprint in rows:
            # clickhouse-connect returns FixedString as bytes by default.
            if isinstance(fingerprint, bytes):
                fingerprint = fingerprint.decode("ascii")
            if digest(payload) != fingerprint or (
                opened in found and found[opened] != payload
            ):
                raise ValueError("CANDLE_ARCHIVE_CONFLICT")
            bar = json.loads(payload)
            if bar["t"] != opened:
                raise ValueError("CANDLE_ARCHIVE_SCOPE")
            found[opened] = payload
        return [json.loads(found[t]) for t in sorted(found, reverse=True)]

    def put(self, target, bars):
        self.put_many({target: bars})

    def put_many(self, groups):
        """One insert per hub batch, avoiding hundreds of tiny parts/minute."""
        pending = []
        spans = {}
        for target, bars in groups.items():
            if not bars:
                continue
            start, end = min(b["t"] for b in bars), max(b["t"] for b in bars) + 60000
            spans[target] = (start, end)
            existing = {b["t"]: canonical(b) for b in self.read(target, start, end)}
            for bar in bars:
                encoded = canonical(bar)
                if bar["t"] in existing and existing[bar["t"]] != encoded:
                    raise ValueError("CANDLE_ARCHIVE_CONFLICT")
                if bar["t"] not in existing:
                    pending.append(
                        (self.environment, target, bar["t"], encoded, digest(encoded))
                    )
        if pending:
            self.client.insert(
                "v2_market_candles_incremental",
                pending,
                column_names=[
                    "environment",
                    "symbol",
                    "open_ms",
                    "payload",
                    "content_digest",
                ],
            )
        for target, (start, end) in spans.items():
            verified = {b["t"]: canonical(b) for b in self.read(target, start, end)}
            if any(verified.get(b["t"]) != canonical(b) for b in groups[target]):
                raise ValueError("CANDLE_ARCHIVE_UNCONFIRMED")


class IncrementalCandles:
    def __init__(self, transport, cache, archive, *, feature_version=PRICE_VERSION):
        # The HTTP host, archive partition and cache namespace must describe
        # the same market. Never relabel LIVE observations as Testnet candles.
        if (
            getattr(transport, "environment", None) not in {"LIVE", "SANDBOX"}
            or getattr(archive, "environment", None) != transport.environment
        ):
            raise ValueError("CANDLE_SOURCE_ARCHIVE_SCOPE_MISMATCH")
        if feature_version not in (
            PRICE_VERSION,
            ROLLING_VERSION,
            BREAKOUT_VERSION,
            CORRECTED_VERSION,
            STRUCTURE_VERSION,
        ):
            raise ValueError("MARKET_FEATURE_VERSION_UNSUPPORTED")
        self.transport, self.cache, self.archive = transport, cache, archive
        self.environment = transport.environment
        self.feature_version = feature_version

    def frame(self, target, observed):
        prepared = self.prepare(target, observed)
        self.archive.put(target, prepared["new"])
        return self.cache_prepared(prepared)

    def prepare(self, target, observed):
        symbol(target)
        milliseconds(observed)
        key = f"v2:incremental:{self.environment}:{target}"
        encoded = self.cache.hget(key, "payload")
        prior = json.loads(encoded) if encoded else None
        if prior is not None and (
            prior.get("environment") != self.environment
            or prior.get("symbol") != target
        ):
            raise ValueError("CANDLE_CACHE_SCOPE")
        bars = (
            prior["bars"]
            if prior
            else self.archive.read(target, observed - 86400000, observed)
        )
        if bars and bars[0]["t"] >= observed:
            raise ValueError("CANDLE_CLOCK_REGRESSION")
        start = (
            max(observed - 86400000, bars[0]["t"] + 60000)
            if bars
            else observed - 86400000
        )
        # A hole cannot be concealed by carrying forward the last price.
        new = []
        if start < observed:
            rows = self.transport(
                "/fapi/v1/klines",
                {
                    "symbol": target,
                    "interval": "1m",
                    "startTime": start,
                    "endTime": observed - 1,
                    "limit": (observed - start) // 60000,
                },
            )
            new = normalize_rows(rows, start, observed)
        combined = (new + bars)[:1440]
        batch = {
            "source": "BINANCE_FUTURES",
            "environment": self.environment,
            "interval": "1m",
            "closed_at": observed,
            "candles": {target: combined},
        }
        frame = build_frame(
            batch, environment=self.environment, feature_version=self.feature_version
        )
        version = {
            PRICE_VERSION: "v2",
            ROLLING_VERSION: "v3",
            BREAKOUT_VERSION: "v4",
            CORRECTED_VERSION: "v5",
            STRUCTURE_VERSION: "v6",
        }[self.feature_version]
        frame["frame_id"] = shared_frame_id(f"s3-shared-{version}", target, observed)
        # Normalize decimals exactly as build_frame does before durable hashing.
        from services.v2_s3_candles import amount

        new = [
            {
                **b,
                **{
                    k: amount(b[k], positive=k in ("o", "h", "l", "c"))[0]
                    for k in ("o", "h", "l", "c", "v", "tbv")
                },
            }
            for b in new
        ]
        combined = new + bars[: 1440 - len(new)]
        return {
            "target": target,
            "observed": observed,
            "new": new,
            "combined": combined,
            "frame": frame,
        }

    def cache_prepared(self, prepared):
        target, observed, combined = (
            prepared[k] for k in ("target", "observed", "combined")
        )
        key = f"v2:incremental:{self.environment}:{target}"
        if not RedisProjection(self.cache, "v2:incremental:" + self.environment).put(
            target,
            observed,
            {"environment": self.environment, "symbol": target, "bars": combined},
        ):
            raise ValueError("CANDLE_CACHE_CONFLICT")
        self.cache.expire(key, 21600)
        return prepared["frame"]


def select_batch(universe, heads, *, observed, now, policy, urgent, movers):
    """Fair background slots are reserved even during a permanent TV flood."""
    eligible = [
        s
        for s in universe
        if heads.get(s, {}).get("observed", 0) < observed
        and now - heads.get(s, {}).get("attempted", 0) >= policy.retry_seconds * 1000
    ]
    eligible.sort(key=lambda s: (heads.get(s, {}).get("attempted", 0), s))
    background = eligible[: policy.background_slots]
    remaining = set(eligible) - set(background)
    priority = []
    for target in (*policy.regime_symbols, *urgent, *movers, *eligible):
        if target in remaining:
            priority.append(target)
            remaining.remove(target)
    return background + priority[: policy.batch_symbols - len(background)]


def rank_movers(rows, universe, previous, policy):
    """Recent ticker-to-ticker change outranks an already old 24h move."""
    from services.v2_s3_candles import amount

    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("MARKET_TICKER_INVALID")
    prices = {}
    scored = []
    for row in rows:
        if not isinstance(row, dict) or row.get("symbol") not in universe:
            continue
        target = row["symbol"]
        if target in prices:
            raise ValueError("MARKET_TICKER_DUPLICATE")
        try:
            price = Decimal(amount(row["lastPrice"], positive=True)[0])
            daily = Decimal(row["priceChangePercent"])
            if not daily.is_finite():
                continue
        except (ValueError, KeyError, ArithmeticError):
            continue
        short = (
            abs(price / previous[target] - 1) * 100
            if target in previous
            else Decimal(0)
        )
        scored.append(
            (
                short * policy.mover_short_weight
                + abs(daily) * policy.mover_day_weight,
                target,
            )
        )
        prices[target] = price
    return [s for _, s in sorted(scored, reverse=True)], prices


class SharedMarketHub:
    def __init__(
        self,
        connect,
        *,
        transport,
        cache,
        archive,
        clock_ms,
        monotonic_ms,
        quant_research=None,
        feature_version=PRICE_VERSION,
        publish_signals=True,
        priority_environment=None,
    ):
        environment = getattr(transport, "environment", None)
        priority_environment = (
            environment if priority_environment is None else priority_environment
        )
        if (
            type(publish_signals) is not bool
            or environment not in {"LIVE", "SANDBOX"}
            or priority_environment not in {"LIVE", "SANDBOX"}
            or (publish_signals and priority_environment != environment)
            or (
                not publish_signals
                and (environment != "LIVE" or quant_research is not None)
            )
        ):
            raise ValueError("MARKET_PUBLIC_SHADOW_BINDING_INVALID")
        self.connect, self.transport, self.cache, self.clock, self.monotonic = (
            connect,
            transport,
            cache,
            clock_ms,
            monotonic_ms,
        )
        self.environment = transport.environment
        self.publish_signals = publish_signals
        self.priority_environment = priority_environment
        self.quant_research = quant_research
        self.catalog = Catalog(connect, self.environment, clock_ms)
        self.candles = IncrementalCandles(
            transport, cache, archive, feature_version=feature_version
        )
        self.context = RedisMarketContext(cache, environment=self.environment)
        self.publisher = (
            ProducerPublisher(
                connect,
                source="s3",
                environment=self.environment,
                market=self.context,
                clock_ms=clock_ms,
                max_age_ms=90000,
                lifetime_ms=90000,
            )
            if publish_signals
            else None
        )
        self.regime = (
            S0Publisher(
                ProducerPublisher(
                    connect,
                    source="s0",
                    environment=self.environment,
                    market=self.context,
                    clock_ms=clock_ms,
                    max_age_ms=90000,
                    lifetime_ms=120000,
                )
            )
            if publish_signals
            else None
        )
        self.catalog_at = self.ticker_at = self.time_at = 0
        self.offset = 0
        self.movers = []
        self.ticker_prices = {}

    def _head(self, target, observed, status, reason, frame_id=None):
        with self.connect() as c:
            c.execute(
                "INSERT INTO v2_market_symbol_heads VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(environment,symbol) DO UPDATE SET observed_at_ms=greatest(v2_market_symbol_heads.observed_at_ms,EXCLUDED.observed_at_ms),"
                "attempted_at_ms=EXCLUDED.attempted_at_ms,status=EXCLUDED.status,reason=EXCLUDED.reason,frame_id=EXCLUDED.frame_id",
                (
                    self.environment,
                    target,
                    observed,
                    self.clock(),
                    status,
                    reason,
                    frame_id,
                ),
            )

    def _priorities(self, now):
        with self.connect() as c:
            tv = c.execute(
                "SELECT DISTINCT snapshot->>'symbol' FROM v2_inbound_signals WHERE source='tv_bridge' "
                "AND environment=%s AND received_at>clock_timestamp()-interval '5 minutes' "
                "AND (snapshot->>'observed_at')::bigint<=%s AND (snapshot->>'expires_at_ms')::bigint>%s",
                (self.priority_environment, now, now),
            ).fetchall()
            held = c.execute(
                "SELECT DISTINCT i.payload->>'symbol' FROM v2_risk_reservations r "
                "JOIN v2_trade_intents i ON i.intent_id=r.episode_id WHERE r.status='HELD' AND i.environment=%s",
                (self.priority_environment,),
            ).fetchall()
            requested = c.execute(
                "SELECT symbol FROM v2_market_demands WHERE environment=%s AND expires_at_ms>%s",
                (self.priority_environment, now),
            ).fetchall()
        return [r[0] for r in (*held, *tv, *requested)]

    def _project_regime(self, policy, observed):
        windows = {}
        for target in policy.regime_symbols:
            envelope = self.context.read("s3", self.environment, target)
            if envelope is None or envelope["observed_at"] != observed:
                return "WAITING_BENCHMARK"
            windows[target] = envelope["features"]
        fingerprint = digest(canonical(windows))
        state = build_s0_state(
            windows,
            universe=policy.regime_symbols,
            s3_frame_id="shared-basket:" + fingerprint,
            s3_input_digest=fingerprint,
        )
        state["evidence"]["breadth_scope"] = "CONFIGURED_BENCHMARK_BASKET"
        if not self.publish_signals:
            return self._project_shadow(
                "s0", "*", observed, state, max_age_ms=policy.context_max_age_ms
            )
        result = self.regime.publish_state(
            state, frame_id="s0-shared:" + fingerprint, observed_at=observed
        )
        return result["market_status"]

    def _project_shadow(self, source, target, observed, features, *, max_age_ms=90000):
        """Public observations only: no producer batches, lifecycle or ingress.

        Candles have already been durably archived by run_once. Redis remains
        reconstructible; a restart never invents a signal or a trade receipt.
        """
        if self.publish_signals or self.environment != "LIVE":
            raise ValueError("MARKET_PUBLIC_SHADOW_BINDING_INVALID")
        # The normal publisher checks age after durable storage, not merely at
        # collection start. Shadow must preserve that barrier too: a slow CH
        # insert or clock jump cannot make stale/future data appear CURRENT.
        if not 0 <= milliseconds(self.clock()) - milliseconds(observed) < max_age_ms:
            raise ValueError("MARKET_PUBLIC_SHADOW_STALE_OR_FUTURE")
        if (
            self.context.put(
                market_envelope(
                    source, self.environment, target, observed, feature_values(features)
                )
            )
            is not True
        ):
            raise ValueError("MARKET_PROJECTION_UNCONFIRMED")
        return "PROJECTED"

    def run_once(self):
        policy = MarketPolicy.read(self.connect, self.environment)
        quant_policy = None
        quant_policy_error = None
        if self.quant_research is not None:
            from services.v2_quant_research import QuantPolicy

            try:
                quant_policy = QuantPolicy.read(self.connect, self.environment)
            except Exception as exc:  # noqa: BLE001 - stop research, not legacy market/protection data
                quant_policy_error = diagnostic_code(exc)
        started = self.monotonic()
        now = self.clock()
        if now - self.catalog_at >= policy.catalog_seconds * 1000:
            self.catalog.save(self.transport("/fapi/v1/exchangeInfo", {}))
            self.catalog_at = now
        listed = self.catalog.read(policy.catalog_max_age_seconds * 1000)
        universe = {name: row for name, row in listed.items() if row["supported"]}
        if now - self.time_at >= policy.ticker_seconds * 1000:
            before = self.clock()
            server = self.transport("/fapi/v1/time", {})["serverTime"]
            after = self.clock()
            if not before - 5000 <= server <= after + 5000 or after < before:
                raise ValueError("MARKET_CLOCK_SKEW")
            self.offset = server - after
            self.time_at = after
        observed = (now + self.offset - 2000) // 60000 * 60000
        if now - self.ticker_at >= policy.ticker_seconds * 1000:
            rows = self.transport("/fapi/v1/ticker/24hr", {})
            self.movers, self.ticker_prices = rank_movers(
                rows, universe, self.ticker_prices, policy
            )
            self.ticker_at = now
        with self.connect() as c:
            rows = c.execute(
                "SELECT symbol,observed_at_ms,attempted_at_ms FROM v2_market_symbol_heads WHERE environment=%s",
                (self.environment,),
            ).fetchall()
        heads = {s: {"observed": o, "attempted": a} for s, o, a in rows}
        batch = select_batch(
            universe,
            heads,
            observed=observed,
            now=now,
            policy=policy,
            urgent=self._priorities(now),
            movers=self.movers,
        )
        results = []
        prepared = []
        for target in batch:
            if self.monotonic() - started >= policy.cycle_budget_ms:
                break
            try:
                prepared.append(self.candles.prepare(target, observed))
            except Exception as exc:  # noqa: BLE001 - safe diagnostic code only
                reason = diagnostic_code(exc)
                self._head(target, 0, "RETRY", reason)
                results.append({"symbol": target, "status": "RETRY", "reason": reason})
        self.candles.archive.put_many({p["target"]: p["new"] for p in prepared})
        quant_spent = 0
        for item in prepared:
            target = item["target"]
            try:
                frame = self.candles.cache_prepared(item)
                if self.publish_signals:
                    result = S3Runtime(self.publisher, state_partition=target).process(
                        **frame
                    )
                else:
                    result = {
                        "status": "RECORDED",
                        "market_status": self._project_shadow(
                            "s3",
                            target,
                            observed,
                            frame["windows"][target],
                            max_age_ms=policy.context_max_age_ms,
                        ),
                    }
                if (
                    result["status"] != "RECORDED"
                    or result["market_status"] != "PROJECTED"
                ):
                    raise ValueError("MARKET_PROJECTION_UNCONFIRMED")
                self._head(target, observed, "CURRENT", "OK", frame["frame_id"])
                heads[target] = {"observed": observed, "attempted": self.clock()}
                measured = {"symbol": target, "status": "CURRENT"}
                if quant_policy is not None and quant_policy.mode != "OFF":
                    quant_started = self.monotonic()
                    if quant_spent >= quant_policy.cycle_budget_ms:
                        measured["quant"] = {"status": "BUDGET_EXHAUSTED"}
                    else:
                        try:
                            measured["quant"] = self.quant_research.observe(
                                target, item["combined"], observed, policy=quant_policy
                            )
                        except Exception as exc:  # noqa: BLE001 - a research fault cannot disable position management
                            measured["quant"] = {
                                "status": "RETRY",
                                "reason": diagnostic_code(exc),
                            }
                    quant_spent += max(0, self.monotonic() - quant_started)
                results.append(measured)
            except Exception as exc:  # noqa: BLE001 - safe diagnostic code only
                reason = diagnostic_code(exc)
                self._head(target, 0, "RETRY", reason)
                results.append({"symbol": target, "status": "RETRY", "reason": reason})
        regime = self._project_regime(policy, observed)
        fresh = sum(
            1
            for s in universe
            if 0
            <= self.clock() - heads.get(s, {}).get("observed", 0)
            <= policy.context_max_age_ms
        )
        report = {
            "market_environment": self.environment,
            "priority_environment": self.priority_environment,
            "publication_mode": "SIGNALS" if self.publish_signals else "PUBLIC_SHADOW",
            "publishes_signals": self.publish_signals,
            "status": "CURRENT" if regime == "PROJECTED" else "WARMING",
            "regime": regime,
            "universe": len(universe),
            "listed": len(listed),
            "lightweight_coverage": len(self.ticker_prices),
            "unsupported_symbols": [
                name for name, row in listed.items() if not row["supported"]
            ],
            "fresh": fresh,
            "pending": len(universe) - fresh,
            "elapsed_ms": self.monotonic() - started,
            "results": results,
            "quant": {
                "mode": "INVALID"
                if quant_policy_error
                else quant_policy.mode
                if quant_policy
                else "OFF",
                **({"policy_error": quant_policy_error} if quant_policy_error else {}),
                "elapsed_ms": quant_spent,
                "failures": int(quant_policy_error is not None)
                + sum(r.get("quant", {}).get("status") == "RETRY" for r in results),
                "budget_skips": sum(
                    r.get("quant", {}).get("status") == "BUDGET_EXHAUSTED"
                    for r in results
                ),
            },
            "policy": policy.values(),
        }
        with self.connect() as c:
            c.execute(
                "INSERT INTO v2_market_hub_status VALUES(%s,%s,%s::jsonb) ON CONFLICT(environment) "
                "DO UPDATE SET observed_at_ms=EXCLUDED.observed_at_ms,payload=EXCLUDED.payload",
                (self.environment, self.clock(), canonical(report)),
            )
        return report
