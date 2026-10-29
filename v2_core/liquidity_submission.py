"""Last-mile opening fence; no public-network calls and no exit-path hooks."""

from v2_core.liquidity import evaluate_execution_quote, evaluate_liquidity
from v2_core.liquidity_controls import from_snapshot, validate_submission


def validate_opening_liquidity(conn, scope, order, *, now_ms):
    validate_submission(conn, scope, order["order_id"])
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
