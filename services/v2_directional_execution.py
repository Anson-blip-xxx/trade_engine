"""Wire the actual-account first-entry gate into an existing Testnet runtime.

Does not enable a transport or install account policy, change leverage, or start
a trading loop. Caller still supplies verified mark data and protection readiness.
"""

from dataclasses import asdict

from v2_core.venue_readiness import GuardedOpeningSubmit, TestnetVenueReadiness


def bind_directional_venue_gate(
    runtime,
    request,
    *,
    mark_reference,
    enabled=False,
    reduce_only_enabled=False,
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
        return {"leverage": desired["leverage"], "margin_type": desired["margin_mode"]}

    gate = GuardedOpeningSubmit(
        runtime.data._connect,
        readiness=TestnetVenueReadiness(
            request, scope=scope, clock_ms=runtime.clock_ms
        ),
        reference=mark_reference,
        plan=plan,
        submit=runtime.execution.submit,
        enabled=enabled,
        reduce_only_enabled=reduce_only_enabled,
    )
    runtime.execution.submit = gate
    return gate
