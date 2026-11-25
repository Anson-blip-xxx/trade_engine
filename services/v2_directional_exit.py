"""Durable legacy/immutable-plan Testnet exits and reduce-only dispatch.

Every decision is derived from immutable entry evidence, reconciled fills and a
fresh injected market observation.  The native close-all stop remains installed
during active exits; Binance reduce-only semantics prevent either path from
opening reverse exposure.  Writes are disabled unless explicitly enabled.
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from uuid import UUID, uuid4

from services.v2_directional_protection import DirectionalStopRecovery
from v2_core.account_coverage import AccountCoverageAudit, coverage_from_facts
from v2_core.account_inventory import persist_inventory
from v2_core.directional_exit import ExitFacts, evaluate_directional_exit
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.opening_halt import halt_opening
from v2_core.precision import derived_amount, money_context
from v2_core.protection import TestnetProtection
from v2_core.scheduling import diagnostic_code
from v2_core.service import TradingData
from v2_core.session_lock import session_advisory_lock
from v2_core.state import BusinessState, StateKey, StateSnapshot
from v2_core.strategy_exit import StrategyExitFacts, evaluate_strategy_exit
from v2_core.strategy_plan import TradePlan
from v2_core.strategy_plan_store import read_trade_plan
from v2_core.symbol_reduction import symbol_reduction_proof

_TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}
_ISOLATABLE_COVERAGE = {"LOCAL_ORDERS_NOT_FINAL", "MISSING_CONFIRMED_STOP"}


def _research_clock(clock):
    """Best-effort local query interval, never an execution freshness clock."""
    try:
        value = clock()
        return value if type(value) is int and 0 <= value < 2**53 else None
    except Exception:  # noqa: BLE001 - optional evidence cannot stop an exit
        return None


def _capture_exit_research(
    *,
    scope,
    episode,
    symbol,
    at_ms,
    opened_at_ms,
    facts,
    decision,
    policy,
    observation,
    hourly,
    spec,
    protection,
    query_started,
    query_finished,
    quantity_evidence=None,
):
    """Attach bounded research to an existing write; no new I/O or authority."""
    try:
        # Optional dependency: even an unavailable adapter cannot block the
        # already evaluated baseline exit or change its order request identity.
        from services.v2_exit_research_snapshot import build_exit_research_snapshot

        snapshot = build_exit_research_snapshot(
            scope=scope,
            episode=episode,
            symbol=symbol,
            at_ms=at_ms,
            opened_at_ms=opened_at_ms,
            facts={**facts, "momentum_closes_15m": list(facts["momentum_closes_15m"])},
            baseline=decision.evidence(),
            baseline_policy=dict(policy),
            # The existing stage clock runs after the complete observer call.
            # This is a conservative available-by receipt, not exchange time.
            observation={**observation, "available_at_ms": at_ms},
            hourly=(
                {
                    **hourly,
                    "market_source": hourly.get(
                        "market_source",
                        "BINANCE_USDM_SANDBOX"
                        if observation.get("environment") == "SANDBOX"
                        and observation.get("source")
                        in ("binance-public-exit-v1", "binance-public-exit-v2")
                        else None,
                    ),
                    "available_at_ms": at_ms,
                }
                if isinstance(hourly, dict)
                else None
            ),
            native={
                "account_scope": asdict(scope),
                "spec": asdict(spec),
                "response": protection.get("research_terms"),
                "observed_at_ms": query_started,
                "available_at_ms": query_finished,
                # The prior coverage audit already acquired this observation.
                # Its original clock is retained, never refreshed to exit time.
                "quantity_evidence": quantity_evidence,
            },
        )
        encoded = canonical(snapshot)
        if len(encoded.encode()) > 65536:
            raise ValueError("RESEARCH_CAPTURE_BOUND")
    except Exception:  # noqa: BLE001 - research must not obstruct a baseline exit
        snapshot = {
            "version": "prospective-exit-research-snapshot-v1",
            "status": "UNKNOWN",
            "reason": "RESEARCH_CAPTURE_UNAVAILABLE",
            "execution_authorized": False,
            "promotion_authorized": False,
        }
        encoded = canonical(snapshot)
    return snapshot, digest(encoded)


def _ledger_digest(trace):
    return digest(
        canonical(
            {
                key: trace[key]
                for key in ("intent_id", "data_revision", "orders", "fills", "request")
            }
        )
    )


class DirectionalExitStage:
    def __init__(
        self,
        connect,
        request,
        *,
        runtime,
        scope,
        observe,
        clock_ms,
        reference,
        allow_writes=False,
        excluded_position_symbols=(),
        limit=10,
        quant_market=None,
    ):
        if (
            runtime.scope != scope
            or runtime.data._connect is not connect
            or (scope.exchange, scope.environment, scope.product)
            != ("BINANCE", "SANDBOX", "FUTURES")
            or not all(callable(port) for port in (observe, clock_ms, reference))
            or type(allow_writes) is not bool
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise ValueError("explicit bounded Testnet exit stage required")
        self.connect, self.request, self.runtime, self.scope = (
            connect,
            request,
            runtime,
            scope,
        )
        self.observe, self.clock, self.allow_writes, self.limit = (
            observe,
            clock_ms,
            allow_writes,
            limit,
        )
        self.data = TradingData(connect)
        self.quant_market = quant_market
        self.store = BusinessState(connect)
        self.cursor = StateKey(
            **asdict(scope), namespace="directional-exit-scan-v1", key="cursor"
        )
        self.audit = AccountCoverageAudit(
            connect,
            request,
            scope=scope,
            clock_ms=clock_ms,
            excluded_position_symbols=excluded_position_symbols,
        )
        self.stop = DirectionalStopRecovery(
            connect,
            request,
            scope=scope,
            reference=reference,
            clock_ms=clock_ms,
            allow_writes=False,
            excluded_position_symbols=excluded_position_symbols,
        )
        self.protection = TestnetProtection(self.store, request, scope=scope)

    def _key(self, episode):
        return StateKey(
            **asdict(self.scope), namespace="directional-exit-v1", key=episode
        )

    def _read_scan_cursor(self):
        # This diagnostic namespace has no generic state-history/coverage
        # indirections. Read its raw payload so a malformed reserved key cannot
        # invoke BusinessState's business-evidence decoders and block all exits.
        # Account/namespace identity and database availability are not repairable
        # paging content: failures here still propagate without any venue action.
        with self.connect() as conn:
            row = conn.execute(
                "SELECT version,payload,deleted,scope FROM v2_business_state WHERE state_id=%s",
                (self.cursor.identity,),
            ).fetchone()
        if row is None:
            return None
        if row[3] != asdict(self.cursor):
            raise ValueError("EXIT_SCAN_CURSOR_IDENTITY_CONFLICT")
        return StateSnapshot(row[0], json.dumps(row[1], separators=(",", ":")), row[2])

    def _save(self, episode, payload, request_key):
        key = self._key(episode)
        previous = self.store.read(key)
        if previous is not None and previous.payload_json == canonical(payload):
            return key, previous
        result = self.store.change(
            key,
            expected_version=previous.version if previous else 0,
            request_key=request_key,
            payload=payload,
            reason="DIRECTIONAL_EXIT_OBSERVATION",
        )
        if result.code not in {"APPLIED", "ALREADY_APPLIED"}:
            raise ValueError("EXIT_STATE_CONFLICT")
        return key, self.store.read(key)

    def _trace(self, episode):
        trace = self.data.trace(str(UUID(episode)))
        if trace is None or any(
            trace["scope"].get(k) != v for k, v in asdict(self.scope).items()
        ):
            raise ValueError("DIRECTIONAL_EXIT_SCOPE_MISMATCH")
        with self.connect() as conn:
            plan = read_trade_plan(conn, episode)
        if plan is not None:
            if plan.scope != self.scope:
                raise ValueError("STRATEGY_EXIT_SCOPE_MISMATCH")
            sizing = {
                "stop_price": plan.stop_price,
                "planned_loss": plan.planned_loss,
                "quantity_step": plan.quantity_step,
            }
        else:
            if (
                trace["scope"]["producer"] not in {"s6", "s8"}
                or trace["configuration"].get("mode") != "TESTNET_ADMISSION_ONLY"
                or trace["configuration"].get("account_scope") != asdict(self.scope)
            ):
                raise ValueError("DIRECTIONAL_EXIT_SCOPE_MISMATCH")
            evaluation = trace["decision"]["features"]["evaluation"]
            plan, sizing = evaluation["market_plan"], evaluation["sizing"]
            if (
                evaluation.get("admission_authorized") is not True
                or evaluation.get("execution_market_gate") != "MARKET_GATES_PASSED"
                or plan.get("system_tag") not in {"S6A", "S6B", "S8"}
                or sizing.get("reason") != "SIZED"
            ):
                raise ValueError("DIRECTIONAL_EXIT_EVIDENCE_REQUIRED")
        orders = {row["order_id"]: row for row in trace["orders"]}
        opening = [row for row in trace["orders"] if row["leg"] == "OPEN"]
        if len(opening) != 1:
            raise ValueError("SINGLE_OPENING_ORDER_REQUIRED")
        if isinstance(plan, TradePlan) and (
            amount(opening[0]["quantity"], positive=True) != amount(plan.quantity)
            or trace["request"]["symbol"] != plan.symbol
            or trace["request"]["side"] != plan.side
        ):
            raise ValueError("STRATEGY_EXIT_OPENING_MISMATCH")
        quantities, value, opened, closed, first = (
            {},
            Decimal(0),
            Decimal(0),
            Decimal(0),
            None,
        )
        with money_context():
            for fill in trace["fills"]:
                order = orders[fill["order_id"]]
                qty, price = (
                    amount(fill["quantity"], positive=True),
                    amount(fill["price"], positive=True),
                )
                quantities[fill["order_id"]] = (
                    quantities.get(fill["order_id"], Decimal(0)) + qty
                )
                if order["leg"] == "OPEN":
                    opened += qty
                    value += qty * price
                    first = (
                        fill["occurred_at_ms"]
                        if first is None
                        else min(first, fill["occurred_at_ms"])
                    )
                else:
                    closed += qty
            remaining = opened - closed
            if opened <= 0 or remaining <= 0 or first is None:
                raise ValueError("ACTIVE_LEDGER_EXPOSURE_REQUIRED")
            entry = value / opened
            if isinstance(plan, TradePlan) and (
                opened > amount(plan.quantity) or closed < 0
            ):
                raise ValueError("STRATEGY_EXIT_LEDGER_MISMATCH")
            planned = (
                amount(sizing["planned_loss"], positive=True)
                * remaining
                / (amount(plan.quantity) if isinstance(plan, TradePlan) else opened)
            )
            # Division can repeat even though every original fill is exact.
            # Normalize only derived decision inputs, never fills or quantity.
            # Round remaining risk upward so a positive dust exposure cannot
            # lose its risk denominator or understate the allocated risk.
            entry = derived_amount(entry, rounding=ROUND_HALF_EVEN, positive=True)
            planned = derived_amount(planned, rounding=ROUND_CEILING, positive=True)
        pending = [
            row
            for row in trace["orders"]
            if row["leg"] == "CLOSE" and row["status"] not in _TERMINAL
        ]
        partial_done = any(
            row["leg"] == "CLOSE"
            and (
                row["request_key"] == "exit:partial-take-profit"
                or row["request_key"].startswith("exit:partial-take-profit:retry:")
            )
            and quantities.get(row["order_id"], Decimal(0)) > 0
            for row in trace["orders"]
        )
        return (
            trace,
            opening[0],
            plan,
            sizing,
            entry,
            remaining,
            planned,
            first,
            partial_done,
            pending,
        )

    def _observation(self, symbol, *, strategy=False):
        raw = self.observe(symbol)
        now = self.clock()
        if (
            not isinstance(raw, dict)
            or raw.get("symbol") != symbol
            or raw.get("environment") != "SANDBOX"
            or type(raw.get("observed_at_ms")) is not int
            or type(now) is not int
            or not 0 <= now - raw["observed_at_ms"] < 10000
            or (
                not strategy
                and (
                    not isinstance(raw.get("momentum_closes_15m"), (list, tuple))
                    or len(raw["momentum_closes_15m"]) != 4
                )
            )
        ):
            raise ValueError("STALE_OR_INVALID_EXIT_OBSERVATION")
        fields = (
            ("mark_price", "exit_fee_rate")
            if strategy
            else (
                "mark_price",
                "funding_rate",
                "ema9_1h",
                "ema20_1h",
                "exit_fee_rate",
            )
        )
        for field in fields:
            if field not in raw:
                raise ValueError("INCOMPLETE_EXIT_OBSERVATION")
        return (
            dict(raw)
            if strategy
            else {**raw, "momentum_closes_15m": list(raw["momentum_closes_15m"])}
        ), now

    def evaluate(self, episode):
        (
            trace,
            opening,
            plan,
            sizing,
            entry,
            remaining,
            planned,
            first,
            partial_done,
            pending,
        ) = self._trace(episode)
        if pending:
            outcomes = {
                order["order_id"]: self.runtime.execution.recover(order["order_id"])
                for order in pending
            }
            return {"status": "RECOVERING_CLOSE", "orders": outcomes}
        spec = self.stop.plan(opening["order_id"])
        query_started = _research_clock(self.clock)
        protection = self.protection.query(spec, research=True)
        query_finished = _research_clock(self.clock)
        if protection["status"] != "NEW":
            return {
                "status": "WAITING_NATIVE_PROTECTION",
                "parent_status": protection["status"],
            }
        observation, now = self._observation(
            trace["request"]["symbol"], strategy=isinstance(plan, TradePlan)
        )
        # Private transport rows never alter the executing observation/digest.
        # Only the bounded, validated research projection below is persisted.
        research_hourly = observation.pop("_research_hourly", None)
        if any(
            row["leg"] == "CLOSE"
            and row["status"] in {"REJECTED", "CANCELLED"}
            and row["request_evidence"].get("observed_at_ms", 0)
            >= observation["observed_at_ms"]
            for row in trace["orders"]
        ):
            return {
                "status": "BLOCKED",
                "reason": "REJECTED_EXIT_AWAITING_FRESH_OBSERVATION",
            }
        saved = self.store.read(self._key(episode))
        prior = json.loads(saved.payload_json) if saved else {}
        strategy_plan = plan if isinstance(plan, TradePlan) else None
        if strategy_plan is not None and prior:
            from v2_core.strategy_lifecycle import plan_provenance

            if prior.get("strategy_plan") != plan_provenance(strategy_plan):
                raise ValueError("STRATEGY_EXIT_STATE_MISMATCH")
        facts_common = {
            "side": trace["request"]["side"],
            "entry_price": str(entry),
            "mark_price": observation["mark_price"],
            "stop_price": sizing["stop_price"],
            "remaining_quantity": str(remaining),
            "planned_loss": str(planned),
            "held_ms": now - first,
            "peak_return_pct": prior.get("peak_return_pct", "0"),
            "partial_done": partial_done,
            "exit_fee_rate": observation["exit_fee_rate"],
        }
        facts = (
            StrategyExitFacts(**facts_common)
            if strategy_plan is not None
            else ExitFacts(
                **facts_common,
                system_tag=plan["system_tag"],
                funding_rate=observation["funding_rate"],
                ema9_1h=observation["ema9_1h"],
                ema20_1h=observation["ema20_1h"],
                momentum_closes_15m=tuple(observation["momentum_closes_15m"]),
            )
        )
        from v2_core.runtime_policy import PolicyStore

        policy = PolicyStore(self.connect, self.scope).read()
        decision = (
            evaluate_strategy_exit(strategy_plan, facts)
            if strategy_plan is not None
            else evaluate_directional_exit(facts, policy=policy.values)
        )
        quant = prior.get("quant_exit")
        quant_failure = None
        if (
            strategy_plan is None
            and trace["decision"].get("source") == "quant"
            and policy.values["quant.trailing_exit_enabled"]
        ):
            try:
                if self.quant_market is None:
                    raise ValueError("QUANT_EXIT_PORT_REQUIRED")
                quant = self.quant_market(
                    entry_proof=trace["decision"]["features"]["context"]["quant"],
                    symbol=trace["request"]["symbol"],
                    side=trace["request"]["side"],
                    opened_at_ms=first,
                    entry_price=str(entry),
                    hard_stop=sizing["stop_price"],
                    mark_price=observation["mark_price"],
                    previous=quant,
                    now=now,
                )
                if quant["status"] == "CLOSE" and decision.action in {
                    "WAIT",
                    "PARTIAL",
                }:
                    decision = replace(
                        decision,
                        action="CLOSE",
                        reason="QUANT_ATR_TRAIL",
                        quantity_fraction="1",
                    )
            except Exception as exc:  # noqa: BLE001 - keep emergency/native exits alive
                quant_failure = type(exc).__name__
        quantity_step = (
            strategy_plan.quantity_step
            if strategy_plan is not None
            else trace["decision"]["features"]["context"]["directional"]["sizing"][
                "quantity_step"
            ]
        )
        amount(quantity_step, positive=True)
        encoded_facts = asdict(facts)
        if strategy_plan is None:
            encoded_facts["momentum_closes_15m"] = list(facts.momentum_closes_15m)
        encoded_facts["peak_return_pct"] = decision.peak_return_pct
        from v2_core.strategy_observations import observe_excursions

        excursions_error = None
        try:
            excursions = observe_excursions(
                prior.get("excursions"), now_ms=now, return_pct=decision.return_pct
            )
        except (ValueError, KeyError, TypeError, ArithmeticError):
            # Analytics cannot delay an emergency close or native protection.
            excursions = prior.get("excursions")
            excursions_error = "SAMPLED_EXCURSIONS_UNAVAILABLE"
        payload = {
            "episode": episode,
            "account_scope": asdict(self.scope),
            "strategy_version": strategy_plan.code_version
            if strategy_plan
            else "directional-admission-v2-1",
            "entry_decision_digest": digest(canonical(trace["decision"])),
            "opening_order_id": opening["order_id"],
            "target_ledger_digest": _ledger_digest(trace),
            "facts": encoded_facts,
            "observation": observation,
            "decision": decision.evidence(),
            "policy_version": policy.version,
            "policy_digest": policy.digest,
            "policy": policy.values,
            "peak_return_pct": decision.peak_return_pct,
            "partial_done": partial_done,
            "execution_authorized": False,
            "quant_exit": quant,
            "quant_exit_error": quant_failure,
            "excursions": excursions,
            "excursions_error": excursions_error,
        }
        if strategy_plan is not None:
            from v2_core.strategy_lifecycle import plan_provenance

            payload.update(
                source="strategy-exit-v1",
                strategy_plan=plan_provenance(strategy_plan),
                exit_handler=strategy_plan.exit_handler,
                exit_version=strategy_plan.exit_version,
                exit_config=strategy_plan.exit_config,
            )
        if strategy_plan is None and decision.reason == "LOW_YIELD_STAGNATION":
            try:
                research_quantity = self.audit.research_position(
                    trace["request"]["symbol"]
                )
            except Exception:  # noqa: BLE001 - optional, no new inventory call
                research_quantity = None
            snapshot, snapshot_digest = _capture_exit_research(
                scope=self.scope,
                episode=episode,
                symbol=trace["request"]["symbol"],
                at_ms=now,
                opened_at_ms=first,
                facts=encoded_facts,
                decision=decision,
                policy=policy.values,
                observation=observation,
                hourly=research_hourly,
                spec=spec,
                protection=protection,
                query_started=query_started,
                query_finished=query_finished,
                quantity_evidence=research_quantity,
            )
            payload["research_snapshot"] = snapshot
            payload["research_snapshot_digest"] = snapshot_digest
        identity = digest(
            canonical(
                {
                    "observation": observation,
                    "decision": decision.evidence(),
                    "policy_digest": policy.digest,
                    "quant_exit": quant,
                    "quant_exit_error": quant_failure,
                    **(
                        {
                            "research_snapshot_digest": payload[
                                "research_snapshot_digest"
                            ]
                        }
                        if "research_snapshot_digest" in payload
                        else {}
                    ),
                }
            )
        )
        key, state = self._save(episode, payload, "observe:" + identity)
        if decision.action in {"WAIT", "WAIT_NATIVE_STOP"}:
            if quant_failure:
                return {
                    "status": "BLOCKED",
                    "reason": "QUANT_EXIT_DATA_UNAVAILABLE",
                    "state_id": key.identity,
                }
            return {
                "status": decision.action,
                "reason": decision.reason,
                "state_id": key.identity,
            }
        if not self.allow_writes:
            return {
                "status": "ACTION_DISABLED",
                "reason": decision.reason,
                "state_id": key.identity,
            }
        return self._dispatch(
            episode,
            remaining,
            decision,
            observation,
            payload,
            key,
            state.version,
            spec,
            quantity_step,
        )

    def _dispatch(
        self,
        episode,
        remaining,
        decision,
        observation,
        payload,
        key,
        version,
        spec,
        quantity_step,
    ):
        with session_advisory_lock(
            self.connect, "testnet-protocol-account:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BLOCKED", "reason": "ACCOUNT_BUSY"}
            from v2_core.runtime_policy import PolicyStore

            policy = PolicyStore(self.connect, self.scope).read()
            if (policy.version, policy.digest) != (
                payload["policy_version"],
                payload["policy_digest"],
            ):
                return {"status": "BLOCKED", "reason": "EXIT_POLICY_CHANGED"}
            isolated = policy.values["exit.symbol_isolation_enabled"]
            max_age = policy.values["exit.preflight_max_age_ms"]
            trace, *_, pending = self._trace(episode)
            if pending or _ledger_digest(trace) != payload["target_ledger_digest"]:
                return {"status": "BLOCKED", "reason": "EXIT_LEDGER_CHANGED"}
            before = self.audit.facts()
            inventory = self.audit.inventory.collect(str(uuid4()))
            after = self.audit.facts()
            blockers = coverage_from_facts(
                self.scope,
                after,
                inventory,
                excluded_position_symbols=self.audit.excluded_position_symbols,
            )
            reduction = None
            if isolated:
                reduction = symbol_reduction_proof(
                    self.scope,
                    after,
                    inventory,
                    episode=episode,
                    symbol=trace["request"]["symbol"],
                    side=trace["request"]["side"],
                    remaining=str(remaining),
                    now_ms=self.clock(),
                    max_age_ms=max_age,
                    excluded_position_symbols=self.audit.excluded_position_symbols,
                )
                blockers = reduction["blockers"]
            if before != after:
                blockers = sorted(set(blockers) | {"LOCAL_FACTS_CHANGED_DURING_EXIT"})
            if blockers or self.protection.query(spec)["status"] != "NEW":
                return {
                    "status": "BLOCKED",
                    "reason": "EXIT_PREFLIGHT_CHANGED",
                    "blockers": blockers,
                }
            if (
                not self._fresh_exit(observation, inventory, max_age)
                or self.audit.facts() != after
                or _ledger_digest(self._trace(episode)[0])
                != payload["target_ledger_digest"]
            ):
                return {"status": "BLOCKED", "reason": "EXIT_PREFLIGHT_CHANGED"}
            persist_inventory(self.connect, scope=self.scope, observation=inventory)
            step = amount(quantity_step, positive=True)
            with money_context():
                quantity = remaining
                if decision.action == "PARTIAL":
                    quantity = (
                        remaining
                        * amount(decision.quantity_fraction, positive=True)
                        / step
                    ).to_integral_value(rounding=ROUND_FLOOR) * step
                if (
                    quantity <= 0
                    or quantity > remaining
                    or quantity % step
                    or (decision.action == "PARTIAL" and quantity == remaining)
                ):
                    raise ValueError("INVALID_EXIT_QUANTITY")
            proof = {
                **payload,
                "preflight_inventory": inventory["observation_id"],
                "preflight_digest": digest(canonical(inventory)),
                "exit_state_id": key.identity,
                "exit_state_version": version,
                "execution_authorized": True,
                "symbol_reduction": reduction,
            }
            key, state = self._save(
                episode, proof, "preflight:" + inventory["observation_id"]
            )
            halt_opening(self.connect, episode, scope=self.scope)
            request_key = (
                "exit:partial-take-profit"
                if decision.action == "PARTIAL"
                else "exit:full:" + decision.reason.lower().replace("_", "-")
            )
            # Retry only terminal rejects/cancels, with a fresh market decision
            # and inventory. Never change the identity of an ambiguous request.
            previous = [
                row
                for row in self.data.trace(episode)["orders"]
                if row["leg"] == "CLOSE"
                and (
                    row["request_key"] == request_key
                    or row["request_key"].startswith(request_key + ":retry:")
                )
            ]
            if previous:
                if any(
                    row["status"] not in {"REJECTED", "CANCELLED"} for row in previous
                ):
                    return {
                        "status": "BLOCKED",
                        "reason": "EXIT_IDENTITY_REQUIRES_RECOVERY",
                    }
                if observation["observed_at_ms"] <= max(
                    row["request_evidence"].get("observed_at_ms", 0) for row in previous
                ):
                    return {
                        "status": "BLOCKED",
                        "reason": "REJECTED_EXIT_AWAITING_FRESH_OBSERVATION",
                    }
                request_key += ":retry:" + str(len(previous))
            if not self._fresh_exit(observation, inventory, max_age):
                return {"status": "BLOCKED", "reason": "EXIT_PREFLIGHT_EXPIRED"}
            order_id, client_id = self.data.orders.prepare(
                episode,
                leg="CLOSE",
                quantity=str(quantity),
                request_key=request_key,
                evidence={
                    "source": payload.get("source", "directional-exit-v1"),
                    **(
                        {"strategy_plan": payload["strategy_plan"]}
                        if "strategy_plan" in payload
                        else {}
                    ),
                    "observed_at_ms": observation["observed_at_ms"],
                    "reason": decision.reason,
                    "state_id": key.identity,
                    "state_version": state.version,
                    "preflight_inventory": inventory["observation_id"],
                    "symbol_reduction": reduction,
                },
            )
            outcome = self.runtime.execution.dispatch(
                order_id,
                **(
                    {
                        "reduce_only_check": lambda order: self._revalidate_reduction(
                            order,
                            episode=episode,
                            spec=spec,
                            facts=after,
                            inventory=inventory,
                            observation=observation,
                            policy=policy,
                            quantity=quantity,
                        )
                    }
                    if isolated
                    else {}
                ),
            )
            return {
                "status": "DISPATCHED"
                if outcome not in {"DENIED", "REJECTED", "CANCELLED"}
                else "BLOCKED",
                "reason": decision.reason,
                "order_id": order_id,
                "client_order_id": client_id,
                "quantity": str(quantity),
                "outcome": outcome,
                "state_id": key.identity,
                "symbol_reduction": reduction,
            }

    def _fresh_exit(self, observation, inventory, max_age):
        now = self.clock()
        return type(now) is int and all(
            type(value) is int and 0 <= now - value < max_age
            for value in (
                observation["observed_at_ms"],
                inventory["started_at_ms"],
                inventory["finished_at_ms"],
            )
        )

    def _revalidate_reduction(
        self, order, *, episode, spec, facts, inventory, observation, policy, quantity
    ):
        """Last pre-submit check; original account/token guard still owns POST.

        Only our two expected journal events may differ from the locked
        preflight: ORDER_PREPARED and its durable SUBMITTING transition. A fill,
        competing order, protection change or unrelated account mutation fails
        closed before the adapter receives permission to submit.
        """
        from v2_core.runtime_policy import PolicyStore

        with self.connect() as conn:
            owner = conn.execute(
                "SELECT episode_id::text FROM v2_orders WHERE order_id=%s",
                (order["order_id"],),
            ).fetchone()
        current = self.runtime.execution.snapshot(order["order_id"])
        if (
            owner != (episode,)
            or current["symbol"] != spec.symbol
            or current["side"] != spec.side
            or current["status"] != "SUBMITTING"
            or current["version"] != 2
            or current["leg"] != "CLOSE"
            or current["reduce_only"] is not True
            or amount(current["quantity"], positive=True) != quantity
            or current["exchange_order_id"] is not None
            or self.protection.query(spec)["status"] != "NEW"
        ):
            return False
        actual = self.audit.facts()
        registered = [o for o in actual["orders"] if o["order_id"] == order["order_id"]]
        if len(registered) != 1 or registered[0]["has_fills"]:
            return False
        normalized = deepcopy(actual)
        normalized["orders"] = [
            o for o in normalized["orders"] if o["order_id"] != order["order_id"]
        ]
        matching = [e for e in normalized["episodes"] if e["episode"] == episode]
        if len(matching) != 1 or type(matching[0]["revision"]) is not int:
            return False
        matching[0]["revision"] -= 2
        if normalized != facts:
            return False
        selected = PolicyStore(self.connect, self.scope).read()
        return (
            (selected.version, selected.digest) == (policy.version, policy.digest)
            and selected.values["exit.symbol_isolation_enabled"]
            and self._fresh_exit(
                observation, inventory, selected.values["exit.preflight_max_age_ms"]
            )
        )

    def run_once(self):
        return self._run_once()

    def run_after_protection(self, coverage):
        """Accept only the pipeline's immediately preceding diagnostic proof."""
        return self._run_once(protection_coverage=coverage)

    def _run_once(self, *, protection_coverage=None):
        with session_advisory_lock(
            self.connect, "directional-exit-stage:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BLOCKED", "reason": "BUSY"}
            cursor = self._read_scan_cursor()
            after = ""
            invalid_cursor = False
            if cursor is not None and not cursor.deleted:
                try:
                    payload = json.loads(cursor.payload_json)
                    if (
                        not isinstance(payload, dict)
                        or set(payload) != {"after"}
                        or not isinstance(payload["after"], str)
                        or str(UUID(payload["after"])) != payload["after"]
                    ):
                        invalid_cursor = True
                    else:
                        after = payload["after"]
                except (ValueError, TypeError):
                    invalid_cursor = True
                # A paging cursor is not exposure or execution authority.
                # Malformed cursor content restarts the bounded scan, retaining
                # all real account/target checks; PG read failures above still
                # propagate. Replace only after a successfully attempted slice.
            with self.connect() as conn:
                from v2_core.strategy_lifecycle import LIFECYCLE_EPISODE_SQL

                rows = conn.execute(
                    f"""SELECT i.intent_id::text FROM v2_trade_intents i
                    JOIN v2_episodes e ON e.episode_id=i.intent_id
                    JOIN v2_orders o ON o.episode_id=i.intent_id
                    LEFT JOIN v2_fills f USING(order_id)
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND {LIFECYCLE_EPISODE_SQL}
                    AND e.status='ACTIVE' GROUP BY i.intent_id
                    HAVING COALESCE(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0)>0
                    ORDER BY (i.intent_id::text<=%s),i.intent_id LIMIT %s""",
                    (*asdict(self.scope).values(), after, self.limit + 1),
                ).fetchall()
            incomplete = len(rows) > self.limit
            scan = {
                "limit": self.limit,
                "selected": min(len(rows), self.limit),
                "active_count_lower_bound": len(rows),
                "remaining_count_lower_bound": max(0, len(rows) - self.limit),
                "count_is_lower_bound": incomplete,
                "complete": not incomplete,
                "cursor_advanced": False,
                "cursor_invalid": invalid_cursor,
                "cursor_rebuilt": False,
            }
            rows = rows[: self.limit]
            if not rows:
                return {
                    "status": "CLEAR",
                    "exits": {},
                    "scan": scan,
                    "execution_authorized": False,
                }

            def finish_scan(result):
                # Checkpoint only after every selected episode received its
                # recovery/evaluation attempt. A crash before this CAS replays
                # the same bounded slice using the original order identities;
                # it never skips an unattempted exit or grants an order permit.
                # Lexical wrap remains valid if the previous cursor episode
                # has since settled or disappeared from this active inventory.
                if after != rows[-1][0] or invalid_cursor:
                    version = cursor.version if cursor else 0
                    saved = self.store.change(
                        self.cursor,
                        expected_version=version,
                        request_key=f"scan:{version + 1}",
                        payload={"after": rows[-1][0]},
                        reason="DIRECTIONAL_EXIT_SCAN",
                    )
                    if saved.code != "APPLIED":
                        raise ValueError("EXIT_SCAN_CURSOR_CONFLICT")
                    scan["cursor_advanced"] = True
                    scan["cursor_rebuilt"] = invalid_cursor
                if incomplete:
                    result["status"] = "BLOCKED"
                    result.setdefault("reason", "EXIT_SCAN_INCOMPLETE")
                return {**result, "scan": scan}

            from v2_core.runtime_policy import PolicyStore

            isolated = (
                PolicyStore(self.connect, self.scope)
                .read()
                .values["exit.symbol_isolation_enabled"]
            )
            # Coverage intentionally rejects nonterminal local orders. Recover
            # already-registered close identities first; this path is GET-only
            # and can never authorize a replacement POST.
            recovering, held = {}, {}
            for (episode,) in rows:
                try:
                    *_, pending = self._trace(episode)
                    outcomes = {}
                    for order in pending:
                        outcome = self.runtime.execution.recover(order["order_id"])
                        recovering[order["order_id"]] = outcome
                        outcomes[order["order_id"]] = outcome
                    if any(outcome not in _TERMINAL for outcome in outcomes.values()):
                        held[episode] = {
                            "status": "RECOVERING_CLOSE",
                            "orders": outcomes,
                        }
                except Exception as exc:  # noqa: BLE001 - poison episode blocks admission
                    recovering[episode] = {
                        "status": "BLOCKED",
                        "error_code": diagnostic_code(exc),
                    }
                    held[episode] = recovering[episode]
            recovery_pending = bool(held)
            if recovery_pending and (not isolated or len(held) == len(rows)):
                return finish_scan(
                    {
                        "status": "BLOCKED",
                        "reason": "CLOSE_RECOVERY_PENDING",
                        "recovery": recovering,
                        **(
                            {
                                "exits": held,
                                "symbol_isolation_enabled": True,
                                "coverage_skipped": "NO_INDEPENDENT_EXIT_CANDIDATE",
                            }
                            if isolated
                            else {}
                        ),
                        "execution_authorized": False,
                    }
                )
            # A recovery attempt (including poison/terminal recovery) can
            # mutate the ledger. Never reuse its pre-recovery diagnostic.
            coverage = (
                self.audit.reuse_after_protection(protection_coverage)
                if not recovering and protection_coverage is not None
                else None
            )
            coverage_reused = coverage is not None
            if coverage is None:
                coverage = self.audit.run_once()
            emergency = {}
            if coverage.get("status") != "ACCOUNT_COVERAGE_CLEAR":
                # A missing stop must prevent new exposure, not prevent a
                # verified reduce-only escape. Every other coverage defect
                # still fails closed; each reduction rechecks fresh inventory.
                if not recovery_pending and set(coverage.get("blockers", [])) == {
                    "MISSING_CONFIRMED_STOP"
                }:
                    for (episode,) in rows:
                        try:
                            emergency[episode] = self._unprotected_exit(episode)
                        except Exception as exc:  # noqa: BLE001 - no secret-bearing exception text
                            emergency[episode] = {
                                "status": "BLOCKED",
                                "error_code": diagnostic_code(exc),
                            }
                if not (
                    isolated
                    and coverage.get("status") == "ACCOUNT_COVERAGE_BLOCKED"
                    and set(coverage.get("blockers", []))
                    and set(coverage["blockers"]) <= _ISOLATABLE_COVERAGE
                ):
                    return finish_scan(
                        {
                            "status": "BLOCKED",
                            "coverage": coverage,
                            "emergency_exits": emergency,
                            "recovery": recovering,
                            "exits": held,
                            **(
                                {"reason": "CLOSE_RECOVERY_PENDING"}
                                if recovery_pending
                                else {}
                            ),
                            "execution_authorized": False,
                        }
                    )
                # This only permits independent evaluation, not an order. The
                # account remains BLOCKED. Each actual POST needs a fresh,
                # symbol-specific proof under the original account lock.
                for episode, result in emergency.items():
                    if result.get("status") != "PROTECTION_PRESENT":
                        held[episode] = result
            results = dict(held)
            for (episode,) in rows:
                if episode in held:
                    continue
                try:
                    results[episode] = self.evaluate(episode)
                except Exception as exc:  # noqa: BLE001 - isolate and sanitize one episode
                    results[episode] = {
                        "status": "BLOCKED",
                        "error_code": diagnostic_code(exc),
                    }
            blocked = (
                recovery_pending
                or coverage.get("status") != "ACCOUNT_COVERAGE_CLEAR"
                or any(
                    result.get("status") not in {"WAIT", "WAIT_NATIVE_STOP"}
                    for result in results.values()
                )
            )
            return finish_scan(
                {
                    "status": "BLOCKED" if blocked else "CLEAR",
                    "exits": results,
                    "coverage": coverage,
                    "recovery": recovering,
                    "emergency_exits": emergency,
                    "coverage_reused": coverage_reused,
                    **(
                        {"reason": "CLOSE_RECOVERY_PENDING"} if recovery_pending else {}
                    ),
                    "symbol_isolation_enabled": isolated,
                    "execution_authorized": False,
                }
            )

    def _unprotected_exit(self, episode):
        """Close a proven-owned naked residual without strategy evaluation.

        An UNKNOWN stop is never resubmitted. Native stops that may exist are
        left untouched. The account lock, exact ledger/venue comparison and
        reduceOnly wire flag prevent new/reverse exposure. An ambiguous close
        keeps its original identity and is recovered by the normal runner.
        """
        from v2_core.runtime_policy import PolicyStore

        policy = PolicyStore(self.connect, self.scope).read()
        if (
            not self.allow_writes
            or not policy.values["protection.emergency_exit_enabled"]
        ):
            return {"status": "ACTION_DISABLED", "reason": "UNCONFIRMED_PROTECTION"}
        with session_advisory_lock(
            self.connect, "testnet-protocol-account:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BLOCKED", "reason": "ACCOUNT_BUSY"}
            trace, opening, plan, _, _, remaining, _, first, _, pending = self._trace(
                episode
            )
            if pending:
                return {"status": "BLOCKED", "reason": "CLOSE_RECOVERY_PENDING"}
            now = self.clock()
            if not 0 <= now - first:
                raise ValueError("INVALID_EXPOSURE_TIME")
            if now - first < policy.values["protection.unconfirmed_grace_ms"]:
                return {"status": "BLOCKED", "reason": "PROTECTION_CONFIRMATION_GRACE"}
            before = self.audit.facts()
            inventory = self.audit.inventory.collect(str(uuid4()))
            after = self.audit.facts()
            blockers = coverage_from_facts(
                self.scope,
                after,
                inventory,
                excluded_position_symbols=self.audit.excluded_position_symbols,
            )
            if before != after or set(blockers) != {"MISSING_CONFIRMED_STOP"}:
                return {
                    "status": "BLOCKED",
                    "reason": "EMERGENCY_PREFLIGHT_CHANGED",
                    "blockers": blockers,
                }
            symbol = trace["request"]["symbol"]
            # Do not close another episode that is properly protected merely
            # because the account has a different unprotected symbol.
            if any(
                r.get("symbol") == symbol and r.get("orderType") == "STOP_MARKET"
                for r in inventory["responses"]["conditional_orders"]
            ):
                return {"status": "PROTECTION_PRESENT"}
            owners = [
                e
                for e in after["episodes"]
                if e["symbol"] == symbol and amount(e["remaining"]) != 0
            ]
            if (
                len(owners) != 1
                or owners[0]["episode"] != episode
                or amount(owners[0]["remaining"]) != remaining
            ):
                raise ValueError("EMERGENCY_EXCLUSIVE_OWNERSHIP_REQUIRED")
            finish = inventory["finished_at_ms"]
            checked = self.clock()
            if type(finish) is not int or not 0 <= checked - finish < 10000:
                raise ValueError("STALE_EMERGENCY_INVENTORY")
            step = amount(
                plan.quantity_step
                if isinstance(plan, TradePlan)
                else trace["decision"]["features"]["context"]["directional"]["sizing"][
                    "quantity_step"
                ],
                positive=True,
            )
            with money_context():
                if remaining <= 0 or remaining % step:
                    raise ValueError("INVALID_EMERGENCY_QUANTITY")
            previous = [
                o
                for o in trace["orders"]
                if o["leg"] == "CLOSE"
                and o["request_key"].startswith("exit:unprotected-emergency")
            ]
            if any(o["status"] not in {"REJECTED", "CANCELLED"} for o in previous):
                return {
                    "status": "BLOCKED",
                    "reason": "EMERGENCY_IDENTITY_REQUIRES_RECOVERY",
                }
            if previous and finish <= max(
                o["request_evidence"].get("observed_at_ms", 0) for o in previous
            ):
                return {
                    "status": "BLOCKED",
                    "reason": "REJECTED_EXIT_AWAITING_FRESH_OBSERVATION",
                }
            persist_inventory(self.connect, scope=self.scope, observation=inventory)
            halt_opening(self.connect, episode, scope=self.scope)
            request_key = "exit:unprotected-emergency" + (
                ":retry:" + str(len(previous)) if previous else ""
            )
            oid, cid = self.data.orders.prepare(
                episode,
                leg="CLOSE",
                quantity=str(remaining),
                request_key=request_key,
                evidence={
                    "source": "unprotected-emergency-v1",
                    "reason": "UNCONFIRMED_PROTECTION",
                    "observed_at_ms": finish,
                    "opening_order_id": opening["order_id"],
                    "preflight_inventory": inventory["observation_id"],
                    "preflight_digest": digest(canonical(inventory)),
                    "policy_version": policy.version,
                    "policy_digest": policy.digest,
                },
            )
            outcome = self.runtime.execution.dispatch(oid)
            return {
                "status": "DISPATCHED"
                if outcome not in {"DENIED", "REJECTED", "CANCELLED"}
                else "BLOCKED",
                "reason": "UNCONFIRMED_PROTECTION",
                "order_id": oid,
                "client_order_id": cid,
                "quantity": str(remaining),
                "outcome": outcome,
            }
