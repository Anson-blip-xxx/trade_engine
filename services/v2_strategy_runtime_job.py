"""Fixed Testnet typed entry target, executed only by the bounded job owner.

The disabled mode needs no venue keys and performs only metadata maintenance
and catalog observation. Enabled mode is explicit and still requires ACTIVE
route/deployment, current capital guards and exact immutable plan evidence.
No legacy evaluator, model, Redis context or dynamic import name is accepted.
"""

import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

from services.v2_entry_worker import EntryPorts
from services.v2_execution_guard import create_execution_guard
from services.v2_management_runtime import ManagementBinding
from services.v2_strategy_catalog_scan import scan_catalog
from services.v2_strategy_entry_job import (
    EntryJobSpec,
    entry_transaction,
    run_entry_work,
)
from v2_core.account_risk import _lock
from v2_core.shared_market import MarketPolicy
from v2_core.strategy_plan import PLAN_SCHEMA
from v2_core.strategy_registry import StrategySpec, strategy_identifier

SPEC = StrategySpec(
    "causal-trend", "causal-trend-v1", PLAN_SCHEMA, "causal-structure-v1", "risk-trail"
)


@dataclass(frozen=True)
class StrategyJobConfig:
    entry: EntryJobSpec
    entries_enabled: bool = False
    selected_leverage: int = 2
    scan_limit: int = 40

    def __post_init__(self):
        if (
            type(self.entry) is not EntryJobSpec
            or self.entry.specs != (SPEC,)
            or type(self.entries_enabled) is not bool
            or type(self.selected_leverage) is not int
            or self.selected_leverage not in {2, 3, 5}
            or type(self.scan_limit) is not int
            or not 1 <= self.scan_limit <= 100
        ):
            raise ValueError("TYPED_STRATEGY_JOB_CONFIG_REQUIRED")


def typed_entry_ports_for(tenant, target, values):
    """Controller-side metadata only; credentials stay inside the child job."""
    from services.v2_managed_testnet import scope_for

    mode = values.get("V2_CONTROLLER_ENABLE_ENTRIES", "false")
    leverage = values.get("V2_TYPED_LEVERAGE", "2")
    instance = values.get("V2_TYPED_INSTANCE", "causal-trend-v1")
    strategy_identifier(instance)
    if (
        values.get("V2_ENVIRONMENT", "SANDBOX") != "SANDBOX"
        or mode not in {"true", "false"}
        or leverage not in {"2", "3", "5"}
    ):
        raise ValueError("TYPED_STRATEGY_EXPLICIT_TESTNET_MODE_REQUIRED")
    binding = ManagementBinding(
        scope_for(tenant, target),
        tenant,
        target["target_registry"],
        target["binding_version"],
        target["epoch"],
        target["worker_token"],
    )
    entry = EntryJobSpec(
        binding,
        instance,
        (SPEC,),
        (SPEC.entry_handler,),
        (SPEC.exit_handler,),
        (PLAN_SCHEMA,),
        limit=5,
        budget_ms=20000,
    )
    config = StrategyJobConfig(entry, mode == "true", int(leverage))
    return EntryPorts(
        binding, typed_runtime_job, (config,), ("quant", "tv_bridge", "s3")
    )


def _clock():
    return time.time_ns() // 1000000


def _bounded_connect(connect):
    @contextmanager
    def bounded():
        with connect() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout='5s'")
            yield conn

    return bounded


class BoundedRiskArchive:
    """Compatibility for risk provenance only, not legacy strategy evaluation."""

    def __init__(self, reader):
        self.reader = reader

    def read(self, target, start, end):
        if (
            type(start) is not int
            or type(end) is not int
            or start % 60000
            or end % 60000
            or end - start != 86400000
        ):
            raise ValueError("TYPED_RISK_WINDOW_REQUIRED")
        return list(reversed(self.reader.read(target, closed_at_ms=end).bars))


def _market_client():
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18123,
        database="trade_v2_testnet",
        username="tradev2strategy",
        password="",
        connect_timeout=1,
        send_receive_timeout=10,
        query_limit=0,
        query_retries=0,
    )


def _dispatch_runtime(
    connect,
    *,
    binding,
    guard,
    private,
    public,
    reader,
    stop,
    signed_connection_factory=None,
    risk_reference=None,
):
    """Only typed initial OPEN. Position management owns recovery and CLOSE."""
    from services.v2_shared_risk_reference import SharedRiskReference
    from services.v2_testnet_daemon_entry import BinanceTestnetMark, PrivateRatePermit
    from v2_core.binance import BinanceFutures
    from v2_core.errors import SubmissionNotSent
    from v2_core.guarded_transport import GuardedSignedRequest
    from v2_core.managed_portfolio import ManagedPortfolio
    from v2_core.runtime import DataRuntime
    from v2_core.shared_market import AdmissionBudget
    from v2_core.strategy_opening import StrategyOpeningGate
    from v2_core.strategy_plan_store import read_trade_plan
    from v2_core.transport import BinanceSignedTransport
    from v2_core.venue_readiness import GuardedOpeningSubmit, TestnetVenueReadiness
    from v2_core.venue_settings import TestnetSymbolSettings

    scope = binding.scope
    write = entry_transaction(connect, binding=binding, guard=guard, stop=stop)
    request = GuardedSignedRequest(
        BinanceSignedTransport(
            account_id=scope.account_id,
            environment="SANDBOX",
            api_key=private["api_key"],
            api_secret=private["api_secret"],
            clock_ms=_clock,
            permit=PrivateRatePermit(
                AdmissionBudget(connect, environment="SANDBOX"),
                entries=True,
                protection=False,
                exits=False,
            ),
            enable_trading=True,
            enable_testnet_settings=True,
            timeout=3,
            **(
                {"connection_factory": signed_connection_factory}
                if signed_connection_factory is not None
                else {}
            ),
        ),
        account_id=scope.account_id,
        environment="SANDBOX",
        guard=guard,
    )
    venue = BinanceFutures(request, account_id=scope.account_id, environment="SANDBOX")
    gate = StrategyOpeningGate(write, scope=scope, clock_ms=_clock)

    # Runner requests contain the original intent payload, not an intent_id
    # within it. Resolve the owning episode from the order, under the same scope.
    def risk(order):
        if stop.is_set() or order.get("leg") != "OPEN":
            return False
        with write() as conn:
            row = conn.execute(
                "SELECT episode_id::text FROM v2_orders WHERE order_id=%s",
                (order["order_id"],),
            ).fetchone()
            plan = read_trade_plan(conn, row[0]) if row is not None else None
            return plan is not None and plan.scope == scope

    def terms(order):
        result = gate.terms(order)
        if result is None:
            raise SubmissionNotSent("VENUE_READINESS_BLOCKED")
        return result

    portfolio = ManagedPortfolio(connect, request, scope=scope, clock_ms=_clock)
    submit = GuardedOpeningSubmit(
        write,
        readiness=TestnetVenueReadiness(
            request, scope=scope, clock_ms=_clock, portfolio=portfolio
        ),
        reference=BinanceTestnetMark(public, clock_ms=_clock),
        plan=terms,
        submit=venue.submit,
        configure=TestnetSymbolSettings(
            write,
            request,
            scope=scope,
            clock_ms=_clock,
            allow_writes=True,
            portfolio=portfolio,
        ).ensure,
        enabled=True,
        reduce_only_enabled=False,
        validate_reference=gate.validate_reference,
        validate_reduction=gate.validate_reduction,
    )
    return DataRuntime(
        write,
        scope=scope,
        submit=submit,
        query=venue.query,
        risk_check=risk,
        clock_ms=_clock,
        risk_reference=risk_reference
        or SharedRiskReference(
            connect, scope=scope, archive=BoundedRiskArchive(reader), clock_ms=_clock
        ),
    )


def _real_producer(connect, *, config, source, stop, guard, reader, private):
    from services.v2_liquidity_context import LiveDepth
    from services.v2_strategy_account_facts import SandboxStrategyAccountFacts
    from services.v2_strategy_candidate_producer import CandidateProducer
    from services.v2_strategy_signal_maintenance import MaintainedEntryStore
    from services.v2_testnet_daemon_entry import PrivateRatePermit
    from v2_core import entry_controls, liquidity_controls
    from v2_core.managed_portfolio import publish_capital_snapshot
    from v2_core.public_market import BinancePublicMarket
    from v2_core.runtime_policy import PolicyStore
    from v2_core.shared_market import AdmissionBudget
    from v2_core.strategy_execution_context import POLICY_FIELDS, execution_context
    from v2_core.transport import BinanceSignedTransport

    binding, scope = config.entry.binding, config.entry.binding.scope
    budget = AdmissionBudget(connect, environment="SANDBOX")
    public = BinancePublicMarket(
        environment="SANDBOX", budget=budget, enabled=True, timeout=3
    )
    request = BinanceSignedTransport(
        account_id=scope.account_id,
        environment="SANDBOX",
        api_key=private["api_key"],
        api_secret=private["api_secret"],
        clock_ms=_clock,
        permit=PrivateRatePermit(budget, entries=False, protection=False, exits=False),
        timeout=3,
    )
    account = SandboxStrategyAccountFacts(
        connect,
        request,
        public,
        scope=scope,
        clock_ms=_clock,
        selected_leverage=config.selected_leverage,
        read_timeout_ms=3000,
        collection_timeout_ms=25000,
        stop=stop,
    )
    write = entry_transaction(connect, binding=binding, guard=guard, stop=stop)
    cached = {}

    def facts(state, deployment, frame):
        cached.clear()
        entry = entry_controls.read(connect, scope)
        liquidity = liquidity_controls.read(connect, scope)
        policy = PolicyStore(connect, scope).read().values
        if (
            entry.values["sizing.remaining_budget_enabled"]
            or entry.values["history.mode"] == "SOFT"
            or entry.values["reentry.mode"] == "ENFORCE"
            or policy["entry.reentry_enabled"]
        ):
            raise ValueError("STRATEGY_ENTRY_CONTROLS_UNSUPPORTED")
        depth = None
        if liquidity.values["mode"] != "OFF":
            depth = LiveDepth(connect, clock_ms=_clock)(state.snapshot["symbol"])
        observed = account(state, deployment, frame)
        verified = account.last_verified_account
        if verified is None:
            raise ValueError("TYPED_VERIFIED_CAPITAL_REQUIRED")
        with write() as conn:
            _lock(conn, scope.key)
            publish_capital_snapshot(
                lambda: nullcontext(conn),
                scope,
                account=verified.account,
                started=verified.started_at_ms,
                deadline=verified.valid_until_ms,
                observation_id=verified.observation_id,
            )
        quote = {
            **observed.leverage_proof["venue"],
            "environment": "SANDBOX",
            "source_host": "demo-fapi.binance.com",
        }
        cached.update(
            entry=entry.snapshot(),
            liquidity=liquidity.snapshot(),
            policy={key: policy[key] for key in POLICY_FIELDS},
            depth=depth,
            quote=quote,
        )
        return observed

    def execution(plan):
        return execution_context(
            plan,
            entry_controls=cached["entry"],
            liquidity_controls=cached["liquidity"],
            policy=cached["policy"],
            liquidity_evidence=cached["depth"],
            execution_quote=cached["quote"],
        )

    return CandidateProducer(
        connect,
        binding=binding,
        registry=config.entry.registry(),
        instance_id=config.entry.producer,
        source=source,
        market_reader=reader,
        account_reader=facts,
        execution_reader=execution,
        stop=stop,
        guard=guard,
        store_factory=MaintainedEntryStore,
    ), public


def typed_runtime_job(stop, config, source):
    """One finite job. Parent timeout remains the hard isolation boundary."""
    if type(config) is not StrategyJobConfig or source not in {
        "quant",
        "tv_bridge",
        "s3",
    }:
        raise ValueError("TYPED_STRATEGY_JOB_REQUIRED")
    if stop.is_set():
        return "STOPPED"
    from services.v2_managed_testnet import database, private_for
    from services.v2_strategy_market_reader import ClosedMarketReader
    from services.v2_strategy_signal_maintenance import (
        MaintainedEntryStore,
        SignalMaintenance,
        run_signal_maintenance,
    )

    connect = _bounded_connect(database())
    spec, binding = config.entry, config.entry.binding
    guard = create_execution_guard(connect, **binding.guard_args)
    guard.preflight(scope=binding.scope)
    maintained = run_signal_maintenance(
        connect,
        binding=binding,
        producer=spec.producer,
        source=source,
        stop=stop,
        guard=guard,
        deployment_window=True,
    )
    maintenance_blocked = maintained.get("status") != "COMPLETED" or any(
        item.get("status") == "BLOCKED"
        for lane in ("signals", "cleanup")
        for item in maintained.get(lane, {}).values()
    )
    if source == "quant" and not stop.is_set():
        scan_catalog(
            connect,
            binding=binding,
            instance_id=spec.producer,
            guard=guard,
            stop=stop,
            limit=config.scan_limit,
            catalog_max_age_ms=MarketPolicy.read(
                connect, "SANDBOX"
            ).catalog_max_age_seconds
            * 1000,
        )
    if not config.entries_enabled or stop.is_set():
        return (
            "STOPPED"
            if stop.is_set()
            else ("BLOCKED" if maintenance_blocked else "COMPLETED")
        )
    # Check current OPEN before decrypting or contacting any venue dependency.
    with entry_transaction(connect, binding=binding, guard=guard, stop=stop)():
        pass
    target = {
        "target_registry": binding.registry_id,
        "binding_version": binding.binding_version,
        "epoch": binding.epoch,
        "worker_token": binding.worker_token,
    }
    private = private_for(binding.tenant_id, target, binding.scope)
    client = _market_client()
    try:
        reader = ClosedMarketReader(client, clock_ms=_clock)
        producer, public = _real_producer(
            connect,
            config=config,
            source=source,
            stop=stop,
            guard=guard,
            reader=reader,
            private=private,
        )
        result = run_entry_work(
            connect,
            binding=binding,
            registry=spec.registry(),
            producer=spec.producer,
            source=source,
            stop=stop,
            clock_ms=_clock,
            limit=spec.limit,
            budget_ms=spec.budget_ms,
            history_page_size=spec.history_page_size,
            history_budget_ms=spec.history_budget_ms,
            guard=guard,
            deployment_window=True,
            candidate_producer=producer,
            maintenance=SignalMaintenance(
                connect,
                binding=binding,
                producer=spec.producer,
                source=source,
                stop=stop,
                guard=guard,
                deployment_window=True,
            ),
            store_factory=MaintainedEntryStore,
        )
        if stop.is_set():
            return "STOPPED"
        runtime = _dispatch_runtime(
            connect,
            binding=binding,
            guard=guard,
            private=private,
            public=public,
            reader=reader,
            stop=stop,
        )
        dispatched = dispatch_prepared(
            connect, runtime=runtime, config=config, guard=guard, stop=stop, limit=1
        )
        if any(
            outcome not in {"ACKNOWLEDGED", "FILLED", "PARTIALLY_FILLED"}
            for outcome in dispatched.values()
        ):
            return "BLOCKED"
        return "BLOCKED" if maintenance_blocked else result["status"]
    finally:
        client.close()


def dispatch_prepared(connect, *, runtime, config, guard, stop, limit=5):
    """Fair typed-only preparation scan; ambiguous orders are management work."""
    from dataclasses import asdict

    if (
        type(config) is not StrategyJobConfig
        or not config.entries_enabled
        or type(limit) is not int
        or not 1 <= limit <= 10
    ):
        raise ValueError("TYPED_DISPATCH_ENABLED_REQUIRED")
    binding, consumer = config.entry.binding, config.entry.binding.scope
    if runtime.scope != consumer:
        raise ValueError("TYPED_DISPATCH_SCOPE_MISMATCH")
    write = entry_transaction(connect, binding=binding, guard=guard, stop=stop)
    key = binding.scope.key + ":" + config.entry.producer
    with write() as conn:
        cursor = conn.execute(
            "SELECT order_id FROM v2_strategy_dispatch_cursors WHERE consumer=%s",
            (key,),
        ).fetchone()
        after = cursor[0] if cursor else ""
        rows = conn.execute(
            "SELECT o.order_id::text FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
            "JOIN v2_decision_evidence e USING(evidence_ref) WHERE "
            "(i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s) "
            "AND i.producer=%s AND e.snapshot ? 'strategy_plan' AND o.leg='OPEN' "
            "AND o.status='PREPARED' ORDER BY (o.order_id::text<=%s),o.order_id LIMIT %s",
            (*asdict(consumer).values(), config.entry.producer, after, limit),
        ).fetchall()
    results = {}
    for (order_id,) in rows:
        if stop.is_set():
            break
        with write() as conn:
            conn.execute(
                "INSERT INTO v2_strategy_dispatch_cursors(consumer,order_id) VALUES(%s,%s) "
                "ON CONFLICT(consumer) DO UPDATE SET order_id=EXCLUDED.order_id",
                (key, order_id),
            )
        try:
            results[order_id] = runtime.execution.dispatch(order_id)
        except Exception:  # noqa: BLE001 - next job reconciles original identity, never blind POST
            results[order_id] = "RECONCILIATION_REQUIRED"
    return results
