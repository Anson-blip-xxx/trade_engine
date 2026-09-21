"""Join validated market envelopes and scoped account/analysis observations.

No neutral/default market facts, fake balances or refreshed source timestamps.
Supplied account provider must produce the full versioned evidence contract.
"""

from copy import deepcopy
from dataclasses import asdict


class DirectionalContext:
    def __init__(self, market_context, account_context, *, scope, clock_ms):
        if scope.environment != "SANDBOX" or not all(
            callable(p) for p in (market_context, account_context, clock_ms)
        ):
            raise ValueError("explicit testnet context sources required")
        self.market, self.account, self.scope, self.clock = (
            market_context,
            account_context,
            scope,
            clock_ms,
        )

    def __call__(self, signal):
        market = deepcopy(self.market(signal))
        account = deepcopy(self.account(signal))
        now = self.clock()
        if (
            market["environment"] != self.scope.environment
            or market["symbol"] != signal["symbol"]
            or account["account_scope"] != asdict(self.scope)
            or account["symbol"] != signal["symbol"]
            or not isinstance(account["evidence"], dict)
            or not account["evidence"]
        ):
            raise ValueError("context source scope or evidence missing")
        times = (
            now,
            market["assembled_at"],
            market["valid_until_ms"],
            account["observed_at_ms"],
            account["valid_until_ms"],
        )
        if any(type(t) is not int or t < 0 for t in times) or not (
            market["assembled_at"] <= now < market["valid_until_ms"]
            and account["observed_at_ms"] <= now < account["valid_until_ms"]
        ):
            raise ValueError("stale or future pipeline context")
        windows = market["sources"]["s3"]["features"]
        regime = market["sources"]["s0"]["features"]["regime"]
        return {
            **market,
            "account_scope": asdict(self.scope),
            "assembled_at": now,
            "valid_until_ms": min(market["valid_until_ms"], account["valid_until_ms"]),
            "account_evidence": account["evidence"],
            "directional": {
                "market": windows,
                "price": windows["15m"]["close"],
                "regime": regime,
                "short_ratio": account["short_ratio"],
                "history": account["history"],
                "sizing": account["sizing"],
                "expected_move_pct": account["expected_move_pct"],
                "funding_rate": account["funding_rate"],
            },
        }
