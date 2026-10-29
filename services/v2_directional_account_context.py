"""Read-only Binance and PG evidence for directional strategy sizing.

No values are defaulted. Collection persists a normalized snapshot before it is
returned; it never submits, cancels, protects or closes an exchange order.
"""

import json
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from functools import partial
from threading import Lock
from uuid import uuid4

from v2_core.account_risk import AccountPolicy, AccountScope
from v2_core.directional import analysis_adjustment, number
from v2_core.drawdown import BalanceObservation, DrawdownState
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds, symbol
from v2_core.ledger import amount
from v2_core.state import BusinessState, StateKey


def _short_ratio(response, target, *, now, max_age_ms):
    """Validate exchange event time, never the time of a fetch/cache hit."""
    if (
        not isinstance(response, list)
        or not 1 <= len(response) <= 3
        or not isinstance(response[-1], dict)
    ):
        raise ValueError("SHORT_RATIO_MISSING")
    row = response[-1]
    if row.get("symbol") != target:
        raise ValueError("SHORT_RATIO_SCOPE")
    value = format(number(row["shortAccount"], minimum=0, maximum=1).normalize(), "f")
    if type(row.get("timestamp")) is not int:
        raise ValueError("SHORT_RATIO_TIMESTAMP")
    observed = milliseconds(row["timestamp"])
    if not 0 <= now - observed <= max_age_ms:
        raise ValueError("SHORT_RATIO_STALE")
    return value, observed


class _PublicSentimentCache:
    """Small in-memory optimization for one fixed public endpoint only.

    Source timestamps and environments are retained. Unknown, invalid and stale
    responses are never cached. No account/private data enters this cache. The
    striped locks bound memory and coalesce concurrent same-symbol reads without
    serializing all symbols. Restart loses only a performance optimization.
    """

    def __init__(self, public, clock_ms, *, ttl_ms=30000, max_entries=256):
        if (
            type(ttl_ms) is not int
            or not 0 <= ttl_ms <= 60000
            or type(max_entries) is not int
            or not 1 <= max_entries <= 1024
        ):
            raise ValueError("SENTIMENT_CACHE_BOUNDS")
        self.public, self.clock = public, clock_ms
        self.ttl_ms, self.max_entries = ttl_ms, max_entries
        self._entries = OrderedDict()
        self._lock = Lock()
        self._stripes = tuple(Lock() for _ in range(32))

    def read(self, target, *, max_age_ms):
        target = symbol(target)
        key = (self.public.environment, target, "1h", 3)
        with self._stripes[hash(key) % len(self._stripes)]:
            now = milliseconds(self.clock())
            with self._lock:
                cached = self._entries.pop(key, None)
                if cached is not None:
                    received, response = cached
                    if 0 <= now - received < self.ttl_ms:
                        try:
                            _short_ratio(
                                response, target, now=now, max_age_ms=max_age_ms
                            )
                        except (ValueError, TypeError, KeyError):
                            pass  # Fetch again; an old cache is never a fallback.
                        else:
                            self._entries[key] = cached
                            return deepcopy(response)
            response = self.public(
                "/futures/data/globalLongShortAccountRatio",
                {"symbol": target, "period": "1h", "limit": 3},
            )
            received = milliseconds(self.clock())
            _short_ratio(response, target, now=received, max_age_ms=max_age_ms)
            # API limit=3 and a bounded encoded size prevent oversized public
            # responses from creating an unbounded memory cache.
            if self.ttl_ms and len(canonical({"response": response})) <= 16384:
                with self._lock:
                    self._entries[key] = (received, deepcopy(response))
                    while len(self._entries) > self.max_entries:
                        self._entries.popitem(last=False)
            return response


def expected_move(signal):
    kind, features = signal["signal"], signal["features"]
    if kind.startswith("QUANT_"):
        from v2_core.directional import supports_event

        if (
            not (supports_event("S6", kind) or supports_event("S8", kind))
            or features.get("expected_move_model") != "ATR_TARGET_NOT_FORECAST"
        ):
            raise ValueError("QUANT_TARGET_MODEL_REQUIRED")
        return format(
            amount(features["quant_target_move_pct"], positive=True).normalize(), "f"
        )
    field = (
        "chg_15m"
        if kind in {"PULSE_UP", "PULSE_DOWN", "PUMP_UP", "PUMP_DOWN", "PANIC_SELL"}
        else "chg_1h"
        if kind in {"TREND_UP", "TREND_DOWN"}
        else "vol_1h"
        if kind in {"VIOLENT_BULLISH", "VIOLENT_BEARISH"}
        else None
    )
    if field is None or field not in features:
        raise ValueError("EXPECTED_MOVE_EVIDENCE_MISSING")
    return format(abs(amount(features[field])).normalize(), "f")


def _target_position(rows, target):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("INVALID_POSITION_SNAPSHOT")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("INVALID_POSITION_SNAPSHOT")
        name = symbol(row["symbol"])
        side = row["positionSide"]
        if side not in {"BOTH", "LONG", "SHORT"}:
            raise ValueError("INVALID_POSITION_SNAPSHOT")
        quantity = amount(row["positionAmt"])
        normalized.append((name, side, format(quantity, "f")))
    if len(normalized) != len({(name, side) for name, side, _ in normalized}):
        raise ValueError("DUPLICATE_POSITION_IDENTITY")
    if any(
        name == target and amount(quantity) != 0 for name, _, quantity in normalized
    ):
        raise ValueError("EXISTING_SYMBOL_POSITION")
    return sorted(normalized)


def _rules(exchange_info, symbol_config, target):
    if not isinstance(exchange_info, dict) or not isinstance(
        exchange_info.get("symbols"), list
    ):
        raise TypeError("INVALID_EXCHANGE_RULES")
    matches = [row for row in exchange_info["symbols"] if row.get("symbol") == target]
    if (
        len(matches) != 1
        or matches[0].get("status") != "TRADING"
        or matches[0].get("contractType") != "PERPETUAL"
        or matches[0].get("quoteAsset")
        != ("USDC" if target.endswith("USDC") else "USDT")
    ):
        raise ValueError("SYMBOL_NOT_TRADING")
    row = matches[0]
    filters = row.get("filters")
    if not isinstance(filters, list):
        raise TypeError("INVALID_EXCHANGE_RULES")
    by_type = {
        item.get("filterType"): item for item in filters if isinstance(item, dict)
    }
    if len(by_type) != len(filters):
        raise ValueError("DUPLICATE_EXCHANGE_RULE")
    lot = by_type.get("MARKET_LOT_SIZE") or by_type.get("LOT_SIZE")
    price, notional = by_type.get("PRICE_FILTER"), by_type.get("MIN_NOTIONAL")
    if not all(isinstance(item, dict) for item in (lot, price, notional)):
        raise ValueError("REQUIRED_EXCHANGE_RULE_MISSING")
    configs = symbol_config if isinstance(symbol_config, list) else [symbol_config]
    configs = [
        item
        for item in configs
        if isinstance(item, dict) and item.get("symbol") == target
    ]
    if len(configs) != 1:
        raise ValueError("ACCOUNT_SYMBOL_CONFIG_MISSING")
    values = {
        "quantity_step": lot["stepSize"],
        "price_tick": price["tickSize"],
        "min_quantity": lot["minQty"],
        "max_quantity": lot["maxQty"],
        "min_notional": notional.get("notional", notional.get("minNotional")),
        "max_notional": configs[0]["maxNotionalValue"],
    }
    for key, value in values.items():
        values[key] = format(amount(value, positive=True).normalize(), "f")
    return values


def _risk_budget(connect, scope, target):
    with connect() as conn:
        row = conn.execute(
            """SELECT a.version,p.config,COALESCE(sum(r.notional) FILTER
            (WHERE r.status='HELD'),0),count(r.episode_id) FILTER
            (WHERE r.status='HELD') FROM v2_risk_accounts a
            JOIN v2_risk_policies p USING(scope,version)
            LEFT JOIN v2_risk_reservations r USING(scope)
            WHERE a.scope=%s GROUP BY a.version,p.config""",
            (scope.key,),
        ).fetchone()
        held_symbol = conn.execute(
            """SELECT 1 FROM v2_risk_reservations r JOIN v2_trade_intents i ON i.intent_id=r.episode_id
            WHERE r.scope=%s AND r.status='HELD' AND i.payload->>'symbol'=%s LIMIT 1""",
            (scope.key, target),
        ).fetchone()
    if row is None:
        raise ValueError("ACCOUNT_RISK_POLICY_MISSING")
    version, config, held_notional, held_positions = row
    if not isinstance(config, dict):
        raise TypeError("ACCOUNT_RISK_POLICY_INVALID")
    try:
        policy = AccountPolicy(**config)
    except (TypeError, ValueError, KeyError):
        raise ValueError("ACCOUNT_RISK_POLICY_INVALID") from None
    currency = "USDC" if target.endswith("USDC") else "USDT"
    if (policy.max_positions is None or policy.max_notional is None) and held_symbol:
        raise ValueError("EXISTING_SYMBOL_POSITION")
    if policy.currency != currency:
        raise ValueError("ACCOUNT_RISK_CURRENCY_MISMATCH")
    held = amount(format(held_notional, "f"))
    remaining = (
        None
        if policy.max_notional is None
        else amount(policy.max_notional, positive=True) - held
    )
    if (
        policy.max_positions is not None and held_positions >= policy.max_positions
    ) or (remaining is not None and remaining <= 0):
        raise ValueError("ACCOUNT_RISK_CAPACITY_UNAVAILABLE")
    evidence = {
        "policy_version": version,
        "policy": config,
        "held_notional": format(held.normalize(), "f"),
        "held_positions": held_positions,
        "available_notional": None
        if remaining is None
        else format(remaining.normalize(), "f"),
    }
    evidence["digest"] = digest(canonical(evidence))
    return evidence


class BinanceDirectionalAccountContext:
    def __init__(
        self,
        connect,
        request,
        public_market,
        history,
        drawdown,
        *,
        scope,
        clock_ms,
        sentiment_market=None,
        max_age_ms=15000,
        sentiment_max_age_ms=7200000,
        history_max_age_ms=120000,
        sentiment_cache_ttl_ms=30000,
        sentiment_cache_max_entries=256,
        reserve_context=None,
    ):
        sentiment_market = (
            public_market if sentiment_market is None else sentiment_market
        )
        if (
            not isinstance(scope, AccountScope)
            or scope.environment != "SANDBOX"
            or not isinstance(drawdown, DrawdownState)
            or drawdown.key.account_id != scope.account_id
            or (
                getattr(request, "account_id", None),
                getattr(request, "environment", None),
            )
            != (scope.account_id, scope.environment)
            or getattr(public_market, "environment", None) != scope.environment
            or getattr(sentiment_market, "environment", None)
            not in {scope.environment, "LIVE"}
            or not all(
                callable(port) for port in (request, public_market, history, clock_ms)
            )
            or type(max_age_ms) is not int
            or not 1000 <= max_age_ms <= 60000
            or type(sentiment_max_age_ms) is not int
            or not 3600000 <= sentiment_max_age_ms <= 7200000
            or type(history_max_age_ms) is not int
            or not 1000 <= history_max_age_ms <= 600000
            or (reserve_context is not None and not callable(reserve_context))
        ):
            raise ValueError("explicit scoped account context dependencies required")
        self.connect, self.request, self.public, self.sentiment, self.history = (
            connect,
            request,
            public_market,
            sentiment_market,
            history,
        )
        self.drawdown, self.scope, self.clock, self.max_age = (
            drawdown,
            scope,
            clock_ms,
            max_age_ms,
        )
        self.sentiment_max_age, self.history_max_age = (
            sentiment_max_age_ms,
            history_max_age_ms,
        )
        self.reserve_context = reserve_context
        self._sentiment_cache = _PublicSentimentCache(
            sentiment_market,
            clock_ms,
            ttl_ms=sentiment_cache_ttl_ms,
            max_entries=sentiment_cache_max_entries,
        )

    def __call__(self, signal):
        target = symbol(signal["symbol"])
        started = milliseconds(self.clock())
        from v2_core.runtime_policy import PolicyStore

        profile = PolicyStore(self.connect, self.scope).read()
        from v2_core.entry_controls import read as read_entry_controls

        entry_controls = read_entry_controls(self.connect, self.scope)
        budget_enabled = entry_controls.values["sizing.remaining_budget_enabled"]
        if budget_enabled and (
            not target.endswith("USDT")
            or not all(
                profile.values[key]
                for key in (
                    "capital.enabled",
                    "capital.model_enabled",
                    "portfolio_risk.enabled",
                )
            )
        ):
            raise ValueError("SIZING_BUDGET_REQUIRES_USDT_CAPITAL_GUARDS")
        leverage_venue = None
        # Fail fast on an already full PG risk budget. The final budget read
        # below and atomic submission reservation remain authoritative.
        _risk_budget(self.connect, self.scope, target)
        # A stale/invalid public ratio must not consume 7-9 account/public GETs.
        # Validate before any signed request; recheck the original event time at
        # the end of collection and bind it into the returned evidence deadline.
        if self._sentiment_cache.public is not self.sentiment:
            # A replaced public adapter must not reuse another source's cache.
            self._sentiment_cache = _PublicSentimentCache(
                self.sentiment,
                self.clock,
                ttl_ms=self._sentiment_cache.ttl_ms,
                max_entries=self._sentiment_cache.max_entries,
            )
        ratio = self._sentiment_cache.read(target, max_age_ms=self.sentiment_max_age)
        preflight_finished = milliseconds(self.clock())
        _short_ratio(
            ratio, target, now=preflight_finished, max_age_ms=self.sentiment_max_age
        )
        if not started <= preflight_finished <= started + self.max_age:
            raise ValueError("ACCOUNT_CONTEXT_DEADLINE")
        request, public = self.request, self.public
        if self.reserve_context is not None:
            # Reserve the complete read set once. Explicit per-invocation
            # bindings are safe across workers; never mutate shared transports.
            ticket = self.reserve_context(
                account_id=self.scope.account_id,
                symbol=target,
                adaptive=profile.values["leverage.adaptive_enabled"],
                deadline_ms=started + self.max_age,
            )
            if ticket is None:
                raise ValueError("ADMISSION_CONTEXT_TICKET_INVALID")
            request = partial(request, admission_ticket=ticket)
            public = partial(
                public, admission_ticket=ticket, account_id=self.scope.account_id
            )
        before = request("GET", "/fapi/v3/positionRisk", {})
        _target_position(before, target)  # Already-held symbols need no further I/O.
        # Independent GETs only. Never cache balances or parallelise admission /
        # order writes. All reads finish before the second position observation;
        # the original evidence interval, quotas and final risk gate still apply.
        reads = (
            (request, ("GET", "/fapi/v3/account", {})),
            (request, ("GET", "/fapi/v1/accountConfig", {})),
            (request, ("GET", "/fapi/v1/symbolConfig", {"symbol": target})),
            (public, ("/fapi/v1/exchangeInfo", {})),
            (public, ("/fapi/v1/premiumIndex", {"symbol": target})),
        )
        if profile.values["leverage.adaptive_enabled"]:
            reads += (
                (request, ("GET", "/fapi/v1/leverageBracket", {"symbol": target})),
                (public, ("/fapi/v1/ticker/bookTicker", {"symbol": target})),
            )
        with ThreadPoolExecutor(
            max_workers=profile.values["context.read_workers"]
        ) as pool:
            futures = [pool.submit(port, *args) for port, args in reads]
            values, failures = [], []
            for future in futures:
                try:
                    values.append(future.result())
                except Exception as exc:  # noqa: BLE001 - inspect every completed read
                    values.append(None)
                    failures.append(exc)
            if failures:
                # A real 429/DB/network failure must not be hidden by another
                # worker observing the penalty/deadline and denying its ticket.
                primary = next(
                    (
                        exc
                        for exc in failures
                        if not (
                            type(exc) is ValueError
                            and exc.args == ("ADMISSION_BUDGET_UNAVAILABLE",)
                        )
                    ),
                    failures[0],
                )
                raise primary
        account, account_config, config, exchange, premium = values[:5]
        if profile.values["leverage.adaptive_enabled"]:
            from v2_core.adaptive_leverage import venue_facts

            leverage_venue = venue_facts(
                values[5],
                values[6],
                target,
                started=started,
                finished=self.clock(),
                max_age_ms=self.max_age,
            )
            leverage_venue.update(
                environment=self.scope.environment,
                source_host="demo-fapi.binance.com",
            )
        after = request("GET", "/fapi/v3/positionRisk", {})
        # History is part of the same evidence collection interval. Capturing
        # ``finished`` before this PG read made a real advancing clock see the
        # freshly observed history as coming from the future.
        history = self.history(signal)
        risk_budget = _risk_budget(self.connect, self.scope, target)
        finished = milliseconds(self.clock())
        if not started <= finished <= started + self.max_age:
            raise ValueError("ACCOUNT_CONTEXT_DEADLINE")
        positions = _target_position(before, target)
        if positions != _target_position(after, target):
            raise ValueError("POSITIONS_CHANGED_DURING_CONTEXT")
        if not isinstance(account, dict):
            raise TypeError("INVALID_ACCOUNT_CONTEXT")
        if (
            not isinstance(account_config, dict)
            or account_config.get("canTrade") is not True
        ):
            raise ValueError("ACCOUNT_TRADE_PERMISSION_UNVERIFIED")
        if (
            account_config.get("dualSidePosition") is not False
            or account_config.get("multiAssetsMargin") is not False
        ):
            raise ValueError("ACCOUNT_MODE_UNVERIFIED")
        balance = format(
            number(account["totalWalletBalance"], minimum=0).normalize(), "f"
        )
        available = format(
            number(account["availableBalance"], minimum=0).normalize(), "f"
        )
        used = amount(account["totalInitialMargin"]) + amount(
            account["totalOpenOrderInitialMargin"]
        )
        from v2_core.runtime_policy import PolicyStore

        profile = PolicyStore(self.connect, self.scope).read()
        capital_health = None
        stop_budget = None
        if budget_enabled and not all(
            profile.values[key]
            for key in (
                "capital.enabled",
                "capital.model_enabled",
                "portfolio_risk.enabled",
            )
        ):
            raise ValueError("SIZING_BUDGET_REQUIRES_USDT_CAPITAL_GUARDS")
        if profile.values["capital.enabled"]:
            from v2_core.capital_model import current_health

            used = amount(account["totalInitialMargin"])
            capital_health = current_health(
                self.connect, self.scope, account, profile.values
            )
            balance = capital_health["base"]
            if profile.values["portfolio_risk.enabled"]:
                from v2_core.capital_model import portfolio_budget

                with self.connect() as conn:
                    stop_budget = portfolio_budget(
                        conn,
                        self.scope,
                        settings=profile.values,
                        capital=capital_health,
                    )
        finished = milliseconds(self.clock())
        if not started <= finished <= started + self.max_age:
            raise ValueError("ACCOUNT_CONTEXT_DEADLINE")
        if used < 0:
            raise ValueError("INVALID_USED_MARGIN")
        rules = _rules(exchange, config, target)
        if risk_budget["available_notional"] is not None:
            rules["max_notional"] = format(
                min(
                    amount(rules["max_notional"], positive=True),
                    amount(risk_budget["available_notional"], positive=True),
                ).normalize(),
                "f",
            )
        short_ratio, ratio_at = _short_ratio(
            ratio, target, now=finished, max_age_ms=self.sentiment_max_age
        )
        if not isinstance(premium, dict) or premium.get("symbol") != target:
            raise ValueError("FUNDING_RATE_MISSING")
        funding = format(
            number(premium["lastFundingRate"], minimum=-1, maximum=1).normalize(), "f"
        )
        if type(premium.get("time")) is not int:
            raise ValueError("FUNDING_RATE_TIMESTAMP")
        funding_at = milliseconds(premium["time"])
        if not 0 <= finished - funding_at <= self.max_age:
            raise ValueError("FUNDING_RATE_STALE")
        sizing_budget = {}
        if budget_enabled:
            if premium.get("markPrice") is None:
                raise ValueError("SIZING_MARK_PRICE_MISSING")
            sizing_budget = {
                "remaining_portfolio_risk": stop_budget["remaining_stop_risk"],
                "sizing_mark_price": format(
                    amount(premium["markPrice"], positive=True), "f"
                ),
                "price_drift_fraction": entry_controls.values[
                    "sizing.price_drift_fraction"
                ],
            }
        if (
            not isinstance(history, dict)
            or set(history) != {"stats", "evidence", "observed_at_ms", "valid_until_ms"}
            or not isinstance(history["stats"], dict)
            or not isinstance(history["evidence"], dict)
            or not history["evidence"]
            or type(history["observed_at_ms"]) is not int
            or type(history["valid_until_ms"]) is not int
            or not history["observed_at_ms"] <= finished < history["valid_until_ms"]
            or finished - history["observed_at_ms"] > self.history_max_age
        ):
            raise ValueError("HISTORY_EVIDENCE_UNAVAILABLE")
        if set(history["stats"]) != {
            "trades",
            "win_rate",
            "avg_quality_score",
            "t60_avg_post_close_return_pct",
            "avg_pct",
        }:
            raise ValueError("HISTORY_STATS_INCOMPLETE")
        analysis_adjustment(history["stats"], mode="hard")
        identity = str(uuid4())
        normalized = {
            "observation_id": identity,
            "account_scope": asdict(self.scope),
            "symbol": target,
            "started_at_ms": started,
            "finished_at_ms": finished,
            "balance": balance,
            "capital_health": capital_health,
            "available_margin": available,
            "used_pool_margin": format(used.normalize(), "f"),
            "rules": rules,
            "account_risk_budget": risk_budget,
            "portfolio_stop_budget": stop_budget,
            **(
                {
                    "entry_controls": entry_controls.snapshot(),
                    "sizing_budget_inputs": sizing_budget,
                }
                if budget_enabled
                else {}
            ),
            "short_ratio": short_ratio,
            "funding_rate": funding,
            "leverage_venue": leverage_venue,
            "market_sources": {
                "contract_environment": self.public.environment,
                "sentiment_environment": self.sentiment.environment,
            },
            "market_observed_at_ms": {
                "long_short_ratio": ratio_at,
                "premium_index": funding_at,
            },
            "response_digests": {
                name: digest(canonical({"response": value}))
                for name, value in {
                    "positions": before,
                    "account": account,
                    "account_config": account_config,
                    "symbol_config": config,
                    "exchange_info": exchange,
                    "long_short_ratio": ratio,
                    "premium_index": premium,
                    "history": history,
                }.items()
            },
        }
        key = StateKey(
            **asdict(self.scope),
            namespace="directional-account-context-v1",
            key=identity,
        )
        result = BusinessState(self.connect).change(
            key,
            expected_version=0,
            request_key=identity,
            payload=normalized,
            reason="DIRECTIONAL_ACCOUNT_CONTEXT",
        )
        if result.code != "APPLIED":
            raise ValueError("ACCOUNT_CONTEXT_PERSIST_FAILED")
        snapshot = self.drawdown.record(
            BalanceObservation(
                identity,
                account["totalWalletBalance"]
                if profile.values["capital.model_enabled"]
                else balance,
                finished,
                digest(canonical(normalized)),
            )
        )
        drawdown = json.loads(snapshot.payload_json)
        deadline = min(
            started + self.max_age + 1,
            ratio_at + self.sentiment_max_age + 1,
            funding_at + self.max_age + 1,
            history["valid_until_ms"],
            history["observed_at_ms"] + self.history_max_age + 1,
            drawdown["observation"]["observed_at_ms"]
            + drawdown["config"]["max_age_ms"]
            + 1,
        )
        if finished >= deadline:
            raise ValueError("ACCOUNT_CONTEXT_STALE")
        if profile.values["capital.enabled"]:
            from v2_core.managed_portfolio import publish_capital_snapshot

            publish_capital_snapshot(
                self.connect,
                self.scope,
                account=account,
                started=started,
                deadline=deadline,
                observation_id=identity,
            )
        from v2_core.drawdown_observation import sizing_factor

        return {
            "account_scope": asdict(self.scope),
            "symbol": target,
            "observed_at_ms": finished,
            "valid_until_ms": deadline,
            "evidence": {
                "state_id": key.identity,
                "version": result.version,
                "drawdown_state_id": self.drawdown.key.identity,
                "drawdown_version": snapshot.version,
                "history": history["evidence"],
                "account_risk_budget": risk_budget,
                "portfolio_stop_budget": stop_budget,
                **(
                    {
                        "entry_controls": entry_controls.snapshot(),
                        "sizing_mark_observed_at_ms": funding_at,
                    }
                    if budget_enabled
                    else {}
                ),
                "sentiment_environment": self.sentiment.environment,
                "sentiment_observed_at_ms": ratio_at,
            },
            "short_ratio": short_ratio,
            "history": history["stats"],
            "sizing": {
                "balance": balance,
                "available_margin": available,
                "used_pool_margin": format(used.normalize(), "f"),
                **rules,
                **(
                    {"remaining_portfolio_risk": stop_budget["remaining_stop_risk"]}
                    if stop_budget
                    else {}
                ),
                **sizing_budget,
                "drawdown_factor": str(
                    sizing_factor(
                        drawdown["state"]["factor"],
                        capital_health,
                        {
                            "capital.drawdown_guard_enabled": (
                                capital_health or {}
                            ).get("drawdown_guard_enabled", True)
                        },
                        self.scope.environment,
                    )
                ),
            },
            "expected_move_pct": expected_move(signal),
            "funding_rate": funding,
            "leverage_venue": leverage_venue,
        }
