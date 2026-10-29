"""Anchored rehearsal capital, conservative compounding and portfolio stop risk.

All values are USDT. Profits must be journal-settled as well as visible in the
wallet; deposits cannot be reinvested as profits. Withdrawals reduce capacity.
There is no timed reset of drawdown risk, and no martingale.
"""

import json
from dataclasses import asdict
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.precision import money_context
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
        or number.copy_abs() >= Decimal("1e20")
        or number.as_tuple().exponent < -72
    ):
        raise ValueError("INVALID_SETTLED_CAPITAL_PNL")
    return number


def capital_base(account, settings):
    with money_context():
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


def health(
    account, settings, previous=None, *, now_ms=None, outcomes=(), environment=None
):
    from v2_core.drawdown_observation import (
        observation_only,
        observe,
        restored_previous,
    )

    only_observe = observation_only(settings, environment)
    if only_observe and not all(
        settings[k]
        for k in ("capital.enabled", "capital.model_enabled", "portfolio_risk.enabled")
    ):
        raise ValueError("DRAWDOWN_OBSERVATION_REQUIRES_CAPITAL_GUARDS")
    base = capital_base(account, settings)
    if not settings["capital.model_enabled"]:
        return {
            "base": str(base),
            "peak": str(base),
            "factor": "1",
            "anchor": anchor(settings),
        }
    with money_context():
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
        result = {
            "base": str(base),
            "peak": str(peak),
            "drawdown": str(drawdown),
            "factor": factor,
            "anchor": anchor(settings),
        }
        from v2_core.capital_recovery import transition

        previous = (
            previous
            if previous and previous.get("anchor") == result["anchor"]
            else None
        )
        if only_observe:
            return observe(result, previous)
        return transition(
            result,
            restored_previous(previous),
            settings,
            now_ms=now_ms,
            outcomes=outcomes,
        )


def current_health(connect, scope, account, settings, *, now_ms=None):
    from v2_core.drawdown_observation import control

    enabled, control_version = control(connect, scope)
    settings = {**settings, "capital.drawdown_guard_enabled": enabled}
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
        with money_context():
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
    outcomes = ()
    recovery = (previous or {}).get("recovery", {})
    if recovery.get("mode") == "PROBE":
        with connect() as conn:
            outcomes = tuple(
                row[0]
                for row in conn.execute(
                    """SELECT s.evidence->>'net_pnl'
                FROM v2_settlements s JOIN v2_trade_intents i ON i.intent_id=s.episode_id
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND i.created_at>=to_timestamp(%s/1000.0)
                AND s.currency='USDT' AND s.evidence->>'source'='directional-settlement-v1'
                AND s.revision=(SELECT max(t.revision) FROM v2_settlements t WHERE t.episode_id=s.episode_id)""",
                    (*asdict(scope).values(), recovery["probe_started_at_ms"]),
                ).fetchall()
            )
    result = health(
        account,
        settings,
        previous,
        now_ms=now_ms,
        outcomes=outcomes,
        environment=scope.environment,
    )
    if control_version:
        result["drawdown_control_version"] = control_version
    return result


def portfolio_budget(conn, scope, *, settings, capital, exclude_episode=None):
    """Advisory residual; admission must re-read it under the account lock.

    HELD reservations retain their original stop risk through partial exits.
    LEFT JOIN deliberately exposes missing decision evidence instead of silently
    treating an unknown reservation as zero usage.
    """
    with money_context():
        base, factor = Decimal(capital["base"]), Decimal(capital["factor"])
        if (
            not base.is_finite()
            or base < 0
            or not factor.is_finite()
            or not 0 <= factor <= 1
        ):
            raise ValueError("INVALID_CAPITAL_RISK_BUDGET")
        cost = Decimal(settings["sizing.cost_buffer_fraction"])
        rows = conn.execute(
            """SELECT r.notional::text,
              e.snapshot->'features'->'evaluation'->'market_plan'->>'stop_fraction',
              r.reference->'capital'->>'proposed_stop_risk',
              i.payload->>'quantity',
              e.snapshot->'features'->'evaluation'->'sizing'->>'stop_price',
              e.snapshot->'features'->'evaluation'->'market_plan'->>'side'
            FROM v2_risk_reservations r
            LEFT JOIN v2_trade_intents i ON i.intent_id=r.episode_id
            LEFT JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE r.scope=%s AND r.status='HELD'
              AND (%s::uuid IS NULL OR r.episode_id<>%s::uuid)""",
            (scope.key, exclude_episode, exclude_episode),
        ).fetchall()
        held = Decimal(0)
        for notional, stop, original, quantity, stop_price, side in rows:
            risk = amount(notional, positive=True) * (
                amount(stop, positive=True) + cost
            )
            if original is not None:
                recorded = report_amount(original)
                if recorded <= 0:
                    raise ValueError("CAPITAL_ORIGINAL_STOP_RISK_UNVERIFIED")
                risk = max(risk, recorded)
            if stop_price is not None:
                from v2_core.sizing_budget import fixed_stop_risk

                risk = max(
                    risk,
                    fixed_stop_risk(
                        quantity=quantity,
                        notional=amount(notional, positive=True),
                        stop_price=stop_price,
                        side=side,
                        stop_fraction=stop,
                        cost_fraction=settings["sizing.cost_buffer_fraction"],
                    ),
                )
            held += risk
        limit = base * Decimal(settings["portfolio_risk.max_fraction"]) * factor
        remaining = max(Decimal(0), limit - held).quantize(
            Decimal("1e-18"), rounding=ROUND_FLOOR
        )
        return {
            "held_stop_risk": str(held),
            "portfolio_risk_limit": str(limit),
            "remaining_stop_risk": str(remaining),
        }


def guard_risk(conn, scope, *, episode, notional, leverage, settings, capital):
    if leverage > 5 and not settings["leverage.adaptive_enabled"]:
        raise ValueError("CAPITAL_LEVERAGE_SAFETY_UNVERIFIED")
    if not settings["portfolio_risk.enabled"]:
        return {}
    with money_context():
        base, factor = Decimal(capital["base"]), Decimal(capital["factor"])
        if factor == 0:
            raise ValueError("CAPITAL_DRAWDOWN_HALT")
        plan = conn.execute(
            """SELECT e.snapshot->'features'->'evaluation'->'market_plan',
                e.snapshot->'features'->'evaluation'->'leverage_decision',i.payload->>'symbol',
                e.snapshot->'features'->'evaluation'->'sizing_budget',
                e.snapshot->'features'->'evaluation'->'sizing',i.payload->>'quantity'
            FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE i.intent_id=%s AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)""",
            (episode, *asdict(scope).values()),
        ).fetchone()
        if not plan or not isinstance(plan[0], dict):
            raise ValueError("CAPITAL_STOP_PLAN_UNVERIFIED")
        leverage_proof = plan[1]
        symbol = plan[2]
        sizing_proof, sizing, quantity = plan[3:]
        plan = plan[0]
        if settings["leverage.adaptive_enabled"] or leverage > 5:
            from v2_core.adaptive_leverage import BOOST_CHECKS, safe_margin

            if (
                not settings["leverage.adaptive_enabled"]
                or not isinstance(leverage_proof, dict)
                or leverage_proof.get("selected") != leverage
                or leverage
                not in {
                    settings["leverage.low"],
                    settings["leverage.medium"],
                    settings["leverage.adaptive_high"],
                    settings["leverage.boost"]
                    if settings["leverage.boost_enabled"]
                    else 0,
                }
                or leverage_proof.get("venue", {}).get("symbol") != symbol
                or (
                    leverage > 5
                    and (
                        not settings["leverage.boost_enabled"]
                        or not all(
                            leverage_proof.get("checks", {}).get(name) is True
                            for name in BOOST_CHECKS
                        )
                    )
                )
                or not safe_margin(
                    leverage,
                    amount(plan["stop_fraction"], positive=True),
                    notional,
                    leverage_proof.get("venue"),
                    settings,
                )
            ):
                raise ValueError("CAPITAL_LEVERAGE_SAFETY_UNVERIFIED")
            if leverage >= settings["leverage.adaptive_high"] and (
                Decimal(leverage_proof["venue"]["spread_fraction"])
                > Decimal(settings["leverage.max_spread"])
                or Decimal(leverage_proof["venue"]["top_notional"])
                < notional * Decimal(settings["leverage.depth_multiple"])
            ):
                raise ValueError("CAPITAL_LEVERAGE_LIQUIDITY_UNVERIFIED")
        if settings["entry.force_isolated"] and plan.get("margin_mode") != "ISOLATED":
            raise ValueError("CAPITAL_ISOLATED_MARGIN_REQUIRED")
        stop = amount(plan["stop_fraction"], positive=True)
        cost = Decimal(settings["sizing.cost_buffer_fraction"])
        proposed = notional * (stop + cost)
        if sizing_proof is not None:
            from v2_core.sizing_budget import fixed_stop_risk, price_envelope

            if (
                not isinstance(sizing_proof, dict)
                or sizing_proof.get("version") != "remaining-risk-sizing-v1"
                or not isinstance(sizing, dict)
                or sizing.get("reason") != "SIZED"
                or not symbol.endswith("USDT")
                or amount(sizing["quantity"], positive=True)
                != amount(quantity, positive=True)
            ):
                raise ValueError("CAPITAL_SIZING_BUDGET_UNVERIFIED")
            envelope = price_envelope(
                price=sizing_proof["reference_price"],
                mark_price=sizing_proof["mark_price"],
                drift_fraction=sizing_proof["price_drift_fraction"],
                side=plan["side"],
                stop_price=sizing["stop_price"],
                stop_fraction=plan["stop_fraction"],
                cost_fraction=settings["sizing.cost_buffer_fraction"],
            )
            if not envelope["stop_valid_through_envelope"]:
                raise ValueError("CAPITAL_SIZING_BUDGET_UNVERIFIED")
            # Keep the ORIGINAL buffered risk reserved, even when a fresh mark
            # temporarily improves entry. A later guard never weakens the lock.
            proposed = max(
                proposed,
                amount(quantity, positive=True) * Decimal(envelope["unit_stop_risk"]),
                fixed_stop_risk(
                    quantity=quantity,
                    notional=notional,
                    stop_price=sizing["stop_price"],
                    side=plan["side"],
                    stop_fraction=plan["stop_fraction"],
                    cost_fraction=settings["sizing.cost_buffer_fraction"],
                ),
            )
        if (
            proposed > base * Decimal(settings["sizing.risk_fraction"]) * factor
            or notional / leverage
            > base * Decimal(settings["sizing.max_margin_fraction"]) * factor
        ):
            raise ValueError("CAPITAL_SINGLE_TRADE_RISK_EXCEEDED")
        budget = portfolio_budget(
            conn, scope, settings=settings, capital=capital, exclude_episode=episode
        )
        if proposed > Decimal(budget["remaining_stop_risk"]):
            raise ValueError("CAPITAL_PORTFOLIO_RISK_EXCEEDED")
        return {
            **budget,
            "proposed_stop_risk": str(proposed),
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
