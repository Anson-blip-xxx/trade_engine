"""Dynamic eligibility and cross-source evidence; never fabricates market facts."""

from types import SimpleNamespace

from v2_core.chunked_archive import ClickHouseChunkedArchive
from v2_core.directional import supports_event
from v2_core.ingress import ContextProvider
from v2_core.producer import RedisMarketContext
from v2_core.public_market import BinancePublicMarket
from v2_core.shared_market import Catalog, HostBudget, MarketPolicy


class SharedMarketStage:
    def __init__(self, connect, *, cache, archive, clock_ms, connection_factory=None):
        self.connect, self.clock = connect, clock_ms
        self.cache = RedisMarketContext(cache, environment="SANDBOX")
        self.catalog = Catalog(connect, "SANDBOX", clock_ms)
        self.source = SimpleNamespace(
            archive=ClickHouseChunkedArchive(archive),
            publisher=SimpleNamespace(environment="SANDBOX"),
        )
        self.collector = SimpleNamespace(
            transport=BinancePublicMarket(
                environment="SANDBOX",
                budget=HostBudget(connect, environment="SANDBOX"),
                enabled=True,
                **(
                    {"connection_factory": connection_factory}
                    if connection_factory
                    else {}
                ),
            )
        )

    def run_once(self):
        policy = MarketPolicy.read(self.connect, "SANDBOX")
        self.catalog.read(policy.catalog_max_age_seconds * 1000)
        with self.connect() as c:
            row = c.execute(
                "SELECT observed_at_ms,payload FROM v2_market_hub_status WHERE environment='SANDBOX'"
            ).fetchone()
        if row is None or not 0 <= self.clock() - row[0] < policy.context_max_age_ms:
            raise ValueError("SHARED_MARKET_HEARTBEAT_STALE")
        regime = self.cache.read("s0", "SANDBOX", "*")
        if (
            regime is None
            or not 0 <= self.clock() - regime["observed_at"] < policy.context_max_age_ms
        ):
            raise ValueError("SHARED_REGIME_STALE")
        return {
            "status": "CURRENT",
            "coverage": row[1].get("fresh"),
            "universe": row[1].get("universe"),
        }


class SharedRegimeStage:
    def __init__(self, market):
        self.market = market
        self.environment = "SANDBOX"

    def run_once(self):
        return {**self.market.run_once(), "status": "PROJECTED"}


class DynamicMarketContext:
    def __init__(self, connect, *, delegate, clock_ms, source):
        if source not in {"s3", "tv_bridge", "quant"}:
            raise ValueError("FUSION_SOURCE_REQUIRED")
        self.source = source
        self.connect, self.delegate, self.clock = connect, delegate, clock_ms
        self.catalog = Catalog(connect, "SANDBOX", clock_ms)

    def __call__(self, signal):
        policy = MarketPolicy.read(self.connect, "SANDBOX")
        from services.v2_signal_fusion_policy import read_policy
        from v2_core.signal_fusion import build_fusion

        fusion_policy, fusion_version = read_policy(self.connect)
        snapshot = self.catalog.snapshot(policy.catalog_max_age_seconds * 1000)
        target = signal["symbol"]
        now = self.clock()
        eligible = snapshot["contracts"].get(target, {}).get("supported") is True
        proof = {
            "eligible": eligible,
            "environment": "SANDBOX",
            "symbol": target,
            "observed_at_ms": snapshot["observed_at_ms"],
            "digest": snapshot["digest"],
            "reason": "LISTED_TRADING_USDT_PERPETUAL"
            if eligible
            else "SYMBOL_NOT_TRADING",
        }
        if not eligible:
            return {"universe_admission": proof}
        if now >= signal["expires_at_ms"]:
            raise ValueError("SIGNAL_EXPIRED_DURING_CONTEXT")
        with self.connect() as c:
            c.execute(
                "INSERT INTO v2_market_demands VALUES('SANDBOX',%s,%s) ON CONFLICT(environment,symbol) "
                "DO UPDATE SET expires_at_ms=greatest(v2_market_demands.expires_at_ms,EXCLUDED.expires_at_ms)",
                (target, signal["expires_at_ms"]),
            )
            others = [s for s in ("s3", "tv_bridge", "quant") if s != self.source]
            rows = c.execute(
                "SELECT signal_id::text,snapshot,source FROM v2_inbound_signals WHERE environment='SANDBOX' "
                "AND source=ANY(%s) AND snapshot->>'symbol'=%s AND received_at>clock_timestamp()-interval '10 minutes' "
                "AND (snapshot->>'observed_at')::bigint BETWEEN %s AND %s "
                "AND (snapshot->>'expires_at_ms')::bigint>%s ORDER BY received_at DESC LIMIT 50",
                (others, target, now - fusion_policy.window_ms, now, now),
            ).fetchall()
        own = "S6" if supports_event("S6", signal["signal"]) else "S8"
        peers = [
            {
                "signal_id": identifier,
                "source": other,
                "environment": "SANDBOX",
                "symbol": s["symbol"],
                "signal": s["signal"],
                "observed_at": s["observed_at"],
                "expires_at_ms": s["expires_at_ms"],
                "relationship": "CONFIRMS"
                if supports_event(own, s["signal"])
                else "CONFLICTS",
            }
            for identifier, s, other in rows
            if supports_event("S6", s["signal"]) or supports_event("S8", s["signal"])
        ]
        fusion = {
            "version": "shared-context-v1",
            "window_ms": policy.fusion_window_ms,
            "peers": peers,
            "rule": "SHARED_MARKET_GATES_NO_AUTOMATIC_SIZE_BOOST",
        }
        if self.source in {"s3", "tv_bridge"} and (
            supports_event("S6", signal["signal"])
            or supports_event("S8", signal["signal"])
        ):
            fusion = build_fusion(
                signal, source=self.source, peers=peers,
                policy=fusion_policy, policy_version=fusion_version, now_ms=now,
            )
        early = getattr(self.delegate, "with_signal_fusion", None)
        if callable(early):
            context = early(signal, fusion)
        else:
            if fusion_policy.enabled and self.source in {"s3", "tv_bridge"}:
                raise ValueError("SIGNAL_FUSION_EARLY_CONTEXT_REQUIRED")
            context = self.delegate(signal)
        context["universe_admission"] = proof
        context.setdefault("signal_fusion", fusion)
        return context


class ConfiguredMarketContext:
    def __init__(self, connect, cache, clock_ms):
        self.connect, self.cache, self.clock = connect, cache, clock_ms

    def __call__(self, signal):
        policy = MarketPolicy.read(self.connect, "SANDBOX")
        return ContextProvider(
            environment="SANDBOX",
            policy={
                "s0": {"scope": "GLOBAL", "max_age_ms": policy.context_max_age_ms},
                "s3": {"scope": "SYMBOL", "max_age_ms": policy.context_max_age_ms},
            },
            read=self.cache.read,
            clock_ms=self.clock,
        )(signal)
