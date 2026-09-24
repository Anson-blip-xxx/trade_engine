"""Pure, bounded recovery transitions; persisted inside the PG capital snapshot.

Only verified account observations advance time. Read-only admission checks
may tighten a gate but cannot spend time to unlock it. Hard halts are latched.
"""

from decimal import Decimal


def transition(health, previous, settings, *, now_ms=None, outcomes=()):
    previous = previous or {}
    state = dict(previous.get("recovery", {}))
    state.setdefault("mode", "ACTIVE")
    state.setdefault("attempts", 0)
    dd = Decimal(health["drawdown"])
    if state["mode"] == "HALTED" or dd >= Decimal(settings["capital.halt_drawdown"]):
        state["mode"] = "HALTED"
    elif settings["recovery.enabled"]:
        if state["mode"] == "ACTIVE" and dd >= Decimal(
            settings["capital.defensive_drawdown"]
        ):
            state.update(mode="PAUSED", paused_at_ms=now_ms)
        if state["mode"] == "PAUSED" and now_ms is not None:
            if state.get("paused_at_ms") is None:
                state["paused_at_ms"] = now_ms
            if now_ms - state["paused_at_ms"] >= settings["recovery.cooldown_ms"]:
                if state["attempts"] >= settings["recovery.max_attempts"]:
                    state["mode"] = "HALTED"
                else:
                    state.update(
                        mode="PROBE",
                        attempts=state["attempts"] + 1,
                        probe_started_at_ms=now_ms,
                        probe_base=health["base"],
                    )
        elif state["mode"] == "PROBE":
            baseline = Decimal(state["probe_base"])
            loss = baseline - Decimal(health["base"])
            failed = sum(Decimal(pnl) < 0 for pnl in outcomes)
            if (
                loss >= baseline * Decimal(settings["recovery.loss_fraction"])
                or failed >= settings["recovery.max_losses"]
            ):
                state.update(
                    mode="HALTED"
                    if state["attempts"] >= settings["recovery.max_attempts"]
                    else "PAUSED",
                    paused_at_ms=now_ms,
                )
            elif (
                len(outcomes) >= settings["recovery.min_settlements"]
                and sum(map(Decimal, outcomes), Decimal(0)) > 0
                and dd < Decimal(settings["capital.reduce_drawdown"])
            ):
                # Attempts are a lifetime budget for this capital anchor, not
                # reset by a winning streak, midnight, or process restart.
                state["mode"] = "ACTIVE"
    factor = health["factor"]
    if state["mode"] in {"PAUSED", "HALTED"}:
        factor = "0"
    elif state["mode"] == "PROBE":
        factor = str(min(Decimal(factor), Decimal(settings["recovery.probe_factor"])))
    return {**health, "factor": factor, "recovery": state}
