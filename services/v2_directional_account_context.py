"""Read-only Binance and PG evidence for directional strategy sizing.

No values are defaulted. Collection persists a normalized snapshot before it is
returned; it never submits, cancels, protects or closes an exchange order.
"""

import json
from dataclasses import asdict
from uuid import uuid4

from v2_core.account_risk import AccountPolicy, AccountScope
from v2_core.directional import analysis_adjustment, number
from v2_core.drawdown import BalanceObservation, DrawdownState
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds, symbol
from v2_core.ledger import amount
from v2_core.state import BusinessState, StateKey


def expected_move(signal):
    kind, features = signal["signal"], signal["features"]
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
    if policy.currency != currency:
        raise ValueError("ACCOUNT_RISK_CURRENCY_MISMATCH")
    held = amount(format(held_notional, "f"))
    remaining = amount(policy.max_notional, positive=True) - held
    if held_positions >= policy.max_positions or remaining <= 0:
        raise ValueError("ACCOUNT_RISK_CAPACITY_UNAVAILABLE")
    evidence = {
        "policy_version": version,
        "policy": config,
        "held_notional": format(held.normalize(), "f"),
        "held_positions": held_positions,
        "available_notional": format(remaining.normalize(), "f"),
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

    def __call__(self, signal):
        target = symbol(signal["symbol"])
        started = milliseconds(self.clock())
        before = self.request("GET", "/fapi/v3/positionRisk", {})
        account = self.request("GET", "/fapi/v3/account", {})
        account_config = self.request("GET", "/fapi/v1/accountConfig", {})
        config = self.request("GET", "/fapi/v1/symbolConfig", {"symbol": target})
        exchange = self.public("/fapi/v1/exchangeInfo", {})
        ratio = self.sentiment(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": target, "period": "1h", "limit": 3},
        )
        premium = self.public("/fapi/v1/premiumIndex", {"symbol": target})
        after = self.request("GET", "/fapi/v3/positionRisk", {})
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
            amount(account["totalWalletBalance"], positive=True).normalize(), "f"
        )
        available = format(
            amount(account["availableBalance"], positive=True).normalize(), "f"
        )
        used = amount(account["totalInitialMargin"]) + amount(
            account["totalOpenOrderInitialMargin"]
        )
        if used < 0:
            raise ValueError("INVALID_USED_MARGIN")
        rules = _rules(exchange, config, target)
        rules["max_notional"] = format(
            min(
                amount(rules["max_notional"], positive=True),
                amount(risk_budget["available_notional"], positive=True),
            ).normalize(),
            "f",
        )
        if not isinstance(ratio, list) or not ratio or not isinstance(ratio[-1], dict):
            raise ValueError("SHORT_RATIO_MISSING")
        if ratio[-1].get("symbol") != target:
            raise ValueError("SHORT_RATIO_SCOPE")
        short_ratio = format(
            number(ratio[-1]["shortAccount"], minimum=0, maximum=1).normalize(), "f"
        )
        if type(ratio[-1].get("timestamp")) is not int:
            raise ValueError("SHORT_RATIO_TIMESTAMP")
        ratio_at = milliseconds(ratio[-1]["timestamp"])
        if not 0 <= finished - ratio_at <= self.sentiment_max_age:
            raise ValueError("SHORT_RATIO_STALE")
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
            "available_margin": available,
            "used_pool_margin": format(used.normalize(), "f"),
            "rules": rules,
            "account_risk_budget": risk_budget,
            "short_ratio": short_ratio,
            "funding_rate": funding,
            "market_sources": {
                "contract_environment": self.public.environment,
                "sentiment_environment": self.sentiment.environment,
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
                identity, balance, finished, digest(canonical(normalized))
            )
        )
        drawdown = json.loads(snapshot.payload_json)
        deadline = min(
            started + self.max_age + 1,
            history["valid_until_ms"],
            history["observed_at_ms"] + self.history_max_age + 1,
            drawdown["observation"]["observed_at_ms"]
            + drawdown["config"]["max_age_ms"]
            + 1,
        )
        if finished >= deadline:
            raise ValueError("ACCOUNT_CONTEXT_STALE")
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
                "sentiment_environment": self.sentiment.environment,
            },
            "short_ratio": short_ratio,
            "history": history["stats"],
            "sizing": {
                "balance": balance,
                "available_margin": available,
                "used_pool_margin": format(used.normalize(), "f"),
                **rules,
                "drawdown_factor": drawdown["state"]["factor"],
            },
            "expected_move_pct": expected_move(signal),
            "funding_rate": funding,
        }
