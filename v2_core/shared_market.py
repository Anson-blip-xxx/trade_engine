"""Shared market contracts, bounded policy and host-wide atomic request budget."""

from dataclasses import asdict, dataclass

from v2_core.evidence import canonical, digest
from v2_core.ingress import IntakeRejected, symbol
from v2_core.state import BusinessState, StateKey


@dataclass(frozen=True)
class MarketPolicy:
    catalog_seconds: int = 300
    catalog_max_age_seconds: int = 900
    ticker_seconds: int = 30
    batch_symbols: int = 16
    background_slots: int = 4
    cycle_budget_ms: int = 8000
    interval_ms: int = 500
    context_max_age_ms: int = 90000
    retry_seconds: int = 30
    fusion_window_ms: int = 60000
    shared_weight_limit: int = 1800
    market_weight_limit: int = 900
    minimum_free_disk_gib: int = 8
    mover_short_weight: int = 100
    mover_day_weight: int = 1
    regime_symbols: tuple = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")

    def __post_init__(self):
        limits = {
            "catalog_seconds": (30, 3600),
            "catalog_max_age_seconds": (60, 7200),
            "ticker_seconds": (10, 300),
            "batch_symbols": (4, 100),
            "background_slots": (1, 25),
            "cycle_budget_ms": (1000, 15000),
            "interval_ms": (100, 10000),
            "context_max_age_ms": (60000, 180000),
            "retry_seconds": (1, 300),
            "fusion_window_ms": (1000, 300000),
            "shared_weight_limit": (100, 2400),
            "market_weight_limit": (50, 1800),
            "minimum_free_disk_gib": (4, 100),
            "mover_short_weight": (1, 1000),
            "mover_day_weight": (0, 1000),
        }
        for field, (lo, hi) in limits.items():
            value = getattr(self, field)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError("MARKET_POLICY_BOUNDS")
        if (
            self.background_slots >= self.batch_symbols
            or self.market_weight_limit >= self.shared_weight_limit
            or self.catalog_seconds >= self.catalog_max_age_seconds
        ):
            raise ValueError("MARKET_POLICY_RESERVES_REQUIRED")
        if (
            not isinstance(self.regime_symbols, (tuple, list))
            or not 1 <= len(self.regime_symbols) <= 20
        ):
            raise ValueError("REGIME_BASKET_REQUIRED")
        for target in self.regime_symbols:
            symbol(target)
        if "BTCUSDT" not in self.regime_symbols or len(set(self.regime_symbols)) != len(
            self.regime_symbols
        ):
            raise ValueError("REGIME_BASKET_INVALID")

    @classmethod
    def read(cls, connect, environment):
        key = StateKey(
            "BINANCE",
            "market-data",
            environment,
            "FUTURES",
            "shared-market-policy",
            "current",
        )
        row = BusinessState(connect).read(key)
        if row is None:
            return cls()
        if row.deleted:
            raise ValueError("MARKET_POLICY_DISABLED")
        import json

        return cls(**json.loads(row.payload_json))

    def values(self):
        return {**asdict(self), "regime_symbols": list(self.regime_symbols)}


def contracts(response):
    rows = response.get("symbols") if isinstance(response, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError("CATALOG_INVALID")
    selected = {}
    seen = set()
    for row in rows:
        name = row.get("symbol") if isinstance(row, dict) else None
        if not isinstance(name, str) or not 1 <= len(name) <= 80:
            raise ValueError("CATALOG_SYMBOL_INVALID")
        if name in seen:
            raise ValueError("CATALOG_DUPLICATE")
        seen.add(name)
        if (
            row.get("status") == "TRADING"
            and row.get("contractType") == "PERPETUAL"
            and row.get("quoteAsset") == "USDT"
        ):
            if not isinstance(row.get("filters"), list) or not row["filters"]:
                raise ValueError("CATALOG_RULES_MISSING")
            selected[name] = {
                key: row[key]
                for key in ("symbol", "status", "contractType", "quoteAsset", "filters")
            }
            try:
                symbol(name)
                selected[name]["supported"] = True
            except IntakeRejected:
                selected[name]["supported"] = False
    if not selected:
        raise ValueError("CATALOG_EMPTY")
    return selected


class Catalog:
    def __init__(self, connect, environment, clock_ms):
        if environment not in {"SANDBOX", "LIVE"}:
            raise ValueError("MARKET_ENVIRONMENT_REQUIRED")
        self.connect, self.environment, self.clock = connect, environment, clock_ms

    def save(self, response):
        data = contracts(response)
        now = self.clock()
        encoded = canonical(data)
        with self.connect() as c:
            c.execute(
                "INSERT INTO v2_market_catalog VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT(environment) DO UPDATE SET observed_at_ms=EXCLUDED.observed_at_ms,digest=EXCLUDED.digest,contracts=EXCLUDED.contracts WHERE v2_market_catalog.observed_at_ms<=EXCLUDED.observed_at_ms",
                (self.environment, now, digest(encoded), encoded),
            )
        return data

    def read(self, max_age_ms):
        return self.snapshot(max_age_ms)["contracts"]

    def snapshot(self, max_age_ms):
        with self.connect() as c:
            row = c.execute(
                "SELECT observed_at_ms,contracts,digest FROM v2_market_catalog WHERE environment=%s",
                (self.environment,),
            ).fetchone()
        if row is None or not 0 <= self.clock() - row[0] <= max_age_ms:
            raise ValueError("CONTRACT_CATALOG_STALE")
        if digest(canonical(row[1])) != row[2]:
            raise ValueError("CONTRACT_CATALOG_CORRUPT")
        return {"observed_at_ms": row[0], "contracts": row[1], "digest": row[2]}


class HostBudget:
    """Every account shares one host bucket. Market traffic cannot consume reserve."""

    max_request_weight = 100

    def __init__(self, connect, *, environment, lane="management"):
        if environment not in {"SANDBOX", "LIVE"} or lane not in {
            "market",
            "management",
        }:
            raise ValueError("EGRESS_SCOPE_INVALID")
        self.connect, self.environment, self.lane = connect, environment, lane
        self.host = {"SANDBOX": "demo-fapi.binance.com", "LIVE": "fapi.binance.com"}[
            environment
        ]

    def permit(self, weight):
        if type(weight) is not int or not 1 <= weight <= 100:
            raise ValueError("EGRESS_WEIGHT_INVALID")
        policy = MarketPolicy.read(self.connect, self.environment)
        with self.connect() as c:
            c.execute(
                "INSERT INTO v2_egress_budgets(host) VALUES(%s) ON CONFLICT DO NOTHING",
                (self.host,),
            )
            window, used, market, blocked, now = c.execute(
                "SELECT window_id,used_weight,market_weight,blocked_until>clock_timestamp(),floor(extract(epoch FROM clock_timestamp())/60)::bigint FROM v2_egress_budgets WHERE host=%s FOR UPDATE",
                (self.host,),
            ).fetchone()
            if now < window or blocked:
                return False
            if window != now:
                used = market = 0
            market_next = market + (weight if self.lane == "market" else 0)
            if (
                used + weight > policy.shared_weight_limit
                or market_next > policy.market_weight_limit
            ):
                return False
            c.execute(
                "UPDATE v2_egress_budgets SET window_id=%s,used_weight=%s,market_weight=%s WHERE host=%s",
                (now, used + weight, market_next, self.host),
            )
        return True

    def penalize(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 259200:
            raise ValueError("EGRESS_COOLDOWN_INVALID")
        with self.connect() as c:
            c.execute(
                "UPDATE v2_egress_budgets SET blocked_until=greatest(blocked_until,clock_timestamp()+make_interval(secs=>%s)) WHERE host=%s",
                (seconds, self.host),
            )
