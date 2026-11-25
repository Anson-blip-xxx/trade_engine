"""Anchored rehearsal capital, conservative compounding and portfolio stop risk.

All values are USDT. Profits must be journal-settled as well as visible in the
wallet; deposits cannot be reinvested as profits. Withdrawals reduce capacity.
There is no timed reset of drawdown risk, and no martingale.
"""

import json
from dataclasses import asdict
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from fractions import Fraction

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


def verified_settled_outcomes(connect, scope, since_ms):
    """Latest verified old/new settlements, with exact new-plan provenance.

    Batch-load compact immutable bindings, not historical market feature blobs.
    Unknown settlement sources never become reinvestable profits. A forged or
    corrupt recognized source fails closed instead of silently changing capital.
    """
    from v2_core.strategy_capital import STRATEGY_CAPITAL_RECORD_SQL
    from v2_core.strategy_lifecycle import validate_settlement_provenance
    from v2_core.strategy_plan_store import read_trade_plan_record

    with connect() as conn:
        rows = conn.execute(
            f"""SELECT s.evidence,{STRATEGY_CAPITAL_RECORD_SQL}
            FROM v2_settlements s JOIN v2_trade_intents i ON i.intent_id=s.episode_id
            JOIN v2_episodes ep ON ep.episode_id=i.intent_id
            JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
            AND i.created_at>=to_timestamp(%s/1000.0)
            AND s.currency='USDT'
            AND ep.status='SETTLED' AND s.revision=ep.accounting_revision
            AND s.evidence->>'source' IN ('directional-settlement-v1','strategy-settlement-v1')
            AND s.revision=(SELECT max(t.revision) FROM v2_settlements t WHERE t.episode_id=s.episode_id)""",
            (*asdict(scope).values(), since_ms),
        ).fetchall()
    outcomes = []
    for evidence, record in rows:
        plan = read_trade_plan_record(record) if record is not None else None
        validate_settlement_provenance(plan, evidence)
        if plan is not None and plan.scope != scope:
            raise ValueError("STRATEGY_SETTLEMENT_PROVENANCE_MISMATCH")
        value = evidence.get("net_pnl")
        report_amount(value)
        outcomes.append(value)
    return tuple(outcomes)


def current_health(connect, scope, account, settings, *, now_ms=None):
    from v2_core.drawdown_observation import control

    enabled, control_version = control(connect, scope)
    settings = {**settings, "capital.drawdown_guard_enabled": enabled}
    if settings["capital.model_enabled"]:
        rows = verified_settled_outcomes(
            connect, scope, settings["capital.started_at_ms"]
        )
        with money_context():
            account = {
                **account,
                "verified_net_pnl": str(
                    sum((report_amount(row) for row in rows), Decimal(0)).quantize(
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
        outcomes = verified_settled_outcomes(
            connect, scope, recovery["probe_started_at_ms"]
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


def _decision_cost(settings, scoring, entry_mode):
    """Retain an immutable trend decision's cost floor across policy changes.

    The plan mode also identifies the profile, so deleting or corrupting its
    scoring proof cannot silently turn a structural decision into a legacy one.
    Legacy/quant decisions keep their existing runtime-cost semantics.
    """
    current = Decimal(settings["sizing.cost_buffer_fraction"])
    profile = "causal-trend-entry-v1"
    structural = entry_mode in {"TREND_BREAKOUT", "TREND_PULLBACK_RESUME"} or (
        isinstance(scoring, dict)
        and (scoring.get("profile") == profile or scoring.get("version") == profile)
    )
    if not structural:
        return current
    try:
        if (
            not isinstance(scoring, dict)
            or scoring.get("profile") != profile
            or scoring.get("version") != profile
        ):
            raise ValueError
        frozen = amount(scoring["cost_buffer_fraction"], positive=True)
        # The effective cost may exceed the trend floor, up to the existing
        # sizing-policy maximum. Never trust an unbounded stored numeric fact.
        if not Decimal("0.0001") <= frozen <= Decimal("0.1"):
            raise ValueError
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise ValueError("CAPITAL_TREND_COST_UNVERIFIED") from None
    return max(current, frozen)


def portfolio_budget(conn, scope, *, settings, capital, exclude_episode=None):
    """Advisory residual; admission must re-read it under the account lock.

    HELD reservations retain their original stop risk through partial exits.
    LEFT JOIN deliberately exposes missing decision evidence instead of silently
    treating an unknown reservation as zero usage.
    """
    from v2_core.strategy_capital import (
        STRATEGY_CAPITAL_RECORD_SQL,
        read_strategy_capital,
    )

    with money_context():
        base, factor = Decimal(capital["base"]), Decimal(capital["factor"])
        if (
            not base.is_finite()
            or base < 0
            or not factor.is_finite()
            or not 0 <= factor <= 1
        ):
            raise ValueError("INVALID_CAPITAL_RISK_BUDGET")
        rows = conn.execute(
            f"""SELECT r.notional::text,
              e.snapshot->'features'->'evaluation'->'market_plan'->>'stop_fraction',
              r.reference->'capital'->>'proposed_stop_risk',
              i.payload->>'quantity',
              e.snapshot->'features'->'evaluation'->'sizing'->>'stop_price',
              e.snapshot->'features'->'evaluation'->'market_plan'->>'side',
              e.snapshot->'features'->'evaluation'->'source_scoring',
              e.snapshot->'features'->'evaluation'->'market_plan'->>'entry_mode',
              ({STRATEGY_CAPITAL_RECORD_SQL}) ||
                jsonb_build_object('reservation_capital',r.reference->'capital')
            FROM v2_risk_reservations r
            LEFT JOIN v2_trade_intents i ON i.intent_id=r.episode_id
            LEFT JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE r.scope=%s AND r.status='HELD'
              AND (%s::uuid IS NULL OR r.episode_id<>%s::uuid)""",
            (scope.key, exclude_episode, exclude_episode),
        ).fetchall()
        held = Decimal(0)
        for (
            notional,
            stop,
            original,
            quantity,
            stop_price,
            side,
            scoring,
            mode,
            record,
        ) in rows:
            strategy = read_strategy_capital(record, scope=scope)
            if strategy is not None:
                # Revaluing at today's lower cost cannot release yesterday's
                # larger committed budget. Typed HELD records must retain both
                # reservation floors bound to this exact immutable plan.
                try:
                    proof = record.get("reservation_capital")
                    if (
                        type(proof) is not dict
                        or proof.get("strategy_plan_digest")
                        != strategy.plan.plan_digest
                    ):
                        raise ValueError
                    frozen_risk = report_amount(proof["proposed_stop_risk"])
                    amount(proof["reserved_margin"], positive=True)
                    if frozen_risk <= 0 or frozen_risk != report_amount(original):
                        raise ValueError
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    raise ValueError(
                        "STRATEGY_CAPITAL_RESERVATION_UNVERIFIED"
                    ) from None
                risk = strategy.stop_risk(
                    notional,
                    current_cost_fraction=settings["sizing.cost_buffer_fraction"],
                )
            else:
                cost = _decision_cost(settings, scoring, mode)
                risk = amount(notional, positive=True) * (
                    amount(stop, positive=True) + cost
                )
            if original is not None:
                recorded = report_amount(original)
                if recorded <= 0:
                    raise ValueError("CAPITAL_ORIGINAL_STOP_RISK_UNVERIFIED")
                risk = max(risk, recorded)
            if strategy is None and stop_price is not None:
                from v2_core.sizing_budget import fixed_stop_risk

                risk = max(
                    risk,
                    fixed_stop_risk(
                        quantity=quantity,
                        notional=amount(notional, positive=True),
                        stop_price=stop_price,
                        side=side,
                        stop_fraction=stop,
                        cost_fraction=str(cost),
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
    from v2_core.strategy_capital import (
        STRATEGY_CAPITAL_RECORD_SQL,
        read_strategy_capital,
    )

    if leverage > 5 and not settings["leverage.adaptive_enabled"]:
        raise ValueError("CAPITAL_LEVERAGE_SAFETY_UNVERIFIED")
    with money_context():
        base, factor = Decimal(capital["base"]), Decimal(capital["factor"])
        plan = conn.execute(
            f"""SELECT e.snapshot->'features'->'evaluation'->'market_plan',
                e.snapshot->'features'->'evaluation'->'leverage_decision',i.payload->>'symbol',
                e.snapshot->'features'->'evaluation'->'sizing_budget',
                e.snapshot->'features'->'evaluation'->'sizing',i.payload->>'quantity',
                e.snapshot->'features'->'evaluation'->'source_scoring',
                {STRATEGY_CAPITAL_RECORD_SQL},
                (SELECT r.reference->'capital'->>'proposed_stop_risk'
                 FROM v2_risk_reservations r WHERE r.episode_id=i.intent_id AND r.status='HELD')
            FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE i.intent_id=%s AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)""",
            (episode, *asdict(scope).values()),
        ).fetchone()
        if plan:
            strategy = read_strategy_capital(plan[7], scope=scope)
            if strategy is not None:
                return _guard_strategy_risk(
                    conn,
                    scope,
                    episode=episode,
                    strategy=strategy,
                    original=plan[8],
                    notional=notional,
                    leverage=leverage,
                    settings=settings,
                    capital=capital,
                )
        if not settings["portfolio_risk.enabled"]:
            return {}
        if factor == 0:
            raise ValueError("CAPITAL_DRAWDOWN_HALT")
        if not plan or not isinstance(plan[0], dict):
            raise ValueError("CAPITAL_STOP_PLAN_UNVERIFIED")
        leverage_proof = plan[1]
        symbol = plan[2]
        sizing_proof, sizing, quantity, scoring = plan[3:7]
        plan = plan[0]
        cost = _decision_cost(settings, scoring, plan.get("entry_mode"))
        effective_settings = {**settings, "sizing.cost_buffer_fraction": str(cost)}
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
                    effective_settings,
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
                cost_fraction=str(cost),
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
                    cost_fraction=str(cost),
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


def _guard_strategy_risk(
    conn, scope, *, episode, strategy, original, notional, leverage, settings, capital
):
    """Typed plan admission; never synthesize legacy strategy/scoring fields."""
    from v2_core.adaptive_leverage import BOOST_CHECKS, safe_margin

    if not settings["capital.enabled"] or not settings["portfolio_risk.enabled"]:
        raise ValueError("STRATEGY_CAPITAL_RISK_POLICY_REQUIRED")
    if Decimal(settings["capital.pool_fraction"]) > Decimal(".70"):
        raise ValueError("CAPITAL_RESERVE_FLOOR_REQUIRED")
    plan = strategy.plan
    if (
        type(leverage) is not int
        or leverage != plan.leverage
        or leverage not in {1, 2, 3, 4, 5, 8}
    ):
        raise ValueError("STRATEGY_CAPITAL_LEVERAGE_MISMATCH")
    # Q and reference price are individually persisted facts; their product
    # may legitimately have up to 36 decimal places. Do not round either fact
    # or reject the exact derived value at the NUMERIC(38,18) storage boundary.
    notional = report_amount(str(notional))
    if notional <= 0:
        raise ValueError("STRATEGY_CAPITAL_RISK_INPUT_INVALID")
    price = Fraction(notional) / Fraction(plan.quantity)
    entry, stop = Fraction(plan.entry_price), Fraction(plan.stop_price)
    drift = min(
        Fraction(strategy.price_drift_fraction),
        Fraction(settings["entry.max_adverse_price_fraction"]),
    )
    if (plan.side == "BUY" and price <= stop) or (
        plan.side == "SELL" and price >= stop
    ):
        raise ValueError("STRATEGY_CAPITAL_STOP_CROSSED")
    if (plan.side == "BUY" and price > entry * (1 + drift)) or (
        plan.side == "SELL" and price < entry * (1 - drift)
    ):
        raise ValueError("STRATEGY_CAPITAL_ENTRY_PRICE_DRIFT")
    if settings["entry.force_isolated"] and plan.margin_mode != "ISOLATED":
        raise ValueError("CAPITAL_ISOLATED_MARGIN_REQUIRED")
    cost = max(strategy.cost_fraction, Decimal(settings["sizing.cost_buffer_fraction"]))
    proposed = strategy.stop_risk(notional, current_cost_fraction=str(cost))
    if original is not None:
        recorded = report_amount(original)
        if recorded <= 0:
            raise ValueError("CAPITAL_ORIGINAL_STOP_RISK_UNVERIFIED")
        proposed = max(proposed, recorded)
    # The fixed stop must remain safe at the admissible current entry, not just
    # at the initial price. Include the retained envelope and declared loss.
    margin_stop = max(
        strategy.stop_fraction,
        (proposed / notional - cost).quantize(Decimal("1e-18"), rounding=ROUND_CEILING),
    )
    proof = strategy.leverage_proof
    if settings["leverage.adaptive_enabled"] or leverage > 5:
        if (
            not settings["leverage.adaptive_enabled"]
            or not isinstance(proof, dict)
            or proof.get("selected") != leverage
            or leverage
            not in {
                settings["leverage.low"],
                settings["leverage.medium"],
                settings["leverage.adaptive_high"],
                settings["leverage.boost"] if settings["leverage.boost_enabled"] else 0,
            }
            or not isinstance(proof.get("venue"), dict)
            or proof["venue"].get("symbol") != plan.symbol
            or (
                leverage > 5
                and (
                    not settings["leverage.boost_enabled"]
                    or not all(
                        proof.get("checks", {}).get(name) is True
                        for name in BOOST_CHECKS
                    )
                )
            )
            or not safe_margin(
                leverage,
                margin_stop,
                notional,
                proof["venue"],
                {**settings, "sizing.cost_buffer_fraction": str(cost)},
            )
        ):
            raise ValueError("CAPITAL_LEVERAGE_SAFETY_UNVERIFIED")
        if leverage >= settings["leverage.adaptive_high"] and (
            Decimal(proof["venue"]["spread_fraction"])
            > Decimal(settings["leverage.max_spread"])
            or Decimal(proof["venue"]["top_notional"])
            < notional * Decimal(settings["leverage.depth_multiple"])
        ):
            raise ValueError("CAPITAL_LEVERAGE_LIQUIDITY_UNVERIFIED")
    base, factor = Decimal(capital["base"]), Decimal(capital["factor"])
    if (
        not base.is_finite()
        or base < 0
        or not factor.is_finite()
        or not 0 <= factor <= 1
    ):
        raise ValueError("INVALID_CAPITAL_RISK_BUDGET")
    if factor == 0:
        raise ValueError("CAPITAL_DRAWDOWN_HALT")
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
        "strategy_plan_digest": plan.plan_digest,
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
