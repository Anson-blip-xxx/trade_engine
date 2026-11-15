"""Join validated market envelopes and scoped account/analysis observations.

No neutral/default market facts, fake balances or refreshed source timestamps.
Supplied account provider must produce the full versioned evidence contract.
"""

from copy import deepcopy
from dataclasses import asdict


def _require_current(now, signal, market, account=None):
    """Only proven expiry is a candidate wait; malformed/future facts are not."""
    intervals = [
        (signal["observed_at"], signal["expires_at_ms"]),
        (market["assembled_at"], market["valid_until_ms"]),
    ]
    if account is not None:
        intervals.append((account["observed_at_ms"], account["valid_until_ms"]))
    if (
        type(now) is not int
        or now < 0
        or any(
            type(value) is not int or value < 0
            for interval in intervals
            for value in interval
        )
        or any(start >= end for start, end in intervals)
    ):
        raise ValueError("INVALID_CONTEXT_TIMESTAMPS")
    # Check every interval before expiry: an expired signal must not hide a
    # future account observation or another invalid source.
    if any(now < start for start, _ in intervals):
        raise ValueError("FUTURE_CONTEXT")
    if now >= signal["expires_at_ms"]:
        raise ValueError("SIGNAL_EXPIRED_DURING_CONTEXT")
    if any(now >= end for _, end in intervals[1:]):
        raise ValueError("STALE_CONTEXT")


class DirectionalContext:
    def __init__(
        self, market_context, account_context, *, scope, clock_ms, market_screen=None
    ):
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
        self.market_screen = market_screen

    def __call__(self, signal):
        return self._assemble(signal)

    def with_signal_fusion(self, signal, proof):
        """Inject public cross-source evidence before the cheap market screen.

        The shared provider validates the proof and replay validates it again.
        This port never replaces fresh market/account evidence or timestamps.
        In particular a confirming peer must not qualify a market-only result
        without collecting the original full account context.
        """
        if not isinstance(proof, dict):
            raise TypeError("SIGNAL_FUSION_PROOF_REQUIRED")
        return self._assemble(signal, signal_fusion=proof)

    def _assemble(self, signal, *, signal_fusion=None):
        market = deepcopy(self.market(signal))
        if signal_fusion is not None:
            market["signal_fusion"] = deepcopy(signal_fusion)
        now = self.clock()
        if (
            market["environment"] != self.scope.environment
            or market["symbol"] != signal["symbol"]
        ):
            raise ValueError("CONTEXT_SCOPE_MISMATCH")
        _require_current(now, signal, market)
        if self.market_screen is not None:
            windows = market["sources"]["s3"]["features"]
            screening = {
                **market,
                "account_scope": asdict(self.scope),
                "assembled_at": now,
                "market_only": True,
                "directional": {
                    "market": windows,
                    "price": windows["15m"]["close"],
                    "regime": market["sources"]["s0"]["features"]["regime"],
                    "short_ratio": None,
                },
            }
            if self.market_screen(signal, screening):
                return screening
        account = deepcopy(self.account(signal))
        now = self.clock()
        if (
            market["environment"] != self.scope.environment
            or market["symbol"] != signal["symbol"]
            or account["account_scope"] != asdict(self.scope)
            or account["symbol"] != signal["symbol"]
        ):
            raise ValueError("CONTEXT_SCOPE_MISMATCH")
        if not isinstance(account["evidence"], dict) or not account["evidence"]:
            raise ValueError("ACCOUNT_CONTEXT_EVIDENCE_INVALID")
        _require_current(now, signal, market, account)
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
                "leverage_venue": account.get("leverage_venue"),
            },
        }
