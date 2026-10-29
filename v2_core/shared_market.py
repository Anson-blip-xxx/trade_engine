"""Shared market contracts, bounded policy and host-wide atomic request budget."""

import os
from dataclasses import asdict, dataclass
from threading import Lock

from v2_core.evidence import canonical, digest
from v2_core.ingress import IntakeRejected, identity, milliseconds, symbol
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


@dataclass(frozen=True)
class AdmissionBudgetPolicy:
    """Independent policy: old MarketPolicy readers remain compatible."""

    admission_reserved_weight: int = 0
    market_reserved_weight: int = 0

    def __post_init__(self):
        if (
            type(self.admission_reserved_weight) is not int
            or not 0 <= self.admission_reserved_weight <= 1750
            or type(self.market_reserved_weight) is not int
            or not 0 <= self.market_reserved_weight <= 1800
        ):
            raise ValueError("ADMISSION_BUDGET_POLICY_BOUNDS")

    def validate_against(self, market_policy):
        if (
            not isinstance(market_policy, MarketPolicy)
            or self.admission_reserved_weight > market_policy.market_weight_limit - 50
            or self.admission_reserved_weight + self.market_reserved_weight
            > market_policy.market_weight_limit
        ):
            raise ValueError("ADMISSION_BUDGET_POLICY_RESERVE_INVALID")
        return self

    @classmethod
    def read(cls, connect, environment):
        if environment not in {"SANDBOX", "LIVE"}:
            raise ValueError("EGRESS_SCOPE_INVALID")
        row = BusinessState(connect).read(
            StateKey(
                "BINANCE",
                "market-data",
                environment,
                "FUTURES",
                "admission-budget-policy",
                "current",
            )
        )
        if row is None:
            return cls()
        if row.deleted:
            raise ValueError("ADMISSION_BUDGET_POLICY_DISABLED")
        import json

        return cls(**json.loads(row.payload_json))

    def values(self):
        return asdict(self)


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
    """One host bucket; market plus admission cannot consume management reserve.

    The existing ``market_weight`` column is the non-critical aggregate: both
    public collection and candidate account/public reads consume it. Keeping
    this shared counter avoids a schema migration or a separate hidden quota.
    """

    max_request_weight = 100

    def __init__(self, connect, *, environment, lane="management"):
        if environment not in {"SANDBOX", "LIVE"} or lane not in {
            "market",
            "admission",
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
        return self._reserve(weight) is not None

    @property
    def market_lane_scope(self):
        return "v2-host-lane:market:" + self.host

    def _locked_state(self, conn):
        conn.execute(
            "INSERT INTO v2_egress_budgets(host) VALUES(%s) ON CONFLICT DO NOTHING",
            (self.host,),
        )
        # Read the clock after obtaining the host lock. A caller waiting for
        # this lock must not reserve against the minute in which it started.
        window, used, market, blocked_until = conn.execute(
            "SELECT window_id,used_weight,market_weight,blocked_until::text "
            "FROM v2_egress_budgets WHERE host=%s FOR UPDATE",
            (self.host,),
        ).fetchone()
        now_ms, blocked = conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint,"
            "%s::timestamptz>clock_timestamp()",
            (blocked_until,),
        ).fetchone()
        if (
            any(
                type(value) is not int or value < 0
                for value in (window, used, market, now_ms)
            )
            or type(blocked) is not bool
            or market > used
        ):
            raise ValueError("EGRESS_LEDGER_INVALID")
        return window, used, market, blocked, now_ms

    def _market_lane(self, conn, *, window, market):
        # The separate counter is NOT another host budget. It only prevents
        # collection from taking the configured admission share. Old binaries
        # did not record their lane, so the first observation conservatively
        # treats all earlier noncritical use as market use, never zero.
        conn.execute(
            "INSERT INTO v2_public_market_budgets(scope,weight_limit,window_id,used_weight) "
            "VALUES(%s,2400,%s,%s) ON CONFLICT DO NOTHING",
            (self.market_lane_scope, window, market),
        )
        lane_window, lane_used = conn.execute(
            "SELECT window_id,used_weight FROM v2_public_market_budgets "
            "WHERE scope=%s FOR UPDATE",
            (self.market_lane_scope,),
        ).fetchone()
        if any(
            type(value) is not int or value < 0 for value in (lane_window, lane_used)
        ):
            raise ValueError("EGRESS_LANE_LEDGER_INVALID")
        if lane_window > window:
            return None
        return lane_used if lane_window == window else 0

    def _reserve(self, weight, *, deadline_ms=None):
        policy = MarketPolicy.read(self.connect, self.environment)
        admission_policy = (
            AdmissionBudgetPolicy.read(self.connect, self.environment).validate_against(
                policy
            )
            if self.lane != "management"
            else None
        )
        with self.connect() as c:
            window, used, market, blocked, now_ms = self._locked_state(c)
            now = now_ms // 60000
            if now < window or blocked:
                return None
            if deadline_ms is not None and not (
                now_ms < deadline_ms <= now_ms + 60000
                and deadline_ms + 500 <= (now + 1) * 60000
            ):
                return None
            if window != now:
                used = market = 0
            market_next = market + (weight if self.lane != "management" else 0)
            if (
                used + weight > policy.shared_weight_limit
                or market_next > policy.market_weight_limit
            ):
                return None
            lane_used = None
            if self.lane != "management":
                lane_used = self._market_lane(c, window=now, market=market)
                if lane_used is None or (
                    self.lane == "market"
                    and admission_policy.admission_reserved_weight
                    and lane_used + weight
                    > policy.market_weight_limit
                    - admission_policy.admission_reserved_weight
                ):
                    return None
                if self.lane == "admission" and admission_policy.market_reserved_weight:
                    # All noncritical requests share the host counter. The
                    # market counter is maintained under the same lock, so
                    # their difference is admission use, not another budget.
                    if lane_used > market:
                        raise ValueError("EGRESS_LANE_LEDGER_INVALID")
                    if (
                        market - lane_used + weight
                        > policy.market_weight_limit
                        - admission_policy.market_reserved_weight
                    ):
                        return None
            c.execute(
                "UPDATE v2_egress_budgets SET window_id=%s,used_weight=%s,market_weight=%s WHERE host=%s",
                (now, used + weight, market_next, self.host),
            )
            if self.lane == "market":
                c.execute(
                    "UPDATE v2_public_market_budgets SET window_id=%s,used_weight=%s "
                    "WHERE scope=%s",
                    (now, lane_used + weight, self.market_lane_scope),
                )
        return now, now_ms

    def penalize(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 259200:
            raise ValueError("EGRESS_COOLDOWN_INVALID")
        with self.connect() as c:
            c.execute(
                "UPDATE v2_egress_budgets SET blocked_until=greatest(blocked_until,clock_timestamp()+make_interval(secs=>%s)) WHERE host=%s",
                (seconds, self.host),
            )


class _ContextTicket:
    """Process-local, nontransferable capability for already charged GETs.

    No business state lives here: the entire charge is durable in PG before
    creation. Losing this object burns unused weight until rollover, never
    refunds a request with an uncertain network outcome.
    """

    __slots__ = (
        "account",
        "created_ms",
        "deadline_ms",
        "environment",
        "host",
        "last_ms",
        "lock",
        "owner",
        "pid",
        "remaining",
        "window",
    )

    def __init__(self, owner, account, target, adaptive, window, now_ms, deadline_ms):
        self.owner, self.pid = owner, os.getpid()
        self.account, self.environment, self.host = (
            account,
            owner.environment,
            owner.host,
        )
        self.window, self.created_ms, self.last_ms = window, now_ms, now_ms
        self.deadline_ms, self.lock = deadline_ms, Lock()
        self.remaining = {
            ("/fapi/v3/positionRisk", "{}", 5): 2,
            ("/fapi/v3/account", "{}", 5): 1,
            ("/fapi/v1/accountConfig", "{}", 5): 1,
            ("/fapi/v1/symbolConfig", canonical({"symbol": target}), 5): 1,
            ("/fapi/v1/exchangeInfo", "{}", 1): 1,
            ("/fapi/v1/premiumIndex", canonical({"symbol": target}), 1): 1,
        }
        if adaptive:
            self.remaining.update(
                {
                    ("/fapi/v1/leverageBracket", canonical({"symbol": target}), 1): 1,
                    ("/fapi/v1/ticker/bookTicker", canonical({"symbol": target}), 2): 1,
                }
            )


class AdmissionBudget(HostBudget):
    """Candidate-local quota deferral, before any signed/public network call.

    Only a local budget denial becomes this expected wait. DB failures and
    actual venue 429/permission/network errors retain their original semantics.
    No fallback to the management lane and no cached account observations.
    """

    def __init__(self, connect, *, environment):
        super().__init__(connect, environment=environment, lane="admission")

    def permit(self, weight):
        if not super().permit(weight):
            raise ValueError("ADMISSION_BUDGET_UNAVAILABLE")
        return True

    def reserve_context(self, *, account_id, symbol: str, adaptive, deadline_ms):
        identity(account_id)
        # Avoid shadowing the imported validator in this public API.
        from v2_core.ingress import symbol as validate_symbol

        target = validate_symbol(symbol)
        milliseconds(deadline_ms)
        if type(adaptive) is not bool:
            raise ValueError("ADMISSION_CONTEXT_TICKET_INVALID")
        charged = self._reserve(30 if adaptive else 27, deadline_ms=deadline_ms)
        if charged is None:
            raise ValueError("ADMISSION_BUDGET_UNAVAILABLE")
        window, now_ms = charged
        return _ContextTicket(
            self, account_id, target, adaptive, window, now_ms, deadline_ms
        )

    def consume_context(
        self, ticket, *, account_id, environment, method, path, params, weight
    ):
        self._ticket_binding(ticket, account_id, environment)
        if (
            method != "GET"
            or type(params) is not dict
            or type(weight) is not int
            or not isinstance(path, str)
            or any(type(k) is not str or type(v) is not str for k, v in params.items())
        ):
            raise ValueError("ADMISSION_CONTEXT_TICKET_INVALID")
        key = (path, canonical(params), weight)
        with ticket.lock:
            if ticket.remaining.get(key, 0) < 1:
                raise ValueError("ADMISSION_CONTEXT_TICKET_INVALID")
            self._ticket_current(ticket)
            # This only consumes the already charged route allowance. Never
            # call permit(), extend the deadline, renew or refund the ticket.
            ticket.remaining[key] -= 1
        return True

    def _ticket_binding(self, ticket, account_id, environment):
        if (
            type(ticket) is not _ContextTicket
            or ticket.owner is not self
            or ticket.pid != os.getpid()
            or (ticket.account, ticket.environment, ticket.host)
            != (account_id, environment, self.host)
            or self.environment != environment
        ):
            raise ValueError("ADMISSION_CONTEXT_TICKET_INVALID")

    def _ticket_current(self, ticket):
        # Caller holds the ticket lock. PG is authoritative even after DNS/TLS
        # establishment: the original charge cannot authorize a later minute.
        policy = MarketPolicy.read(self.connect, self.environment)
        AdmissionBudgetPolicy.read(self.connect, self.environment).validate_against(
            policy
        )
        with self.connect() as conn:
            window, used, market, blocked, now_ms = self._locked_state(conn)
            if (
                window != ticket.window
                or now_ms // 60000 != ticket.window
                or not ticket.created_ms
                <= ticket.last_ms
                <= now_ms
                < ticket.deadline_ms
                or blocked
                or used > policy.shared_weight_limit
                or market > policy.market_weight_limit
            ):
                raise ValueError("ADMISSION_BUDGET_UNAVAILABLE")
        ticket.last_ms = now_ms

    def revalidate_context(self, ticket, *, account_id, environment):
        self._ticket_binding(ticket, account_id, environment)
        with ticket.lock:
            self._ticket_current(ticket)
        return True
