"""Fresh LIVE public depth evidence for SANDBOX opening decisions only.

No credentials, order endpoint, background loop, or local business-state files.
Frozen decision evidence owns the snapshot; replay never fetches a newer book.
"""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256

from services.v2_directional_replay import replay_decision
from v2_core.evidence import canonical
from v2_core.liquidity_controls import read
from v2_core.public_market import BinancePublicMarket, PublicMarketError, symbol
from v2_core.runtime_policy import PolicyStore
from v2_core.shared_market import HostBudget


class LiveDepth:
    """A single bounded, host-budgeted GET; no retry inside an entry cycle."""

    def __init__(self, connect, *, clock_ms, transport=None, connection_factory=None):
        if transport is not None and getattr(transport, "environment", None) != "LIVE":
            raise ValueError("LIQUIDITY_LIVE_TRANSPORT_REQUIRED")
        self.clock = clock_ms
        self.transport = transport or BinancePublicMarket(
            environment="LIVE",
            budget=HostBudget(connect, environment="LIVE", lane="market"),
            enabled=True,
            timeout=1,
            depth=True,
            **(
                {"connection_factory": connection_factory} if connection_factory else {}
            ),
        )

    def __call__(self, instrument):
        symbol(instrument)
        started = self.clock()
        try:
            raw = self.transport("/fapi/v1/depth", {"symbol": instrument, "limit": 100})
            received = self.clock()
            if (
                not isinstance(raw, dict)
                or any(
                    type(raw.get(key)) is not int for key in ("T", "E", "lastUpdateId")
                )
                or not 0 <= raw["T"] <= raw["E"] <= received
                or not 0 <= started <= received
                or raw["lastUpdateId"] <= 0
                or any(
                    not isinstance(raw.get(key), list) or not 1 <= len(raw[key]) <= 100
                    for key in ("bids", "asks")
                )
            ):
                raise ValueError("invalid depth envelope")
            return {
                "version": "binance-depth-v1",
                "symbol": instrument,
                "source_environment": "LIVE",
                "source_host": "fapi.binance.com",
                "request_started_ms": started,
                "observed_at_ms": raw["T"],
                "output_at_ms": raw["E"],
                "received_at_ms": received,
                "last_update_id": raw["lastUpdateId"],
                "requested_limit": 100,
                "bids": deepcopy(raw["bids"]),
                "asks": deepcopy(raw["asks"]),
                "raw_digest": sha256(canonical(raw).encode()).hexdigest(),
            }
        except Exception as exc:  # noqa: BLE001 - public response details stay private
            code = (
                str(exc)
                if isinstance(exc, PublicMarketError)
                and str(exc)
                in {"QUOTA_DENIED", "RATE_LIMITED", "PUBLIC_TRANSPORT_FAILURE"}
                else "DEPTH_UNAVAILABLE"
            )
            return {
                "version": "binance-depth-v1",
                "status": "UNAVAILABLE",
                "reason": code,
                "symbol": instrument,
                "source_environment": "LIVE",
                "source_host": "fapi.binance.com",
            }


class LiquidityContext:
    def __init__(self, connect, delegate, *, scope, strategy, source, clock_ms, depth):
        if scope.environment != "SANDBOX" or strategy not in {"S6", "S8"}:
            raise ValueError("LIQUIDITY_TESTNET_SCOPE_REQUIRED")
        self.connect, self.delegate, self.scope = connect, delegate, scope
        self.strategy, self.source = strategy, source
        self.clock, self.depth = clock_ms, depth

    def __call__(self, signal):
        context = deepcopy(self.delegate(signal))
        if (
            context.get("market_only")
            or context.get("universe_admission", {}).get("eligible") is False
        ):
            return context
        control = read(self.connect, self.scope)
        # Do not spend HTTP budget on signals already rejected by sizing,
        # funding, structural re-entry, portfolio risk or directional gates.
        if control.values["mode"] != "OFF":
            preliminary = replay_decision(
                signal,
                context,
                {
                    "mode": "REPLAY_ONLY",
                    "strategy": self.strategy,
                    "source": self.source,
                    "analysis_mode": "hard",
                    "account_scope": asdict(self.scope),
                    "policy": PolicyStore(self.connect, self.scope).read().values,
                },
            )
            if preliminary.rationale == "DIRECTIONAL_REPLAY_ONLY":
                context["liquidity_evidence"] = self.depth(signal["symbol"])
                now = self.clock()
                if (
                    not context["assembled_at"]
                    <= now
                    < min(context["valid_until_ms"], signal["expires_at_ms"])
                ):
                    raise ValueError("LIQUIDITY_CONTEXT_EXPIRED")
                # Other frozen proofs (e.g. layered history) bind assembled_at.
                # A later depth read must not rewrite their historical as-of.
                context["liquidity_as_of_ms"] = now
                observed = context["liquidity_evidence"].get("observed_at_ms")
                if (
                    control.values["mode"] == "ENFORCE"
                    and type(observed) is int
                    and observed <= now < observed + control.values["max_age_ms"]
                ):
                    context["valid_until_ms"] = min(
                        context["valid_until_ms"],
                        observed + control.values["max_age_ms"],
                    )
        if read(self.connect, self.scope).snapshot() != control.snapshot():
            raise ValueError("LIQUIDITY_CONTROLS_CHANGED_DURING_CONTEXT")
        context["liquidity_controls"] = control.snapshot()
        return context
