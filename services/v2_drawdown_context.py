"""Bind durable drawdown evidence to injected strategy/replay contexts.

Does not fetch balances, start a daemon or authorize an order. Freshness is
shortened to the balance observation deadline; no timer refresh on cache reads.
"""

import json

from v2_core.drawdown import DrawdownState
from v2_core.evidence import canonical


class DrawdownContext:
    def __init__(self, provider, drawdown):
        if not callable(provider) or not isinstance(drawdown, DrawdownState):
            raise TypeError("explicit context provider and durable drawdown required")
        self.provider, self.drawdown = provider, drawdown

    def __call__(self, signal):
        context = json.loads(canonical(self.provider(signal)))
        key = self.drawdown.key
        expected = {
            field: getattr(key, field)
            for field in ("exchange", "account_id", "environment", "product")
        }
        if (
            context["account_scope"] != expected
            or context["environment"] != key.environment
        ):
            raise ValueError("drawdown context account mismatch")
        if context["symbol"] != signal["symbol"]:
            raise ValueError("drawdown context symbol mismatch")
        snapshot = self.drawdown.current()
        evidence = json.loads(snapshot.payload_json)
        observed_at = evidence["observation"]["observed_at_ms"]
        assembled_at, deadline = context["assembled_at"], context["valid_until_ms"]
        if (
            type(assembled_at) is not int
            or type(deadline) is not int
            or not observed_at <= assembled_at < deadline
        ):
            raise ValueError("drawdown observation outside context time")
        deadline = min(deadline, observed_at + evidence["config"]["max_age_ms"] + 1)
        if assembled_at >= deadline:
            raise ValueError("drawdown context expired")
        context["valid_until_ms"] = deadline
        context["directional"]["sizing"]["drawdown_factor"] = evidence["state"][
            "factor"
        ]
        context["directional"]["sizing"]["balance"] = evidence["observation"]["balance"]
        context["drawdown"] = {
            "state_id": key.identity,
            "version": snapshot.version,
            **evidence,
        }
        return context
