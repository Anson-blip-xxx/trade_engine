"""Anchored rehearsal capital, conservative compounding and portfolio stop risk.

All values are USDT. Profits must be journal-settled as well as visible in the
wallet; deposits cannot be reinvested as profits. Withdrawals reduce capacity.
There is no timed reset of drawdown risk, and no martingale.
"""

import json
from dataclasses import asdict
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext

from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.state import BusinessState, StateKey


def report_amount(value):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("INVALID_SETTLED_CAPITAL_PNL")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("INVALID_SETTLED_CAPITAL_PNL") from None
    if (
        not number.is_finite()
        or abs(number) >= Decimal("1e20")
        or number.as_tuple().exponent < -72
    ):
        raise ValueError("INVALID_SETTLED_CAPITAL_PNL")
    return number


def capital_base(account, settings):
    with localcontext() as ctx:
        ctx.prec = 100
        wallet, equity, available, used = (
            amount(account[name])
            for name in (
                "totalWalletBalance",
                "totalMarginBalance",
                "availableBalance",
                "totalInitialMargin",
            )
        )
        if min(wallet, equity, available, used) < 0:
            raise ValueError("INVALID_CAPITAL_ACCOUNT_VALUES")
        base = min(wallet, equity, available + used)
        if settings["capital.model_enabled"]:
            net = min(
                wallet - Decimal(settings["capital.reference_wallet"]),
                amount(account.get("verified_net_pnl", "0")),
            )
            modeled = (
                Decimal(settings["capital.initial_equity"])
                + min(net, Decimal(0))
                + max(net, Decimal(0))
                * Decimal(settings["capital.profit_reinvest_fraction"])
                + min(equity - wallet, Decimal(0))
            )
            base = min(base, max(Decimal(0), modeled))
        return base.quantize(Decimal("1e-18"), rounding=ROUND_FLOOR)


def anchor(settings):
    return digest(
        canonical(
            {
                key: settings[key]
                for key in (
                    "capital.model_enabled",
                    "capital.initial_equity",
                    "capital.reference_wallet",
                    "capital.started_at_ms",
                    "capital.profit_reinvest_fraction",
                )
            }
        )
    )


def health(account, settings, previous=None):
    base = capital_base(account, settings)
    if not settings["capital.model_enabled"]:
        return {
            "base": str(base),
            "peak": str(base),
            "factor": "1",
            "anchor": anchor(settings),
        }
    with localcontext() as ctx:
        ctx.prec = 100
        peak = max(base, Decimal(settings["capital.initial_equity"]))
        if previous and previous.get("anchor") == anchor(settings):
            peak = max(peak, Decimal(previous["peak"]))
        drawdown = (peak - base) / peak
        factor = (
            "0"
            if drawdown >= Decimal(settings["capital.halt_drawdown"])
            else settings["capital.defensive_factor"]
            if drawdown >= Decimal(settings["capital.defensive_drawdown"])
            else settings["capital.reduced_factor"]
            if drawdown >= Decimal(settings["capital.reduce_drawdown"])
            else "1"
        )
        return {
            "base": str(base),
            "peak": str(peak),
            "drawdown": str(drawdown),
            "factor": factor,
            "anchor": anchor(settings),
        }


def current_health(connect, scope, account, settings):
    if settings["capital.model_enabled"]:
        with connect() as conn:
            rows = conn.execute(
                """SELECT s.evidence->>'net_pnl'
                FROM v2_settlements s JOIN v2_trade_intents i ON i.intent_id=s.episode_id
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND i.created_at>=to_timestamp(%s/1000.0)
                AND s.currency='USDT' AND s.evidence->>'source'='directional-settlement-v1'
                AND s.revision=(SELECT max(t.revision) FROM v2_settlements t WHERE t.episode_id=s.episode_id)""",
                (*asdict(scope).values(), settings["capital.started_at_ms"]),
            ).fetchall()
        with localcontext() as ctx:
            ctx.prec = 100
            account = {
                **account,
                "verified_net_pnl": str(
                    sum((report_amount(row[0]) for row in rows), Decimal(0)).quantize(
                        Decimal("1e-18"), rounding=ROUND_FLOOR
                    )
                ),
            }
    saved = BusinessState(connect).read(
        StateKey(**asdict(scope), namespace="capital-snapshot-v1", key="latest")
    )
    previous = (
        json.loads(saved.payload_json).get("capital_health")
        if saved and not saved.deleted
        else None
    )
    return health(account, settings, previous)


def guard_risk(conn, scope, *, episode, notional, leverage, settings, capital):
    if not settings["portfolio_risk.enabled"]:
        return {}
    with localcontext() as ctx:
        ctx.prec = 100
        base, factor = Decimal(capital["base"]), Decimal(capital["factor"])
        if factor == 0:
            raise ValueError("CAPITAL_DRAWDOWN_HALT")
        plan = conn.execute(
            """SELECT e.snapshot->'features'->'evaluation'->'market_plan'
            FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE i.intent_id=%s AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)""",
            (episode, *asdict(scope).values()),
        ).fetchone()
        if not plan or not isinstance(plan[0], dict):
            raise ValueError("CAPITAL_STOP_PLAN_UNVERIFIED")
        plan = plan[0]
        if settings["entry.force_isolated"] and plan.get("margin_mode") != "ISOLATED":
            raise ValueError("CAPITAL_ISOLATED_MARGIN_REQUIRED")
        stop = amount(plan["stop_fraction"], positive=True)
        cost = Decimal(settings["sizing.cost_buffer_fraction"])
        proposed = notional * (stop + cost)
        if (
            proposed > base * Decimal(settings["sizing.risk_fraction"]) * factor
            or notional / leverage
            > base * Decimal(settings["sizing.max_margin_fraction"]) * factor
        ):
            raise ValueError("CAPITAL_SINGLE_TRADE_RISK_EXCEEDED")
        rows = conn.execute(
            """SELECT r.notional::text,e.snapshot->'features'->'evaluation'->'market_plan'->>'stop_fraction'
            FROM v2_risk_reservations r JOIN v2_trade_intents i ON i.intent_id=r.episode_id
            JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE r.scope=%s AND r.status='HELD' AND r.episode_id<>%s""",
            (scope.key, episode),
        ).fetchall()
        # Original stop risk remains reserved until verified settlement. Partial
        # exits and tighter stops never manufacture extra entry capacity.
        held = sum(
            (
                amount(n, positive=True) * (amount(s, positive=True) + cost)
                for n, s in rows
            ),
            Decimal(0),
        )
        limit = base * Decimal(settings["portfolio_risk.max_fraction"]) * factor
        if held + proposed > limit:
            raise ValueError("CAPITAL_PORTFOLIO_RISK_EXCEEDED")
        return {
            "held_stop_risk": str(held),
            "proposed_stop_risk": str(proposed),
            "portfolio_risk_limit": str(limit),
            "capital_health": capital,
        }


def rehearsal_profile(reference_wallet, *, started_at_ms=0):
    """Explicit operator activation; read-only callers never install defaults."""
    amount(reference_wallet, positive=True)
    return {
        "capital.enabled": True,
        "capital.model_enabled": True,
        "capital.initial_equity": "1000",
        "capital.reference_wallet": reference_wallet,
        "capital.started_at_ms": started_at_ms,
        "capital.profit_reinvest_fraction": "0.50",
        "capital.pool_fraction": "0.70",
        "sizing.pool_fraction": "0.70",
        "capital.margin_buffer": "1.10",
        "portfolio_risk.enabled": True,
        "portfolio_risk.max_fraction": "0.03",
        "sizing.risk_fraction": "0.005",
        "sizing.max_margin_fraction": "0.05",
        "sizing.cost_buffer_fraction": "0.002",
        "sizing.min_allocation": "0.03",
        "sizing.max_allocation": "0.15",
        "entry.force_isolated": True,
        "leverage.low": 2,
        "leverage.medium": 2,
        "leverage.volatile": 1,
        "leverage.atr_threshold": "4",
        "leverage.pulse": 3,
        "leverage.trend": 3,
        "leverage.pump": 2,
        "leverage.takeover": 2,
    }
