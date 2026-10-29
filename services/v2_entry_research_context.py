"""Bounded new-entry research, isolated from exit/protection processing.

Policy and evidence are frozen with the decision. Shadow observations cannot
boost allocation or bypass existing gates. Never use asynchronous verdicts.
"""

from copy import deepcopy

from services.v2_directional_replay import market_plan
from services.v2_layered_history import LayeredHistory
from v2_core.entry_controls import read
from v2_core.runtime_policy import PolicyStore


class EntryResearchContext:
    def __init__(self, connect, delegate, *, scope, strategy, source, clock_ms):
        if (
            scope.environment != "SANDBOX"
            or strategy not in {"S6", "S8"}
            or source not in {"s3", "tv_bridge"}
        ):
            raise ValueError("ENTRY_RESEARCH_SCOPE_REQUIRED")
        self.connect, self.delegate, self.scope = connect, delegate, scope
        self.strategy, self.source, self.clock = strategy, source, clock_ms
        self.history = LayeredHistory(connect, scope=scope, producer=strategy.lower())

    def __call__(self, signal):
        context = deepcopy(self.delegate(signal))
        if (
            context.get("market_only")
            or context.get("universe_admission", {}).get("eligible") is False
        ):
            return context
        control = read(self.connect, self.scope)
        saved = context.get("entry_controls")
        account_saved = context.get("account_evidence", {}).get("entry_controls")
        if any(
            value is not None and value != control.snapshot()
            for value in (saved, account_saved)
        ):
            raise ValueError("ENTRY_CONTROLS_CHANGED_DURING_CONTEXT")
        context["entry_controls"] = control.snapshot()
        if control.values["history.mode"] != "OFF":
            plan, _ = market_plan(
                signal,
                context,
                {
                    "strategy": self.strategy,
                    "source": self.source,
                    "policy": PolicyStore(self.connect, self.scope).read().values,
                },
            )
            if plan.reason == "CANDIDATE":
                result = self.history.evaluate(
                    {**signal, "source": self.source}, context, plan, control
                )
                context["layered_history"] = result.as_dict()
        if (
            not context["assembled_at"]
            <= self.clock()
            < min(context["valid_until_ms"], signal["expires_at_ms"])
        ):
            raise ValueError("ENTRY_RESEARCH_CONTEXT_EXPIRED")
        return context
