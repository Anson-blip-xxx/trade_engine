"""Single-signal trusted producer bridge, not installed in a running service.

Original receipts/candidates win before *any* fact collection. Only fresh server
bars and explicit scoped sizing facts may form a new frozen plan. Reader ports
are trusted composition, not HTTP/client inputs; their authentication and hard
wall-time isolation remain the caller's responsibility. This bridge does not
submit orders, acquire credentials, invent legacy scores, or scan the universe.
"""

from contextlib import contextmanager
from dataclasses import asdict, replace
from uuid import UUID

from services.v2_execution_guard import create_execution_guard
from services.v2_management_runtime import ManagementBinding
from services.v2_strategy_entry_job import entry_transaction
from services.v2_strategy_market_reader import ClosedMarketBars
from v2_core.account_risk import _lock
from v2_core.evidence import canonical
from v2_core.ingress import milliseconds
from v2_core.precision import amount
from v2_core.shared_market import Catalog
from v2_core.strategy import StrategyScope
from v2_core.strategy_candidate_builder import (
    BUILDER_VERSION,
    CandidatePolicy,
    assemble_frozen_candidate,
    build_candidate,
)
from v2_core.strategy_deployment import StrategyDeployments, _key, _read_current
from v2_core.strategy_entry_store import StrategyEntryStore
from v2_core.strategy_registry import StrategyRegistry, strategy_identifier
from v2_core.strategy_sizing import (
    StrategySizingFacts,
    StrategySizingPolicy,
    size_candidate,
)

_CLOCK = "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"


def _policies(binding):
    config = binding.config
    builder, sizing, producer = (
        config.get(name) for name in ("candidate_builder", "sizing", "producer")
    )
    if (
        type(builder) is not dict
        or set(builder) != {"schema", "policy"}
        or builder["schema"] != BUILDER_VERSION
        or type(sizing) is not dict
        or set(sizing) != {"schema", "policy"}
        or sizing["schema"] != "strategy-sizing-v1"
        or type(producer) is not dict
        or set(producer)
        != {"schema", "catalog_max_age_ms", "exit_version", "exit_config"}
        or producer["schema"] != "strategy-candidate-producer-v1"
        or type(producer["catalog_max_age_ms"]) is not int
        or not 1000 <= producer["catalog_max_age_ms"] <= 7200000
        or type(producer["exit_config"]) is not dict
    ):
        raise ValueError("STRATEGY_PRODUCER_CONFIG_REQUIRED")
    strategy_identifier(producer["exit_version"])
    values = dict(sizing["policy"])
    if type(values.get("allowed_leverages")) is not list:
        raise ValueError("STRATEGY_PRODUCER_SIZING_CONFIG_INVALID")
    values["allowed_leverages"] = tuple(values["allowed_leverages"])
    return (
        CandidatePolicy(**builder["policy"]),
        StrategySizingPolicy(**values),
        producer,
    )


def _rules(row, target):
    if (
        type(row) is not dict
        or row.get("symbol") != target
        or row.get("supported") is not True
        or row.get("status") != "TRADING"
        or row.get("contractType") != "PERPETUAL"
        or row.get("quoteAsset") != "USDT"
        or type(row.get("filters")) is not list
        or not 1 <= len(row["filters"]) <= 32
    ):
        raise ValueError("STRATEGY_PRODUCER_MARKET_UNAVAILABLE")
    filters = row["filters"]
    if any(
        type(item) is not dict or type(item.get("filterType")) is not str
        for item in filters
    ):
        raise ValueError("STRATEGY_PRODUCER_FILTERS_INVALID")
    found = {item["filterType"]: item for item in filters}
    if len(found) != len(filters):
        raise ValueError("STRATEGY_PRODUCER_FILTERS_INVALID")
    try:
        lot = found.get("MARKET_LOT_SIZE") or found["LOT_SIZE"]
        price, notional = found["PRICE_FILTER"], found["MIN_NOTIONAL"]
        return {
            "quantity_step": lot["stepSize"],
            "min_quantity": lot["minQty"],
            "max_quantity": lot["maxQty"],
            "price_tick": price["tickSize"],
            "min_price": price["minPrice"],
            "max_price": price["maxPrice"],
            "min_notional": notional.get("notional", notional.get("minNotional")),
        }
    except KeyError:
        raise ValueError("STRATEGY_PRODUCER_FILTERS_INVALID") from None


class CandidateProducer:
    """Explicit server ports, no route auto-activation and no venue write port.

    ``market_reader.read(symbol, closed_at_ms=...)`` returns closed bar facts.
    ``account_reader(state, deployment, frame)`` returns StrategySizingFacts.
    ``execution_reader(plan)`` returns the existing plan-bound safety context.
    New UNKNOWN/NO_SETUP/NO_BUDGET observations are not fake terminal receipts;
    the caller must separately account for deferred/expired unfrozen work.
    """

    def __init__(
        self,
        connect,
        *,
        binding,
        registry,
        instance_id,
        source,
        market_reader,
        account_reader,
        execution_reader,
        stop,
        guard=None,
        store_factory=StrategyEntryStore,
    ):
        if (
            not callable(connect)
            or type(binding) is not ManagementBinding
            or type(registry) is not StrategyRegistry
            or source not in {"s2", "s3", "tv_bridge", "quant"}
            or not callable(getattr(market_reader, "read", None))
            or not callable(account_reader)
            or not callable(execution_reader)
            or not callable(getattr(stop, "is_set", None))
            or not callable(store_factory)
        ):
            raise TypeError("STRATEGY_PRODUCER_PORTS_REQUIRED")
        strategy_identifier(instance_id)
        self.connect, self.binding, self.registry = connect, binding, registry
        self.instance_id, self.source, self.stop = instance_id, source, stop
        self.scope = StrategyScope(**asdict(binding.scope), producer=instance_id)
        self.market, self.account, self.execution = (
            market_reader,
            account_reader,
            execution_reader,
        )
        self.guard = guard or create_execution_guard(connect, **binding.guard_args)
        self.write = entry_transaction(
            connect, binding=binding, guard=self.guard, stop=stop
        )
        self.store_factory = store_factory
        self.store = store_factory(connect)

    @contextmanager
    def _read_connection(self):
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='1s'")
            yield conn

    def _state(self, identity):
        with self._read_connection() as conn:
            return self.store.read(
                conn, scope=self.scope, source=self.source, signal_id=identity
            )

    def _now(self):
        with self._read_connection() as conn:
            return conn.execute(_CLOCK).fetchone()[0]

    @staticmethod
    def _recovery(state):
        if state.receipt is not None or state.candidate is not None:
            return {
                "status": "RECOVERY_REQUIRED",
                "signal_id": state.signal_id,
                "candidate_digest": None
                if state.candidate is None
                else state.candidate.candidate_digest,
                "receipt": None if state.receipt is None else state.receipt.outcome,
                "execution_authorized": False,
            }
        return None

    def _freeze(self, candidate):
        @contextmanager
        def final_transaction():
            # Fence route before Store.freeze takes signal UPDATE. Account lock
            # comes only AFTER yield, retaining route -> signal -> account order.
            with self.write() as conn:
                yield conn
                _lock(conn, self.binding.scope.key)
                current = _read_current(
                    conn, _key(self.binding.scope, self.instance_id)
                )
                if current != candidate.binding:
                    raise ValueError("STRATEGY_PRODUCER_DEPLOYMENT_CHANGED")
                if conn.execute(_CLOCK).fetchone()[0] >= candidate.expires_at_ms:
                    raise ValueError("STRATEGY_PRODUCER_EXPIRED_BEFORE_COMMIT")

        return self.store_factory(final_transaction).freeze(candidate)

    def prepare(self, signal_id):
        identity = str(UUID(signal_id))
        if self.stop.is_set():
            return self._deferred(identity, "STRATEGY_PRODUCER_STOPPED")
        state = self._state(identity)
        if recovery := self._recovery(state):
            return recovery
        snapshot = state.snapshot
        if snapshot["signal"] == "EVENT_END":
            return self._deferred(identity, "STRATEGY_PRODUCER_NON_ENTRY_SIGNAL")
        if not snapshot["observed_at"] <= state.now_ms < snapshot["expires_at_ms"]:
            return self._deferred(identity, "STRATEGY_PRODUCER_SIGNAL_NOT_CURRENT")
        # Short probe; never hold route/account/signal locks during reader I/O.
        with self.write():
            pass
        deployment = StrategyDeployments(self._read_connection, self.registry).read(
            self.binding.scope, self.instance_id
        )
        if deployment is None or deployment.mode != "ACTIVE":
            return self._deferred(identity, "STRATEGY_PRODUCER_DEPLOYMENT_DISABLED")
        self.registry.resolve_entry(deployment)
        builder_policy, sizing_policy, config = _policies(deployment)
        catalog = Catalog(self._read_connection, state.environment, self._now).snapshot(
            config["catalog_max_age_ms"]
        )
        rules = _rules(catalog["contracts"].get(snapshot["symbol"]), snapshot["symbol"])
        frame = self.market.read(
            snapshot["symbol"], closed_at_ms=state.now_ms // 60000 * 60000
        )
        if type(frame) is not ClosedMarketBars or (
            frame.symbol != snapshot["symbol"]
            or frame.environment != state.environment
            or frame.closed_at_ms != state.now_ms // 60000 * 60000
        ):
            raise ValueError("STRATEGY_PRODUCER_MARKET_SCOPE_MISMATCH")
        latest = self._state(identity)
        if recovery := self._recovery(latest):
            return recovery
        if self.stop.is_set():
            return self._deferred(identity, "STRATEGY_PRODUCER_STOPPED")
        if latest.now_ms >= snapshot["expires_at_ms"]:
            return self._deferred(identity, "STRATEGY_PRODUCER_SIGNAL_NOT_CURRENT")
        if frame.closed_at_ms < snapshot["observed_at"]:
            return self._deferred(identity, "STRATEGY_PRODUCER_WAIT_CLOSED_MINUTE")
        initial = build_candidate(
            frame.bars,
            symbol=frame.symbol,
            environment=frame.environment,
            closed_at_ms=frame.closed_at_ms,
            available_at_ms=frame.available_at_ms,
            decided_at_ms=latest.now_ms,
            policy=builder_policy,
        )
        if initial.status != "CANDIDATE":
            return self._deferred(identity, initial.reason, observation=initial.status)
        # Account I/O is bounded by its port/process owner, never run in a DB tx.
        facts = self.account(state, deployment, frame)
        if type(facts) is not StrategySizingFacts:
            raise TypeError("STRATEGY_PRODUCER_TYPED_ACCOUNT_FACTS_REQUIRED")
        if facts.scope != self.binding.scope or facts.symbol != snapshot["symbol"]:
            raise ValueError("STRATEGY_PRODUCER_ACCOUNT_SCOPE_MISMATCH")
        if any(
            amount(getattr(facts, name)) != amount(value)
            for name, value in rules.items()
        ):
            raise ValueError("STRATEGY_PRODUCER_CATALOG_RULE_MISMATCH")
        latest = self._state(identity)
        if recovery := self._recovery(latest):
            return recovery
        if self.stop.is_set():
            return self._deferred(identity, "STRATEGY_PRODUCER_STOPPED")
        now = latest.now_ms
        if now >= snapshot["expires_at_ms"]:
            return self._deferred(identity, "STRATEGY_PRODUCER_SIGNAL_NOT_CURRENT")
        if not 0 <= now - catalog["observed_at_ms"] <= config["catalog_max_age_ms"]:
            raise ValueError("CONTRACT_CATALOG_STALE")
        decision = build_candidate(
            frame.bars,
            symbol=frame.symbol,
            environment=frame.environment,
            closed_at_ms=frame.closed_at_ms,
            available_at_ms=frame.available_at_ms,
            decided_at_ms=now,
            policy=builder_policy,
        )
        if decision.status != "CANDIDATE":
            return self._deferred(
                identity, decision.reason, observation=decision.status
            )
        venue = facts.leverage_proof.get("venue")
        if type(venue) is not dict:
            raise ValueError("STRATEGY_PRODUCER_VENUE_FACTS_REQUIRED")
        venue_at, book_at = (
            milliseconds(venue.get(name)) for name in ("observed_at_ms", "book_at_ms")
        )
        # Inclusive age checks become an exclusive absolute expiry. Slow safety
        # reads / signal or account lock waits cannot freeze stale quote facts.
        expires = min(
            snapshot["expires_at_ms"],
            now + sizing_policy.entry_ttl_ms,
            now + builder_policy.max_decision_delay_ms + 1,
            frame.available_at_ms + builder_policy.max_decision_delay_ms + 1,
            decision.structure["last_closed_at_ms"]
            + builder_policy.max_structure_age_ms
            + 1,
            catalog["observed_at_ms"] + config["catalog_max_age_ms"] + 1,
            facts.quote_at_ms + sizing_policy.max_quote_age_ms + 1,
            book_at + sizing_policy.max_quote_age_ms + 1,
            venue_at + sizing_policy.max_fact_age_ms + 1,
            *(
                stamp + sizing_policy.max_fact_age_ms + 1
                for stamp in (
                    facts.observed_at_ms,
                    facts.account_at_ms,
                    facts.filters_at_ms,
                )
            ),
        )
        if now >= expires:
            return self._deferred(identity, "STRATEGY_PRODUCER_FACTS_EXPIRED")
        sized = size_candidate(
            decision,
            binding=deployment,
            policy=sizing_policy,
            facts=facts,
            expires_at_ms=expires,
            exit_version=config["exit_version"],
            exit_config=config["exit_config"],
        )
        if sized.status != "SIZED":
            return self._deferred(identity, sized.reason, observation=sized.status)
        execution = self.execution(sized.plan)
        latest = self._state(identity)
        if recovery := self._recovery(latest):
            return recovery
        if latest.now_ms >= expires:
            return self._deferred(identity, "STRATEGY_PRODUCER_FACTS_EXPIRED")
        candidate = assemble_frozen_candidate(
            decision,
            signal=latest,
            binding=deployment,
            plan=sized.plan,
            execution=execution,
        )
        candidate = replace(
            candidate,
            features_json=canonical(
                {
                    **candidate.features,
                    "capital": sized.capital_evidence,
                    "sizing": sized.evidence,
                    "catalog": {
                        "digest": catalog["digest"],
                        "observed_at_ms": catalog["observed_at_ms"],
                    },
                }
            ),
        )
        # Re-read recovery after safety reader I/O as well. The store still
        # resolves the remaining race under signal lock; never overwrite it.
        if recovery := self._recovery(self._state(identity)):
            return recovery
        try:
            saved = self._freeze(candidate)
        except ValueError as exc:
            if str(exc) not in {
                "STRATEGY_ENTRY_ALREADY_TERMINAL",
                "STRATEGY_PRODUCER_DEPLOYMENT_CHANGED",
                "STRATEGY_PRODUCER_EXPIRED_BEFORE_COMMIT",
                "EXECUTION_FENCED",
            }:
                raise
            recovery = self._recovery(self._state(identity))
            if recovery is None:
                raise
            return recovery
        return {
            "status": "FROZEN",
            "signal_id": identity,
            "candidate_digest": saved.candidate_digest,
            "execution_authorized": False,
        }

    @staticmethod
    def _deferred(identity, reason, **details):
        return {
            "status": "DEFERRED",
            "signal_id": identity,
            "reason": reason,
            "execution_authorized": False,
            **details,
        }
