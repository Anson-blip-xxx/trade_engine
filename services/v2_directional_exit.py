"""Durable S6/S8 Testnet exit supervision and reduce-only dispatch.

Every decision is derived from immutable entry evidence, reconciled fills and a
fresh injected market observation.  The native close-all stop remains installed
during active exits; Binance reduce-only semantics prevent either path from
opening reverse exposure.  Writes are disabled unless explicitly enabled.
"""

import json
from dataclasses import asdict
from decimal import ROUND_FLOOR, Decimal, localcontext
from uuid import UUID, uuid4

from services.v2_directional_protection import DirectionalStopRecovery
from v2_core.account_coverage import AccountCoverageAudit, coverage_from_facts
from v2_core.account_inventory import persist_inventory
from v2_core.directional_exit import ExitFacts, evaluate_directional_exit
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.opening_halt import halt_opening
from v2_core.protection import TestnetProtection
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey

_TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}


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
        self.store = BusinessState(connect)
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
        if (
            trace is None
            or any(trace["scope"].get(k) != v for k, v in asdict(self.scope).items())
            or trace["scope"]["producer"] not in {"s6", "s8"}
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
        quantities, value, opened, closed, first = (
            {},
            Decimal(0),
            Decimal(0),
            Decimal(0),
            None,
        )
        with localcontext() as ctx:
            ctx.prec = 100
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
            planned = amount(sizing["planned_loss"], positive=True) * remaining / opened
        pending = [
            row
            for row in trace["orders"]
            if row["leg"] == "CLOSE" and row["status"] not in _TERMINAL
        ]
        partial_done = any(
            row["leg"] == "CLOSE"
            and row["request_key"] == "exit:partial-take-profit"
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

    def _observation(self, symbol):
        raw = self.observe(symbol)
        now = self.clock()
        if (
            not isinstance(raw, dict)
            or raw.get("symbol") != symbol
            or raw.get("environment") != "SANDBOX"
            or type(raw.get("observed_at_ms")) is not int
            or type(now) is not int
            or not 0 <= now - raw["observed_at_ms"] < 10000
            or not isinstance(raw.get("momentum_closes_15m"), (list, tuple))
            or len(raw["momentum_closes_15m"]) != 4
        ):
            raise ValueError("STALE_OR_INVALID_EXIT_OBSERVATION")
        for field in (
            "mark_price",
            "funding_rate",
            "ema9_1h",
            "ema20_1h",
            "exit_fee_rate",
        ):
            if field not in raw:
                raise ValueError("INCOMPLETE_EXIT_OBSERVATION")
        return {**raw, "momentum_closes_15m": list(raw["momentum_closes_15m"])}, now

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
        protection = self.protection.query(spec)
        if protection["status"] != "NEW":
            return {
                "status": "WAITING_NATIVE_PROTECTION",
                "parent_status": protection["status"],
            }
        observation, now = self._observation(trace["request"]["symbol"])
        saved = self.store.read(self._key(episode))
        prior = json.loads(saved.payload_json) if saved else {}
        facts = ExitFacts(
            system_tag=plan["system_tag"],
            side=trace["request"]["side"],
            entry_price=str(entry),
            mark_price=observation["mark_price"],
            stop_price=sizing["stop_price"],
            remaining_quantity=str(remaining),
            planned_loss=str(planned),
            held_ms=now - first,
            funding_rate=observation["funding_rate"],
            ema9_1h=observation["ema9_1h"],
            ema20_1h=observation["ema20_1h"],
            momentum_closes_15m=tuple(observation["momentum_closes_15m"]),
            peak_return_pct=prior.get("peak_return_pct", "0"),
            partial_done=partial_done,
            exit_fee_rate=observation["exit_fee_rate"],
        )
        from v2_core.runtime_policy import PolicyStore

        policy = PolicyStore(self.connect, self.scope).read()
        decision = evaluate_directional_exit(facts, policy=policy.values)
        quantity_step = trace["decision"]["features"]["context"]["directional"][
            "sizing"
        ]["quantity_step"]
        amount(quantity_step, positive=True)
        encoded_facts = asdict(facts)
        encoded_facts["momentum_closes_15m"] = list(facts.momentum_closes_15m)
        encoded_facts["peak_return_pct"] = decision.peak_return_pct
        payload = {
            "episode": episode,
            "account_scope": asdict(self.scope),
            "strategy_version": "directional-admission-v2-1",
            "entry_decision_digest": digest(canonical(trace["decision"])),
            "opening_order_id": opening["order_id"],
            "facts": encoded_facts,
            "observation": observation,
            "decision": decision.evidence(),
            "policy_version": policy.version,
            "policy_digest": policy.digest,
            "policy": policy.values,
            "peak_return_pct": decision.peak_return_pct,
            "partial_done": partial_done,
            "execution_authorized": False,
        }
        identity = digest(
            canonical(
                {
                    "observation": observation,
                    "decision": decision.evidence(),
                    "policy_digest": policy.digest,
                }
            )
        )
        key, state = self._save(episode, payload, "observe:" + identity)
        if decision.action in {"WAIT", "WAIT_NATIVE_STOP"}:
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
        with self.connect() as account:
            if not account.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + self.scope.key,),
            ).fetchone()[0]:
                return {"status": "BLOCKED", "reason": "ACCOUNT_BUSY"}
            account.commit()
            before = self.audit.facts()
            inventory = self.audit.inventory.collect(str(uuid4()))
            after = self.audit.facts()
            blockers = coverage_from_facts(self.scope, after, inventory)
            if before != after:
                blockers = sorted(set(blockers) | {"LOCAL_FACTS_CHANGED_DURING_EXIT"})
            if blockers or self.protection.query(spec)["status"] != "NEW":
                return {
                    "status": "BLOCKED",
                    "reason": "EXIT_PREFLIGHT_CHANGED",
                    "blockers": blockers,
                }
            persist_inventory(self.connect, scope=self.scope, observation=inventory)
            step = amount(quantity_step, positive=True)
            with localcontext() as ctx:
                ctx.prec = 100
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
            order_id, client_id = self.data.orders.prepare(
                episode,
                leg="CLOSE",
                quantity=str(quantity),
                request_key=request_key,
                evidence={
                    "source": "directional-exit-v1",
                    "reason": decision.reason,
                    "state_id": key.identity,
                    "state_version": state.version,
                    "preflight_inventory": inventory["observation_id"],
                },
            )
            outcome = self.runtime.execution.dispatch(order_id)
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
            }

    def run_once(self):
        with self.connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("directional-exit-stage:" + self.scope.key,),
            ).fetchone()[0]:
                return {"status": "BLOCKED", "reason": "BUSY"}
            guard.commit()
            with self.connect() as conn:
                rows = conn.execute(
                    """SELECT i.intent_id::text FROM v2_trade_intents i
                    JOIN v2_episodes e ON e.episode_id=i.intent_id
                    JOIN v2_orders o ON o.episode_id=i.intent_id
                    LEFT JOIN v2_fills f USING(order_id)
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1'
                    AND e.status='ACTIVE' GROUP BY i.intent_id
                    HAVING COALESCE(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0)>0
                    ORDER BY i.intent_id LIMIT %s""",
                    (*asdict(self.scope).values(), self.limit + 1),
                ).fetchall()
            if len(rows) > self.limit:
                return {"status": "BLOCKED", "reason": "EXIT_SCAN_CAPACITY_EXCEEDED"}
            if not rows:
                return {"status": "CLEAR", "exits": {}, "execution_authorized": False}
            # Coverage intentionally rejects nonterminal local orders. Recover
            # already-registered close identities first; this path is GET-only
            # and can never authorize a replacement POST.
            recovering = {}
            for (episode,) in rows:
                try:
                    *_, pending = self._trace(episode)
                    for order in pending:
                        recovering[order["order_id"]] = self.runtime.execution.recover(
                            order["order_id"]
                        )
                except Exception as exc:  # noqa: BLE001 - poison episode blocks admission
                    recovering[episode] = {
                        "status": "BLOCKED",
                        "error_code": type(exc).__name__,
                    }
            if any(
                value not in _TERMINAL
                for value in recovering.values()
                if isinstance(value, str)
            ) or any(isinstance(value, dict) for value in recovering.values()):
                return {
                    "status": "BLOCKED",
                    "reason": "CLOSE_RECOVERY_PENDING",
                    "recovery": recovering,
                    "execution_authorized": False,
                }
            coverage = self.audit.run_once()
            if coverage.get("status") != "ACCOUNT_COVERAGE_CLEAR":
                return {
                    "status": "BLOCKED",
                    "coverage": coverage,
                    "execution_authorized": False,
                }
            results = {}
            for (episode,) in rows:
                try:
                    results[episode] = self.evaluate(episode)
                except Exception as exc:  # noqa: BLE001 - isolate and sanitize one episode
                    results[episode] = {
                        "status": "BLOCKED",
                        "error_code": type(exc).__name__,
                    }
            blocked = any(
                result.get("status") not in {"WAIT", "WAIT_NATIVE_STOP"}
                for result in results.values()
            )
            return {
                "status": "BLOCKED" if blocked else "CLEAR",
                "exits": results,
                "coverage": coverage,
                "execution_authorized": False,
            }
