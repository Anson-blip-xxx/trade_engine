"""Risk-sized leverage with conservative maintenance-margin and liquidity checks.

This is NOT a liquidation-price guarantee. Tier deductions are ignored, the
worst maintenance ratio is used, and loss + costs + maintenance may consume
only a configured fraction of initial margin. Missing facts never allow 8x.
"""

from decimal import Decimal

from v2_core.ledger import amount

BOOST_CHECKS = frozenset(
    {
        "score",
        "trend",
        "flow",
        "volatility",
        "fresh",
        "funding",
        "normal_risk",
        "liquidity",
        "tv_source",
        "boost_score",
        "tight_stop",
    }
)


def venue_facts(brackets, book, symbol, *, started, finished, max_age_ms):
    rows = brackets if isinstance(brackets, list) else [brackets]
    if (
        len(rows) != 1
        or rows[0].get("symbol") != symbol
        or book.get("symbol") != symbol
    ):
        raise ValueError("LEVERAGE_VENUE_SYMBOL_MISMATCH")
    stamp = book.get("time")
    if (
        type(stamp) is not int
        or not finished - max_age_ms <= stamp <= finished
        or finished < started
    ):
        raise ValueError("LEVERAGE_BOOK_STALE")
    bid, ask, bq, aq = (
        amount(book[k], positive=True)
        for k in ("bidPrice", "askPrice", "bidQty", "askQty")
    )
    if ask < bid:
        raise ValueError("LEVERAGE_BOOK_CROSSED")
    tiers = rows[0].get("brackets")
    if not isinstance(tiers, list) or not tiers:
        raise ValueError("LEVERAGE_BRACKETS_MISSING")
    normalized = []
    for row in tiers:
        ratio = amount(str(row["maintMarginRatio"]), positive=True)
        floor = amount(str(row["notionalFloor"]))
        cap = amount(str(row["notionalCap"]), positive=True)
        lev = row["initialLeverage"]
        if not 0 <= floor < cap or not 0 < ratio < 1 or type(lev) is not int or lev < 1:
            raise ValueError("LEVERAGE_BRACKET_INVALID")
        normalized.append(
            {
                "floor": str(floor),
                "cap": str(cap),
                "max_leverage": lev,
                "mmr": str(ratio),
            }
        )
    return {
        "symbol": symbol,
        "observed_at_ms": started,
        "book_at_ms": stamp,
        "spread_fraction": str((ask - bid) / bid),
        "top_notional": str(min(bid * bq, ask * aq)),
        "tiers": normalized,
    }


def safe_margin(leverage, stop, notional, facts, settings):
    if not facts:
        return False
    stress = notional * (1 + stop + Decimal(settings["sizing.cost_buffer_fraction"]))
    tiers = facts["tiers"]
    # Require coverage at both current and adverse short-side stressed notional.
    if not all(
        any(
            Decimal(t["floor"]) <= n < Decimal(t["cap"])
            and leverage <= t["max_leverage"]
            for t in tiers
        )
        for n in (notional, stress)
    ):
        return False
    mmr = max(Decimal(t["mmr"]) for t in tiers if Decimal(t["floor"]) <= stress)
    margin_use = (
        stop
        + Decimal(settings["sizing.cost_buffer_fraction"])
        + (stress / notional) * mmr
    )
    return margin_use * leverage <= Decimal(settings["leverage.max_margin_consumption"])


def choose(plan, snapshot, *, source, age_ms, settings, notional):
    market, facts = snapshot["market"], snapshot.get("leverage_venue")
    side = plan.side
    aligned = lambda x, y: (
        Decimal(str(x)) >= Decimal(str(y))
        if side == "LONG"
        else Decimal(str(x)) <= Decimal(str(y))
    )
    flow = market["15m"].get("taker_buy_ratio")
    checks = {
        "score": plan.score >= settings["leverage.high_score"],
        "trend": aligned(snapshot["price"], market["4h"]["ema20"])
        and aligned(market["24h"]["ema20"], market["24h"]["ema60"]),
        "flow": flow is not None
        and aligned(
            flow,
            settings["entry.flow_long"]
            if side == "LONG"
            else settings["entry.flow_short"],
        ),
        "volatility": Decimal(str(market["1h"]["atr_pct"]))
        <= Decimal(settings["leverage.boost_max_atr"]),
        "fresh": age_ms <= settings["leverage.boost_max_age_ms"],
        "funding": abs(Decimal(snapshot["funding_rate"]))
        <= Decimal(settings["leverage.boost_max_funding"]),
        "normal_risk": Decimal(snapshot["sizing"]["drawdown_factor"]) == 1,
        "liquidity": bool(facts)
        and Decimal(facts["spread_fraction"])
        <= Decimal(settings["leverage.max_spread"])
        and Decimal(facts["top_notional"])
        >= notional * Decimal(settings["leverage.depth_multiple"]),
    }
    high = all(checks.values())
    desired = (
        settings["leverage.adaptive_high"]
        if high
        else settings["leverage.medium"]
        if plan.score >= settings["leverage.low_score"]
        else settings["leverage.low"]
    )
    checks["tv_source"] = source == "tv_bridge"
    checks["boost_score"] = plan.score >= settings["leverage.boost_score"]
    checks["tight_stop"] = Decimal(plan.stop_fraction) <= Decimal(
        settings["leverage.boost_max_stop"]
    )
    if settings["leverage.boost_enabled"] and all(checks.values()):
        desired = settings["leverage.boost"]
    if not checks["volatility"] or not checks["normal_risk"]:
        desired = min(desired, settings["leverage.low"])
    # Test all lower allowed tiers; never shorten the strategy stop for leverage.
    candidates = sorted(
        {
            settings["leverage.low"],
            settings["leverage.medium"],
            settings["leverage.adaptive_high"],
            settings["leverage.boost"],
        },
        reverse=True,
    )
    selected = next(
        (
            lev
            for lev in candidates
            if lev <= desired
            and safe_margin(lev, Decimal(plan.stop_fraction), notional, facts, settings)
        ),
        None,
    )
    return selected, {
        "checks": checks,
        "desired": desired,
        "selected": selected,
        "venue": facts,
        "reason": "SAFE_TIER" if selected else "NO_VERIFIED_SAFE_LEVERAGE",
    }
