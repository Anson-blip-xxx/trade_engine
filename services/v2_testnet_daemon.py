"""Explicit composition and supervised loop for the directional Testnet stack.

Construction starts no threads and performs no I/O.  Entry, protection creation
and reduce-only exit permissions are separate booleans; LIVE scope is rejected.
"""

from dataclasses import asdict
from uuid import uuid4

from services.v2_directional_exit import DirectionalExitStage
from services.v2_directional_exit_market import BinanceDirectionalExitMarket
from services.v2_directional_followup import DirectionalFollowupStage
from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_trading_pipeline import TradingPipeline
from v2_core.state import BusinessState, StateKey
from v2_core.venue_readiness import GuardedOpeningSubmit


def create_directional_testnet_pipeline(
    *,
    runtime,
    request,
    public_market,
    market,
    regime,
    schedulers,
    mark_reference,
    clock_ms,
    exit_fee_rate,
    enable_entries=False,
    enable_protection_writes=False,
    enable_reduce_only_exits=False,
    external_position_exclusions=(),
):
    flags = (enable_entries, enable_protection_writes, enable_reduce_only_exits)
    scope = runtime.scope
    gate = runtime.execution.submit
    if (
        any(type(flag) is not bool for flag in flags)
        or scope is None
        or (scope.exchange, scope.environment, scope.product)
        != ("BINANCE", "SANDBOX", "FUTURES")
        or not isinstance(gate, GuardedOpeningSubmit)
        or gate.enabled != enable_entries
        or gate.reduce_only_enabled != enable_reduce_only_exits
        or getattr(request, "account_id", None) != scope.account_id
        or getattr(request, "environment", None) != scope.environment
        or getattr(public_market, "environment", None) != scope.environment
        or getattr(regime, "environment", None) != scope.environment
        or not all(
            callable(port)
            for port in (request, public_market, mark_reference, clock_ms)
        )
    ):
        raise ValueError("explicit matching Testnet daemon dependencies required")
    if enable_entries and not enable_protection_writes:
        raise ValueError("entry dispatch requires protection creation permission")
    observe = BinanceDirectionalExitMarket(
        public_market,
        environment=scope.environment,
        clock_ms=clock_ms,
        exit_fee_rate=exit_fee_rate,
    )
    protection = DirectionalProtectionStage(
        runtime.data._connect,
        request,
        scope=scope,
        reference=mark_reference,
        clock_ms=clock_ms,
        allow_writes=enable_protection_writes,
        excluded_position_symbols=external_position_exclusions,
    )
    exits = DirectionalExitStage(
        runtime.data._connect,
        request,
        runtime=runtime,
        scope=scope,
        observe=observe,
        clock_ms=clock_ms,
        reference=mark_reference,
        allow_writes=enable_reduce_only_exits,
        excluded_position_symbols=external_position_exclusions,
    )
    settlement = DirectionalSettlementStage(
        runtime.data._connect,
        request,
        scope=scope,
        clock_ms=clock_ms,
        excluded_position_symbols=external_position_exclusions,
    )
    followups = DirectionalFollowupStage(
        runtime.data._connect,
        public_market,
        scope=scope,
        clock_ms=clock_ms,
    )
    return TradingPipeline(
        runtime=runtime,
        market=market,
        regime=regime,
        schedulers=schedulers,
        protection=protection,
        exits=exits,
        settlement=settlement,
        followups=followups,
        enable_entries=enable_entries,
    )


class TestnetTradingDaemon:
    __test__ = False

    def __init__(self, pipeline, *, stop, notify, interval_seconds=10):
        if (
            not isinstance(pipeline, TradingPipeline)
            or pipeline.scope.environment != "SANDBOX"
            or not callable(getattr(stop, "is_set", None))
            or not callable(getattr(stop, "wait", None))
            or not callable(notify)
            or type(interval_seconds) is not int
            or not 1 <= interval_seconds <= 60
        ):
            raise ValueError("explicit bounded Testnet daemon required")
        self.pipeline, self.stop, self.notify, self.interval = (
            pipeline,
            stop,
            notify,
            interval_seconds,
        )
        self.scope, self.connect = pipeline.scope, pipeline.connect
        self.store = BusinessState(self.connect)
        self.key = StateKey(
            **asdict(self.scope), namespace="testnet-trading-daemon-v1", key="latest"
        )

    def _checkpoint(self, payload):
        previous = self.store.read(self.key)
        result = self.store.change(
            self.key,
            expected_version=previous.version if previous else 0,
            request_key=str(uuid4()),
            payload={
                **payload,
                "account_scope": asdict(self.scope),
                "entry_dispatch_enabled": self.pipeline.enable_entries,
                "protection_writes_enabled": getattr(
                    self.pipeline.protection, "allow_writes", False
                ),
                "reduce_only_exits_enabled": getattr(
                    self.pipeline.exits, "allow_writes", False
                ),
            },
            reason="TESTNET_TRADING_DAEMON_CYCLE",
        )
        if result.code != "APPLIED":
            raise ValueError("daemon checkpoint conflict")

    def run_once(self):
        # Durable start marker must commit before any stage or exchange action.
        self._checkpoint({"status": "STARTING"})
        result = self.pipeline.run_once()
        self._checkpoint(
            {
                "status": "RUNNING"
                if result.get("status") in {"CYCLE_COMPLETE", "ENTRY_BLOCKED", "BUSY"}
                else "DEGRADED",
                "pipeline_status": result.get("status", "INVALID"),
                "cycle_id": result.get("cycle_id"),
            }
        )
        return result

    def serve(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - safe external supervision notice
                try:
                    self.notify(
                        {
                            "status": "UNAVAILABLE",
                            "error_code": type(exc).__name__,
                            "account_scope": asdict(self.scope),
                        }
                    )
                except Exception:  # noqa: BLE001, S110 - outer supervisor remains alive
                    pass
            self.stop.wait(self.interval)
