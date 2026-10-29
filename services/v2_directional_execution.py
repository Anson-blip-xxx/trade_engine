"""Wire the actual-account first-entry gate into an existing Testnet runtime.

Changes bounded per-symbol Testnet leverage/margin settings only on a flat,
order-free account and only behind the entry write gate. It does not enable a
transport or start a trading loop. Caller still supplies mark/protection data.
"""

from dataclasses import asdict
from decimal import Decimal

from v2_core.errors import SubmissionNotSent
from v2_core.venue_readiness import GuardedOpeningSubmit, TestnetVenueReadiness
from v2_core.venue_settings import TestnetSymbolSettings


def bind_directional_venue_gate(
    runtime,
    request,
    *,
    mark_reference,
    enabled=False,
    reduce_only_enabled=False,
    excluded_position_symbols=(),
):
    if (
        runtime.scope is None
        or runtime.execution.scope != runtime.scope
        or not callable(runtime.execution.risk_reference)
    ):
        raise ValueError("matching bound runtime required")
    if isinstance(runtime.execution.submit, GuardedOpeningSubmit):
        raise ValueError("venue gate already bound")  # noqa: TRY004 - composition state
    scope = runtime.scope
    from v2_core.managed_portfolio import ManagedPortfolio

    portfolio = ManagedPortfolio(
        runtime.data._connect,
        request,
        scope=scope,
        clock_ms=runtime.clock_ms,
        exclusions=excluded_position_symbols,
    )

    def plan(order):
        with runtime.data._connect() as conn:
            config, snapshot, version = conn.execute(
                """SELECT e.config,e.snapshot,i.strategy_version FROM v2_orders o
                JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                JOIN v2_decision_evidence e USING(evidence_ref) WHERE o.order_id=%s""",
                (order["order_id"],),
            ).fetchone()
        if (
            version != "directional-admission-v2-1"
            or config.get("mode") != "TESTNET_ADMISSION_ONLY"
            or config.get("account_scope") != asdict(scope)
            or config.get("strategy") not in {"S6", "S8"}
            or snapshot["result"]["action"] != "OPEN"
        ):
            raise ValueError("directional execution evidence required")
        desired = snapshot["features"]["evaluation"]["market_plan"]
        from v2_core.runtime_policy import PolicyStore, resolve

        current_settings = PolicyStore(runtime.data._connect, scope).read().values
        frozen_settings = resolve(config.get("policy"))
        if (
            current_settings["entry.reentry_enabled"]
            or frozen_settings["entry.reentry_enabled"]
        ):
            from services.v2_reentry_gate import prior_close, structure_policy, validate

            try:
                proof = snapshot["features"]["evaluation"]["reentry"]
                now = runtime.clock_ms()
                if validate(
                    proof,
                    scope=asdict(scope),
                    target=order["symbol"],
                    side=order["side"],
                    observed=snapshot["observed_at"],
                    now=now,
                ):
                    raise ValueError("reentry not qualified")
                if proof["policy"] != asdict(structure_policy(current_settings)):
                    raise ValueError("reentry policy changed")
                if (
                    now - proof["observed_at_ms"]
                    >= current_settings["entry.reentry_max_age_ms"]
                ):
                    raise ValueError("reentry freshness policy tightened")
                if (
                    now - proof["market_cutoff_ms"]
                    >= current_settings["entry.reentry_max_bar_age_ms"]
                ):
                    raise ValueError("reentry market freshness policy tightened")
                with runtime.data._connect() as conn:
                    conn.execute("SET LOCAL statement_timeout='2s'")
                    previous, missing = prior_close(conn, scope, order["symbol"], now)
                if (
                    missing not in {None, "NO_PREVIOUS_FILLED_EPISODE"}
                    or (previous["previous_episode"] if previous else None)
                    != proof["previous_episode"]
                    or previous != proof.get("previous_exit")
                ):
                    raise ValueError("previous episode changed")
            except (ValueError, TypeError, KeyError):
                raise SubmissionNotSent("REENTRY_SUBMISSION_BLOCKED") from None
        if snapshot.get("source") == "quant":
            from services.v2_quant_research import QuantPolicy
            from v2_core.runtime_policy import PolicyStore

            current = QuantPolicy.read(runtime.data._connect, scope.environment)
            plugin = snapshot["features"]["evaluation"]["source_scoring"]["strategy_id"]
            if (
                current.mode != "CANDIDATE"
                or plugin not in current.plugins
                or snapshot["features"]["context"]["quant"]["frame"][
                    "timeframe_minutes"
                ]
                not in current.timeframes
                or not PolicyStore(runtime.data._connect, scope)
                .read()
                .values["quant.admission_enabled"]
            ):
                raise SubmissionNotSent("QUANT_EXECUTION_DISABLED")
        return {"leverage": desired["leverage"], "margin_type": desired["margin_mode"]}

    def validate_reference(order, reference):
        from v2_core.runtime_policy import PolicyStore, resolve

        with runtime.data._connect() as conn:
            config, snapshot = conn.execute(
                "SELECT e.config,e.snapshot FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id JOIN v2_decision_evidence e USING(evidence_ref) WHERE o.order_id=%s",
                (order["order_id"],),
            ).fetchone()
        if snapshot.get("source") != "quant":
            from v2_core.entry_price import validate_entry_price

            current = PolicyStore(runtime.data._connect, scope).read().values
            frozen = resolve(config.get("policy"))
            if not (
                current["entry.price_guard_enabled"]
                or frozen["entry.price_guard_enabled"]
            ):
                return None
            try:
                return validate_entry_price(
                    side=order["side"],
                    original=snapshot["features"]["context"]["directional"]["price"],
                    mark=reference["mark_price"],
                    stop=snapshot["features"]["evaluation"]["sizing"]["stop_price"],
                    frozen=frozen,
                    current=current,
                )
            except (KeyError, TypeError):
                raise SubmissionNotSent(
                    "DIRECTIONAL_SUBMISSION_PRICE_INVALID"
                ) from None
        frame = snapshot["features"]["context"]["quant"]["frame"]
        selected = PolicyStore(runtime.data._connect, scope).read()
        limit = min(
            Decimal(selected.values["quant.max_chase_atr"]),
            Decimal(resolve(config.get("policy"))["quant.max_chase_atr"]),
        )
        mark = Decimal(reference["mark_price"])
        original, atr = (
            Decimal(frame["current"]["close"]),
            Decimal(frame["current"]["atr"]),
        )
        stop = Decimal(snapshot["features"]["evaluation"]["sizing"]["stop_price"])
        distance = mark - stop if order["side"] == "BUY" else stop - mark
        if (
            mark <= 0
            or atr <= 0
            or abs(mark - original) > limit * atr
            or not 0
            < distance / mark
            <= Decimal(frame["parameters"]["max_stop_fraction"])
        ):
            raise SubmissionNotSent("QUANT_SUBMISSION_PRICE_MOVED")
        return {
            "source": "quant-pre-submit-price-v1",
            "reference_price": str(original),
            "mark_price": str(mark),
            "atr": str(atr),
            "max_chase_atr": str(limit),
            "policy_version": selected.version,
        }

    gate = GuardedOpeningSubmit(
        runtime.data._connect,
        readiness=TestnetVenueReadiness(
            request,
            scope=scope,
            clock_ms=runtime.clock_ms,
            excluded_position_symbols=excluded_position_symbols,
            portfolio=portfolio,
        ),
        reference=mark_reference,
        plan=plan,
        submit=runtime.execution.submit,
        configure=TestnetSymbolSettings(
            runtime.data._connect,
            request,
            scope=scope,
            clock_ms=runtime.clock_ms,
            allow_writes=enabled,
            excluded_position_symbols=excluded_position_symbols,
            portfolio=portfolio,
        ).ensure,
        enabled=enabled,
        reduce_only_enabled=reduce_only_enabled,
        validate_reference=validate_reference,
    )
    runtime.execution.submit = gate
    return gate
