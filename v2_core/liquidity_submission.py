"""Last-mile opening fence; no public-network calls and no exit-path hooks."""

from v2_core.liquidity import evaluate_execution_quote, evaluate_liquidity
from v2_core.liquidity_controls import from_snapshot, validate_submission


def validate_opening_liquidity(conn, scope, order, *, now_ms):
    typed = validate_submission(conn, scope, order["order_id"])
    if typed is not None:
        plan, context = typed
        if order["symbol"] != plan.symbol or order["side"] != plan.side:
            raise ValueError("LIQUIDITY_SUBMISSION_SCOPE_INVALID")
        control = from_snapshot(context["liquidity_controls"], scope)
        if control.values["mode"] != "ENFORCE":
            return None
        book, quote = context.get("liquidity_evidence"), context.get("execution_quote")
        # Immutable decision evidence cannot have been observed after decision.
        for evidence, names in (
            (book, ("observed_at_ms", "received_at_ms")),
            (quote, ("book_at_ms",)),
        ):
            if isinstance(evidence, dict) and any(
                type(evidence.get(name)) is int and evidence[name] > plan.decided_at_ms
                for name in names
            ):
                raise ValueError("STRATEGY_LIQUIDITY_POST_DECISION_EVIDENCE")
        assessment = evaluate_liquidity(
            evidence=book,
            controls=control.values,
            symbol=plan.symbol,
            side=plan.side,
            quantity=str(order["quantity"]),
            reference_price=plan.entry_price,
            now_ms=now_ms,
        )
        if assessment["reason"] != "LIQUIDITY_PASSED":
            raise ValueError(assessment["reason"])
        execution = evaluate_execution_quote(
            evidence=quote,
            controls=control.values,
            symbol=plan.symbol,
            now_ms=now_ms,
        )
        if execution["reason"] != "LIQUIDITY_EXECUTION_PASSED":
            raise ValueError(execution["reason"])
        return min(
            book["observed_at_ms"] + control.values["max_age_ms"],
            quote["book_at_ms"] + control.values["max_execution_quote_age_ms"],
        )
    row = conn.execute(
        "SELECT e.snapshot#>'{features,context}' FROM v2_orders o "
        "JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
        "JOIN v2_decision_evidence e USING(evidence_ref) WHERE o.order_id=%s",
        (order["order_id"],),
    ).fetchone()
    context = row[0] if row and isinstance(row[0], dict) else {}
    saved = context.get("liquidity_controls")
    if saved is None:
        # The config fence above permits absence only for legacy version 0.
        return None
    control = from_snapshot(saved, scope)
    if control.values["mode"] != "ENFORCE":
        return None
    if (
        context.get("account_scope")
        != {
            "exchange": scope.exchange,
            "account_id": scope.account_id,
            "environment": scope.environment,
            "product": scope.product,
        }
        or context.get("symbol") != order["symbol"]
    ):
        raise ValueError("LIQUIDITY_SUBMISSION_SCOPE_INVALID")
    book = context.get("liquidity_evidence")
    assessment = evaluate_liquidity(
        evidence=book,
        controls=control.values,
        symbol=order["symbol"],
        side=order["side"],
        quantity=str(order["quantity"]),
        reference_price=str(context.get("directional", {}).get("price")),
        now_ms=now_ms,
    )
    if assessment["reason"] != "LIQUIDITY_PASSED":
        raise ValueError(assessment["reason"])
    quote = context.get("directional", {}).get("leverage_venue")
    execution = evaluate_execution_quote(
        evidence=quote,
        controls=control.values,
        symbol=order["symbol"],
        now_ms=now_ms,
    )
    if execution["reason"] != "LIQUIDITY_EXECUTION_PASSED":
        raise ValueError(execution["reason"])
    return min(
        book["observed_at_ms"] + control.values["max_age_ms"],
        quote["book_at_ms"] + control.values["max_execution_quote_age_ms"],
    )
