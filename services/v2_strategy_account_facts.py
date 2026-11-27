"""Read-only, bounded SANDBOX facts for the typed candidate producer.

Only the existing bounded Binance transports are accepted, with every signed
write switch disabled. The owner must additionally isolate this synchronous
reader in BoundedProcessJob: socket/SQL timeouts do not bound DNS, an injected
connection factory, or an indefinitely blocked credential/quota dependency.

No legacy signal filter, score, market-state rule, model call or write appears
here. Account/reservation facts are advisory, never a reservation or permission.
All HELD reservations, including unknown openings, retain their full margin as
a conservative upper bound; completed venue exposure can be counted twice.
The final admission still rechecks capital under the account and route locks.
"""

import json
import math
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from fractions import Fraction

from services.v2_strategy_market_reader import ClosedMarketBars
from v2_core.account_risk import AccountScope
from v2_core.adaptive_leverage import venue_facts
from v2_core.capital_model import current_health, portfolio_budget
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds, symbol
from v2_core.managed_portfolio import _margin, _original_capital
from v2_core.precision import amount, decimal_text, money_context
from v2_core.public_market import BinancePublicMarket
from v2_core.runtime_policy import PolicyStore
from v2_core.shared_market import Catalog, contracts
from v2_core.strategy_capital import STRATEGY_CAPITAL_RECORD_SQL, read_strategy_capital
from v2_core.strategy_entry_worker import EntrySignalState
from v2_core.strategy_registry import DeploymentBinding
from v2_core.strategy_sizing import StrategySizingFacts, StrategySizingPolicy
from v2_core.transport import BinanceSignedTransport


@dataclass(frozen=True)
class VerifiedCapitalAccount:
    """Validated venue account projection, not permission to publish or trade.

    The caller may pass account/started/deadline to a separately route-fenced
    capital snapshot publisher. Wallet/equity are copied from the actual venue
    response, never reconstructed from the model's smaller capital_base.
    """

    scope: AccountScope
    account_json: str
    started_at_ms: int
    valid_until_ms: int
    observation_id: str

    @property
    def account(self):
        return json.loads(self.account_json)


def _policy(binding):
    value = binding.config.get("sizing")
    if (
        type(value) is not dict
        or set(value) != {"schema", "policy"}
        or value["schema"] != "strategy-sizing-v1"
    ):
        raise ValueError("STRATEGY_FACTS_SIZING_POLICY_REQUIRED")
    values = dict(value["policy"])
    if type(values.get("allowed_leverages")) is not list:
        raise ValueError("STRATEGY_FACTS_SIZING_POLICY_REQUIRED")
    values["allowed_leverages"] = tuple(values["allowed_leverages"])
    policy = StrategySizingPolicy(**values)
    if binding.config.get("capital") != policy.capital_config():
        raise ValueError("STRATEGY_FACTS_CAPITAL_CONFIG_MISMATCH")
    return policy


def _positions(rows, target):
    if type(rows) is not list or len(rows) > 10000:
        raise ValueError("STRATEGY_FACTS_POSITIONS_INVALID")
    normalized = []
    for row in rows:
        if type(row) is not dict or row.get("positionSide") != "BOTH":
            raise ValueError("STRATEGY_FACTS_ONE_WAY_REQUIRED")
        name = symbol(row.get("symbol"))
        quantity = decimal_text(amount(row.get("positionAmt")))
        if not name.endswith("USDT") and amount(quantity) != 0:
            raise ValueError("STRATEGY_FACTS_NON_USDT_EXPOSURE_UNSUPPORTED")
        if name == target and amount(quantity) != 0:
            raise ValueError("STRATEGY_FACTS_EXISTING_POSITION")
        normalized.append((name, quantity))
    if len(normalized) != len({name for name, _ in normalized}):
        raise ValueError("STRATEGY_FACTS_DUPLICATE_POSITION")
    return tuple(sorted(normalized))


def _account(raw, config):
    if type(config) is not dict or config.get("canTrade") is not True:
        raise ValueError("STRATEGY_FACTS_ACCOUNT_PERMISSION_REQUIRED")
    if (
        config.get("dualSidePosition") is not False
        or config.get("multiAssetsMargin") is not False
    ):
        raise ValueError("STRATEGY_FACTS_SINGLE_ASSET_ONE_WAY_REQUIRED")
    if (
        type(raw) is not dict
        or type(raw.get("assets")) is not list
        or len(raw["assets"]) > 100
    ):
        raise ValueError("STRATEGY_FACTS_ACCOUNT_INVALID")
    assets = raw["assets"]
    if any(
        type(row) is not dict or type(row.get("asset")) is not str for row in assets
    ):
        raise ValueError("STRATEGY_FACTS_ASSETS_INVALID")
    if len(assets) != len({row["asset"] for row in assets}):
        raise ValueError("STRATEGY_FACTS_ASSETS_INVALID")
    usdt = [row for row in assets if row["asset"] == "USDT"]
    # V3 omits V2's marginAvailable (a multi-assets flag). Single-asset
    # eligibility is established by accountConfig above and the USDT totals;
    # never require an undocumented V3 field or invent its value in fixtures.
    if len(usdt) != 1 or (
        "marginAvailable" in usdt[0] and usdt[0]["marginAvailable"] is not True
    ):
        raise ValueError("STRATEGY_FACTS_USDT_COLLATERAL_REQUIRED")
    names = {
        "totalWalletBalance": "walletBalance",
        "totalMarginBalance": "marginBalance",
        "availableBalance": "availableBalance",
        "totalInitialMargin": "initialMargin",
        "totalPositionInitialMargin": "positionInitialMargin",
        "totalOpenOrderInitialMargin": "openOrderInitialMargin",
    }
    result = {}
    for key, asset_key in names.items():
        value = amount(raw.get(key))
        asset_value = amount(usdt[0].get(asset_key))
        if value < 0 or asset_value < 0 or value != asset_value:
            raise ValueError("STRATEGY_FACTS_USDT_TOTALS_UNVERIFIED")
        result[key] = decimal_text(value)
    # V3 can return idle BTC/USDC wallets even in single-asset mode. Its global
    # totals are USDT-only in that mode, verified against the USDT row above.
    # Do not add or convert those wallets into capital. Require complete zero
    # risk evidence before ignoring them; missing values never mean zero.
    for row in assets:
        if row["asset"] == "USDT":
            continue
        try:
            idle = all(
                amount(row.get(key)) == 0
                for key in (
                    "initialMargin",
                    "positionInitialMargin",
                    "openOrderInitialMargin",
                    "maintMargin",
                    "unrealizedProfit",
                    "crossUnPnl",
                )
            ) and all(
                amount(row.get(key)) >= 0
                for key in (
                    "walletBalance",
                    "marginBalance",
                    "crossWalletBalance",
                    "availableBalance",
                    "maxWithdrawAmount",
                )
            )
        except ValueError:
            idle = False
        if not idle:
            raise ValueError("STRATEGY_FACTS_OTHER_COLLATERAL_UNSUPPORTED")
    if Fraction(result["totalInitialMargin"]) < Fraction(
        result["totalPositionInitialMargin"]
    ) + Fraction(result["totalOpenOrderInitialMargin"]):
        raise ValueError("STRATEGY_FACTS_MARGIN_COMPONENTS_INCONSISTENT")
    return result


def _rules(row, target):
    if (
        type(row) is not dict
        or row.get("symbol") != target
        or row.get("supported") is not True
        or row.get("status") != "TRADING"
        or row.get("contractType") != "PERPETUAL"
        or row.get("quoteAsset") != "USDT"
        or type(row.get("filters")) is not list
        or not 1 <= len(row["filters"]) <= 32
    ):
        raise ValueError("STRATEGY_FACTS_MARKET_UNAVAILABLE")
    filters = row["filters"]
    if any(
        type(item) is not dict or type(item.get("filterType")) is not str
        for item in filters
    ):
        raise ValueError("STRATEGY_FACTS_FILTERS_INVALID")
    found = {item["filterType"]: item for item in filters}
    if len(found) != len(filters):
        raise ValueError("STRATEGY_FACTS_FILTERS_INVALID")
    try:
        lot = found.get("MARKET_LOT_SIZE") or found["LOT_SIZE"]
        price, notional = found["PRICE_FILTER"], found["MIN_NOTIONAL"]
        result = {
            "quantity_step": lot["stepSize"],
            "min_quantity": lot["minQty"],
            "max_quantity": lot["maxQty"],
            "price_tick": price["tickSize"],
            "min_price": price["minPrice"],
            "max_price": price["maxPrice"],
            "min_notional": notional.get("notional", notional.get("minNotional")),
        }
        return {
            name: decimal_text(amount(value, positive=True))
            for name, value in result.items()
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError("STRATEGY_FACTS_FILTERS_INVALID") from None


def _brackets(raw, target):
    rows = raw if type(raw) is list else [raw]
    if len(rows) != 1 or type(rows[0]) is not dict or rows[0].get("symbol") != target:
        raise ValueError("STRATEGY_FACTS_BRACKETS_SCOPE_MISMATCH")
    rows = rows[0].get("brackets")
    if type(rows) is not list or not 1 <= len(rows) <= 100:
        raise ValueError("STRATEGY_FACTS_BRACKETS_INVALID")
    normalized, previous = [], Decimal(0)
    for row in rows:
        if type(row) is not dict:
            raise ValueError("STRATEGY_FACTS_BRACKETS_INVALID")
        # Existing signed JSON transport parses JSON numeric MMR as float.
        # Use the upper neighboring binary float, then ceil to storage precision;
        # this bounds the original JSON rounding interval instead of using a
        # nearest-float decimal as an exact or potentially understated MMR.
        ratio = row.get("maintMarginRatio")
        if type(ratio) is float:
            if not math.isfinite(ratio) or not 0 < ratio < 1:
                raise ValueError("STRATEGY_FACTS_BRACKETS_INVALID")
            with money_context():
                ratio = decimal_text(
                    Decimal.from_float(math.nextafter(ratio, math.inf)).quantize(
                        Decimal("1e-18"), rounding=ROUND_CEILING
                    )
                )
        elif type(ratio) is int:
            ratio = str(ratio)
        ratio = amount(ratio, positive=True)
        bounds = []
        for key in ("notionalFloor", "notionalCap"):
            value = row.get(key)
            value = str(value) if type(value) is int else value
            bounds.append(amount(value))
        floor, cap = bounds
        leverage = row.get("initialLeverage")
        if (
            floor != previous
            or not 0 <= floor < cap
            or ratio >= 1
            or type(leverage) is not int
            or not 1 <= leverage <= 125
        ):
            raise ValueError("STRATEGY_FACTS_BRACKETS_INVALID")
        normalized.append(
            {
                "notionalFloor": decimal_text(floor),
                "notionalCap": decimal_text(cap),
                "maintMarginRatio": decimal_text(ratio),
                "initialLeverage": leverage,
            }
        )
        previous = cap
    return {"symbol": target, "brackets": normalized}


def _held_margin(conn, scope, buffer):
    rows = conn.execute(
        "SELECT r.notional::text,"
        "e.snapshot->'features'->'evaluation'->'market_plan'->>'leverage',"
        + STRATEGY_CAPITAL_RECORD_SQL
        + ",r.reference->'capital',e.evidence_ref,"
        "jsonb_build_object('exchange',i.exchange,'account_id',i.account_id,"
        "'environment',i.environment,'product',i.product) "
        "FROM v2_risk_reservations r LEFT JOIN v2_trade_intents i ON i.intent_id=r.episode_id "
        "LEFT JOIN v2_decision_evidence e USING(evidence_ref) "
        "WHERE r.scope=%s AND r.status='HELD' LIMIT 5001",
        (scope.key,),
    ).fetchall()
    if len(rows) > 5000:
        raise ValueError("STRATEGY_FACTS_RESERVATION_BOUND")
    held = Fraction(0)
    for notional, legacy_leverage, record, capital, evidence_ref, row_scope in rows:
        if evidence_ref is None or row_scope != asdict(scope):
            raise ValueError("STRATEGY_FACTS_RESERVATION_EVIDENCE_REQUIRED")
        strategy = read_strategy_capital(record, scope=scope)
        leverage = (
            Decimal(strategy.plan.leverage)
            if strategy
            else amount(legacy_leverage, positive=True)
        )
        if leverage != leverage.to_integral_value() or not 1 <= leverage <= 125:
            raise ValueError("STRATEGY_FACTS_RESERVATION_LEVERAGE_INVALID")
        original, _ = _original_capital(capital, strategy)
        held += Fraction(
            max(_margin(amount(notional, positive=True), leverage, buffer), original)
        )
    return _margin(held, 1, 1) if held else Decimal(0), len(rows)


class SandboxStrategyAccountFacts:
    def __init__(
        self,
        connect,
        request,
        public_market,
        *,
        scope,
        clock_ms,
        selected_leverage,
        read_timeout_ms,
        collection_timeout_ms,
        pg_statement_timeout_ms=1000,
        stop=None,
        monotonic=time.monotonic,
    ):
        if type(scope) is not AccountScope or (
            scope.exchange,
            scope.environment,
            scope.product,
        ) != ("BINANCE", "SANDBOX", "FUTURES"):
            raise ValueError("STRATEGY_FACTS_SANDBOX_SCOPE_REQUIRED")
        if not all(callable(value) for value in (connect, clock_ms, monotonic)):
            raise TypeError("STRATEGY_FACTS_BOUNDED_PORTS_REQUIRED")
        if type(selected_leverage) is not int or selected_leverage not in {2, 3, 5}:
            raise ValueError("STRATEGY_FACTS_EXPLICIT_LOW_LEVERAGE_REQUIRED")
        if any(
            type(value) is not int
            for value in (
                read_timeout_ms,
                collection_timeout_ms,
                pg_statement_timeout_ms,
            )
        ) or not (
            1000 <= read_timeout_ms <= 10000
            and read_timeout_ms <= collection_timeout_ms <= 30000
            and 10 <= pg_statement_timeout_ms <= 5000
        ):
            raise ValueError("STRATEGY_FACTS_TIMEOUT_BOUNDS_REQUIRED")
        if stop is not None and not callable(getattr(stop, "is_set", None)):
            raise TypeError("STRATEGY_FACTS_STOP_REQUIRED")
        self.connect, self.request, self.public = connect, request, public_market
        self.scope = AccountScope(**asdict(scope))
        self.clock, self.monotonic, self.stop = clock_ms, monotonic, stop
        self.selected_leverage = selected_leverage
        self.read_timeout_ms, self.collection_timeout_ms = (
            read_timeout_ms,
            collection_timeout_ms,
        )
        self.pg_statement_timeout_ms = pg_statement_timeout_ms
        self._verified, self._busy = None, False
        self._validate_transports()

    def _validate_transports(self):
        request, public = self.request, self.public
        if (
            type(request) is not BinanceSignedTransport
            or type(public) is not BinancePublicMarket
            or (request.account_id, request.environment)
            != (self.scope.account_id, "SANDBOX")
            or public.environment != "SANDBOX"
            or public.enabled is not True
            or any(
                getattr(request, name) is not False
                for name in (
                    "_trading",
                    "_cancel",
                    "_order_cancel",
                    "_protection",
                    "_settings",
                )
            )
            or type(request._timeout) is not int
            or not 1000 <= request._timeout * 1000 <= self.read_timeout_ms
            or type(public.timeout) is not int
            or not 1000 <= public.timeout * 1000 <= self.read_timeout_ms
        ):
            raise ValueError("STRATEGY_FACTS_READ_ONLY_TRANSPORT_REQUIRED")

    @property
    def last_verified_account(self):
        snapshot = self._verified
        if snapshot is None or (self.stop is not None and self.stop.is_set()):
            return None
        now = milliseconds(self.clock())
        return (
            snapshot
            if snapshot.started_at_ms <= now < snapshot.valid_until_ms
            else None
        )

    def _check(self, started, monotonic_started, expires):
        if self.stop is not None and self.stop.is_set():
            raise ValueError("STRATEGY_FACTS_STOPPED")
        now = milliseconds(self.clock())
        elapsed = (self.monotonic() - monotonic_started) * 1000
        if (
            not 0 <= elapsed <= self.collection_timeout_ms
            or not started <= now <= started + self.collection_timeout_ms
            or now >= expires
        ):
            raise ValueError("STRATEGY_FACTS_DEADLINE")
        return now

    @contextmanager
    def _read(self):
        with self.connect() as conn:
            if getattr(conn, "autocommit", None) is not False:
                raise ValueError("STRATEGY_FACTS_TRANSACTION_REQUIRED")
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute(
                f"SET LOCAL statement_timeout='{self.pg_statement_timeout_ms}ms'"
            )
            conn.execute("SET LOCAL lock_timeout='250ms'")
            conn.execute(
                f"SET LOCAL idle_in_transaction_session_timeout='{self.pg_statement_timeout_ms + 1000}ms'"
            )
            yield conn

    def __call__(self, state, deployment, frame):
        self._verified = None
        if self._busy:
            raise ValueError("STRATEGY_FACTS_SINGLE_OWNER_REQUIRED")
        self._busy = True
        try:
            return self._collect(state, deployment, frame)
        finally:
            self._busy = False

    def _collect(self, state, deployment, frame):
        self._validate_transports()
        if (
            type(state) is not EntrySignalState
            or type(deployment) is not DeploymentBinding
            or type(frame) is not ClosedMarketBars
        ):
            raise TypeError("STRATEGY_FACTS_TYPED_INPUTS_REQUIRED")
        target = state.snapshot["symbol"]
        if (
            deployment.scope != self.scope
            or state.environment != "SANDBOX"
            or frame.environment != "SANDBOX"
            or frame.symbol != target
            or not target.endswith("USDT")
            or state.candidate is not None
            or state.receipt is not None
            or deployment.mode != "ACTIVE"
        ):
            raise ValueError("STRATEGY_FACTS_INPUT_SCOPE_MISMATCH")
        policy = _policy(deployment)
        if self.selected_leverage not in policy.allowed_leverages:
            raise ValueError("STRATEGY_FACTS_LEVERAGE_POLICY_MISMATCH")
        started, monotonic_started = milliseconds(self.clock()), self.monotonic()
        expires = state.snapshot["expires_at_ms"]
        check = lambda: self._check(started, monotonic_started, expires)
        check()
        if (
            not state.snapshot["observed_at"]
            <= frame.closed_at_ms
            <= frame.available_at_ms
            <= started
        ):
            raise ValueError("STRATEGY_FACTS_MARKET_TIME_MISMATCH")
        with self._read() as conn:
            initial_profile = PolicyStore(lambda: nullcontext(conn), self.scope).read()
            try:
                catalog = Catalog(
                    lambda: nullcontext(conn), "SANDBOX", self.clock
                ).snapshot(policy.max_fact_age_ms)
            except ValueError as exc:
                if str(exc) != "CONTRACT_CATALOG_STALE":
                    raise
                catalog = None
        check()
        settings = initial_profile.values
        if not all(
            settings[name] is True
            for name in (
                "capital.enabled",
                "capital.model_enabled",
                "portfolio_risk.enabled",
            )
        ):
            raise ValueError("STRATEGY_FACTS_CAPITAL_GUARDS_REQUIRED")
        if amount(settings["capital.pool_fraction"]) > Decimal(".70"):
            raise ValueError("STRATEGY_FACTS_RESERVE_FLOOR_REQUIRED")
        if settings["leverage.adaptive_enabled"] and self.selected_leverage not in {
            settings["leverage.low"],
            settings["leverage.medium"],
            settings["leverage.adaptive_high"],
        }:
            raise ValueError("STRATEGY_FACTS_RUNTIME_LEVERAGE_MISMATCH")

        def signed(path, params):
            self._validate_transports()
            before = check()
            value = self.request("GET", path, params)
            check()
            return value, before

        def public(path, params):
            self._validate_transports()
            before = check()
            value = self.public(path, params)
            check()
            return value, before

        before, _ = signed("/fapi/v3/positionRisk", {})
        before = _positions(before, target)
        raw_account, account_at = signed("/fapi/v3/account", {})
        config, config_at = signed("/fapi/v1/accountConfig", {})
        account = _account(raw_account, config)
        symbol_config, symbol_at = signed("/fapi/v1/symbolConfig", {"symbol": target})
        configs = symbol_config if type(symbol_config) is list else [symbol_config]
        if (
            len(configs) != 1
            or type(configs[0]) is not dict
            or configs[0].get("symbol") != target
        ):
            raise ValueError("STRATEGY_FACTS_SYMBOL_CONFIG_MISMATCH")
        max_notional = decimal_text(
            amount(configs[0].get("maxNotionalValue"), positive=True)
        )
        brackets, brackets_at = signed("/fapi/v1/leverageBracket", {"symbol": target})
        brackets = _brackets(brackets, target)
        if catalog is None:
            exchange, filters_at = public("/fapi/v1/exchangeInfo", {})
            rules = _rules(contracts(exchange).get(target), target)
        else:
            filters_at = catalog["observed_at_ms"]
            rules = _rules(catalog["contracts"].get(target), target)
        premium, premium_at = public("/fapi/v1/premiumIndex", {"symbol": target})
        book, book_read_at = public("/fapi/v1/ticker/bookTicker", {"symbol": target})
        if (
            type(premium) is not dict
            or premium.get("symbol") != target
            or type(book) is not dict
            or book.get("symbol") != target
        ):
            raise ValueError("STRATEGY_FACTS_QUOTE_SCOPE_MISMATCH")
        premium_time, book_time = (
            milliseconds(premium.get("time")),
            milliseconds(book.get("time")),
        )
        if any(
            not frame.closed_at_ms <= stamp <= check()
            or check() - stamp > policy.max_quote_age_ms
            for stamp in (premium_time, book_time)
        ):
            raise ValueError("STRATEGY_FACTS_QUOTE_STALE")
        mark = decimal_text(amount(premium.get("markPrice"), positive=True))
        # Only server structure chooses side. Recompute its pure causal decision;
        # never accept TV signal direction/strength as a sizing permission.
        from v2_core.strategy_candidate_builder import CandidatePolicy, build_candidate

        builder = deployment.config.get("candidate_builder")
        if (
            type(builder) is not dict
            or builder.get("schema") != "closed-structure-candidate-v1"
        ):
            raise ValueError("STRATEGY_FACTS_BUILDER_POLICY_REQUIRED")
        decision = build_candidate(
            frame.bars,
            symbol=target,
            environment="SANDBOX",
            closed_at_ms=frame.closed_at_ms,
            available_at_ms=frame.available_at_ms,
            decided_at_ms=check(),
            policy=CandidatePolicy(**builder["policy"]),
        )
        if decision.status != "CANDIDATE":
            raise ValueError("STRATEGY_FACTS_NO_SERVER_CANDIDATE")
        price = decimal_text(
            amount(
                book.get("askPrice" if decision.side == "BUY" else "bidPrice"),
                positive=True,
            )
        )
        with money_context():
            venue = venue_facts(
                brackets,
                book,
                target,
                started=brackets_at,
                finished=check(),
                max_age_ms=policy.max_quote_age_ms,
            )
        # venue_facts is shared with legacy planning and returns the ambient
        # Decimal division's full precision. Typed immutable capital evidence
        # has a bounded scale. Round the exact spread UP, never down, so its
        # serialized shape cannot understate the liquidity risk or depend on
        # the caller's Decimal context.
        spread = (
            Fraction(amount(book["askPrice"], positive=True))
            - Fraction(amount(book["bidPrice"], positive=True))
        ) / Fraction(amount(book["bidPrice"], positive=True))
        units = -(-(spread.numerator * 10**18) // spread.denominator)
        integer, fractional = divmod(units, 10**18)
        venue["spread_fraction"] = decimal_text(amount(f"{integer}.{fractional:018d}"))
        # Venue normalization validates liquidity numbers; explicit endpoint
        # metadata is evidence only, never a general license to call that host.
        venue.update(environment="SANDBOX", source_host="demo-fapi.binance.com")
        after, _ = signed("/fapi/v3/positionRisk", {})
        if before != _positions(after, target):
            raise ValueError("STRATEGY_FACTS_POSITIONS_CHANGED")
        pg_at = check()
        with self._read() as conn:
            bound = lambda: nullcontext(conn)
            current = PolicyStore(bound, self.scope).read()
            if (
                current.digest != initial_profile.digest
                or current.version != initial_profile.version
            ):
                raise ValueError("STRATEGY_FACTS_POLICY_CHANGED")
            with money_context():
                health = current_health(
                    bound, self.scope, account, settings, now_ms=pg_at
                )
                budget = portfolio_budget(
                    conn, self.scope, settings=settings, capital=health
                )
                pending, held_count = _held_margin(
                    conn,
                    self.scope,
                    amount(settings["capital.margin_buffer"], positive=True),
                )
        finished = check()
        account_at = min(account_at, config_at, pg_at)
        quote_at = min(premium_at, book_read_at, premium_time, book_time)
        filters_at = min(filters_at, symbol_at)
        if any(
            finished - stamp > limit
            for stamp, limit in (
                (account_at, policy.max_fact_age_ms),
                (filters_at, policy.max_fact_age_ms),
                (brackets_at, policy.max_fact_age_ms),
                (quote_at, policy.max_quote_age_ms),
            )
        ):
            raise ValueError("STRATEGY_FACTS_COLLECTION_STALE")
        proof = {
            "selected": self.selected_leverage,
            "checks": {},
            "venue": venue,
            "selection": "EXPLICIT_PROVIDER_TIER_NO_MODEL_SCORE",
            "capital_context": {
                "policy_version": current.version,
                "policy_digest": current.digest,
                "pending_mode": "ALL_HELD_FULL_UPPER_BOUND",
                "held_count": held_count,
                "unknown_reservations_not_dropped": True,
            },
        }
        facts = StrategySizingFacts(
            scope=self.scope,
            symbol=target,
            observed_at_ms=finished,
            available_at_ms=finished,
            quote_at_ms=quote_at,
            account_at_ms=account_at,
            filters_at_ms=filters_at,
            entry_price=price,
            mark_price=mark,
            **rules,
            max_notional=max_notional,
            capital_base=health["base"],
            available_margin=account["availableBalance"],
            used_margin=account["totalInitialMargin"],
            pending_reserved_margin=decimal_text(pending),
            remaining_stop_risk=budget["remaining_stop_risk"],
            drawdown_factor=health["factor"],
            selected_leverage=self.selected_leverage,
            leverage_proof_json=canonical(proof),
            margin_buffer=settings["capital.margin_buffer"],
        )
        valid_until = min(expires, account_at + policy.max_fact_age_ms + 1)
        account_json = canonical(account)
        self._verified = VerifiedCapitalAccount(
            self.scope,
            account_json,
            account_at,
            valid_until,
            "typed-account:"
            + digest(
                canonical(
                    {
                        "scope": asdict(self.scope),
                        "account": account,
                        "started": account_at,
                        "finished": finished,
                    }
                )
            ),
        )
        return facts
