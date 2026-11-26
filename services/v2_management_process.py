"""Minimal, explicitly fenced Testnet position-management composition.

Construction performs no I/O. This process has no entry, strategy scheduler,
settlement, archive, cache, projector or notification dependency.
"""

from dataclasses import dataclass
from decimal import Decimal

from services.v2_directional_exit import DirectionalExitStage
from services.v2_directional_exit_market import BinanceDirectionalExitMarket
from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_management_runtime import ManagementBinding, ManagementSupervisor
from services.v2_testnet_daemon_bootstrap import TestnetDaemonConfig
from services.v2_testnet_daemon_entry import (
    BinanceTestnetMark,
    PrivateRatePermit,
)
from v2_core.binance import BinanceFutures
from v2_core.errors import SubmissionNotSent
from v2_core.guarded_transport import GuardedSignedRequest
from v2_core.precision import amount
from v2_core.public_market import BinancePublicMarket, PublicRateBudget
from v2_core.runtime import DataRuntime
from v2_core.transport import BinanceSignedTransport
from v2_core.venue_readiness import GuardedOpeningSubmit, TestnetVenueReadiness


@dataclass(frozen=True)
class ManagementProcessConfig:
    """Only management inputs; entry/model settings are deliberately absent."""

    daemon: TestnetDaemonConfig
    exit_fee_rate: str
    shared_market: bool = False

    def __post_init__(self):
        daemon = self.daemon
        if (
            not isinstance(daemon, TestnetDaemonConfig)
            or daemon.enable_entries is not False
            or type(daemon.interval_seconds) is not int
            or any(
                type(flag) is not bool
                for flag in (
                    daemon.enable_protection_writes,
                    daemon.enable_reduce_only_exits,
                )
            )
            or type(self.shared_market) is not bool
            or not isinstance(self.exit_fee_rate, str)
        ):
            raise ValueError("EXPLICIT_MANAGEMENT_CONFIG_REQUIRED")
        # Validate direct construction as well as mappings. The actual write
        # acknowledgement is checked by from_mapping before this reconstruction.
        TestnetDaemonConfig.from_mapping(
            {
                "V2_ACCOUNT_ID": daemon.account_id,
                "V2_ENVIRONMENT": "SANDBOX",
                "V2_INTERVAL_SECONDS": str(daemon.interval_seconds),
                "V2_ENABLE_ENTRIES": "false",
                "V2_ENABLE_PROTECTION_WRITES": str(
                    daemon.enable_protection_writes
                ).lower(),
                "V2_ENABLE_REDUCE_ONLY_EXITS": str(
                    daemon.enable_reduce_only_exits
                ).lower(),
                "V2_TESTNET_WRITE_ACK": "SANDBOX:" + daemon.account_id,
            }
        )
        fee = amount(self.exit_fee_rate)
        if not 0 <= fee <= Decimal(".01") or self.exit_fee_rate != format(fee, "f"):
            raise ValueError("CANONICAL_MANAGEMENT_EXIT_FEE_REQUIRED")

    @classmethod
    def from_mapping(cls, values):
        daemon = TestnetDaemonConfig.from_mapping(values)
        mode = values.get("V2_SHARED_MARKET", "false")
        if mode not in ("true", "false"):
            raise ValueError("EXPLICIT_SHARED_MARKET_MODE_REQUIRED")
        return cls(daemon, values.get("V2_EXIT_FEE_RATE"), mode == "true")


class ManagementSignedRequest(GuardedSignedRequest):
    """Deny opening/settings capability even while the route is ACTIVE."""

    def __call__(self, method, path, params, *, admission_ticket=None):
        # Preserve the classified scalar request while waiting for the route
        # lock; caller mutation must not turn a reduction into an opening.
        if isinstance(params, dict):
            params = dict(params)
        if method != "GET" and self.operation(method, path, params) != "MANAGE":
            raise SubmissionNotSent("ENDPOINT_OR_WRITE_DISABLED")
        return super().__call__(method, path, params, admission_ticket=admission_ticket)


def _opening_disabled(_order):
    raise SubmissionNotSent("ENDPOINT_OR_WRITE_DISABLED")


def create_management_process(
    config,
    secrets,
    *,
    connect,
    binding,
    execution_guard,
    clock_ms,
    signed_connection_factory=None,
    public_connection_factory=None,
):
    """Build real management ports; the caller owns release and loop cadence."""
    if (
        not isinstance(config, ManagementProcessConfig)
        or not isinstance(binding, ManagementBinding)
        or config.daemon.account_id != binding.scope.account_id
        or config.daemon.enable_entries is not False
        or not callable(connect)
        or not callable(clock_ms)
        or not callable(execution_guard)
        or getattr(execution_guard, "_args", None) != binding.guard_args
        or set(secrets) != {"BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"}
    ):
        raise ValueError("EXPLICIT_MANAGEMENT_PROCESS_REQUIRED")
    scope = binding.scope
    public_budget = PublicRateBudget(connect, scope="v2-testnet-public", limit=1200)
    private_budget = PublicRateBudget(
        connect, scope="v2-testnet-private:" + scope.account_id, limit=2400
    )
    if config.shared_market:
        from v2_core.shared_market import HostBudget

        public_budget = private_budget = HostBudget(connect, environment="SANDBOX")
    public = BinancePublicMarket(
        environment="SANDBOX",
        budget=public_budget,
        enabled=True,
        **(
            {"connection_factory": public_connection_factory}
            if public_connection_factory is not None
            else {}
        ),
    )
    private = BinanceSignedTransport(
        account_id=scope.account_id,
        environment="SANDBOX",
        api_key=secrets["BINANCE_TESTNET_API_KEY"],
        api_secret=secrets["BINANCE_TESTNET_API_SECRET"],
        clock_ms=clock_ms,
        permit=PrivateRatePermit(
            private_budget,
            entries=False,
            protection=config.daemon.enable_protection_writes,
            exits=config.daemon.enable_reduce_only_exits,
        ),
        enable_trading=config.daemon.enable_reduce_only_exits,
        enable_testnet_cancellation=config.daemon.enable_protection_writes,
        enable_testnet_order_cancellation=config.daemon.enable_protection_writes,
        enable_testnet_protection=config.daemon.enable_protection_writes,
        enable_testnet_settings=False,
        **(
            {"connection_factory": signed_connection_factory}
            if signed_connection_factory is not None
            else {}
        ),
    )
    request = ManagementSignedRequest(
        private,
        account_id=scope.account_id,
        environment=scope.environment,
        guard=execution_guard,
    )
    venue = BinanceFutures(
        request, account_id=scope.account_id, environment=scope.environment
    )
    mark = BinanceTestnetMark(public, clock_ms=clock_ms)
    submit = GuardedOpeningSubmit(
        connect,
        readiness=TestnetVenueReadiness(
            request,
            scope=scope,
            clock_ms=clock_ms,
        ),
        reference=mark,
        plan=_opening_disabled,
        submit=venue.submit,
        enabled=False,
        reduce_only_enabled=config.daemon.enable_reduce_only_exits,
    )
    runtime = DataRuntime(
        connect,
        scope=scope,
        submit=submit,
        query=venue.query,
        risk_check=lambda order: (
            order.get("leg") == "CLOSE" and order.get("reduce_only") is True
        ),
        clock_ms=clock_ms,
    )
    protection = DirectionalProtectionStage(
        connect,
        request,
        scope=scope,
        reference=mark,
        clock_ms=clock_ms,
        allow_writes=config.daemon.enable_protection_writes,
    )
    exits = DirectionalExitStage(
        connect,
        request,
        runtime=runtime,
        scope=scope,
        observe=BinanceDirectionalExitMarket(
            public,
            environment=scope.environment,
            clock_ms=clock_ms,
            exit_fee_rate=config.exit_fee_rate,
        ),
        reference=mark,
        clock_ms=clock_ms,
        allow_writes=config.daemon.enable_reduce_only_exits,
    )
    return ManagementSupervisor(
        runtime,
        protection,
        exits,
        binding=binding,
        guard=execution_guard,
        clock_ms=clock_ms,
    )
