"""One account-bound lifecycle supervisor; default no new order dispatch.

Explicit PM/exit/settlement/T60 stages are mandatory. They must return CLEAR only
after their own evidence-based checks, never merely because a scan did not crash.
This composition is not a substitute for implementing or deploying those stages.
"""

import json
import time
from contextlib import contextmanager
from dataclasses import asdict
from uuid import UUID, uuid4

from services.v2_settlement_entry import settled_result_valid, settlement_result_valid
from v2_core.evidence import canonical
from v2_core.runtime_policy import PolicyStore, resolve
from v2_core.scheduling import candidate_wait_reason, drain_requested
from v2_core.state import BusinessState, StateKey
from v2_core.venue_readiness import GuardedOpeningSubmit


def failed(value):
    if isinstance(value, dict):
        return bool(set(value) & {"error", "error_code"}) or any(
            failed(v) for v in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(failed(v) for v in value)
    return (
        value in {"UNKNOWN", "UNAVAILABLE", "RETRY", "BLOCKED"}
        if isinstance(value, str)
        else False
    )


def _reclaim_failures_are_expected(diagnostic, results):
    """Permit more queue work, never promote a waiting candidate to an order.

    Scheduling retains real exceptions in ``failures`` even for established
    candidate-local waits. Require its matching, non-authorizing wait proof
    and exact deferred result; unknown/ACK/dependency failures stay ineligible.
    No lease, retry, risk, receipt or admission disposition is changed here.
    """
    failures = diagnostic.get("failures", [])
    if not isinstance(failures, list):
        return False
    if not failures:
        return True
    waits = diagnostic.get("candidate_waits")
    if not isinstance(waits, list):
        return False
    for failure in failures:
        if not isinstance(failure, dict):
            return False
        identifier, code, stage = (
            failure.get("signal_id"),
            failure.get("error_code"),
            failure.get("stage"),
        )
        if (
            not isinstance(identifier, str)
            or not isinstance(code, str)
            or not isinstance(stage, str)
            or stage not in {"CONTEXT", "CONSUME"}
            or type(failure.get("attempt")) is not int
            or failure["attempt"] < 1
            or failure.get("outcome") != "DEFERRED"
            or results.get(identifier) != "DEFERRED"
        ):
            return False
        try:
            if str(UUID(identifier)) != identifier:
                return False
        except ValueError:
            return False
        reason = candidate_wait_reason(ValueError(code), stage=stage)
        if reason is None:
            return False
        matching = [
            wait
            for wait in waits
            if isinstance(wait, dict)
            and wait.get("signal_id") == identifier
            and wait.get("error_code") == code
        ]
        if len(matching) != 1:
            return False
        wait = matching[0]
        if (
            wait.get("scope") != "CANDIDATE"
            or wait.get("execution_authorized") is not False
            or wait.get("reason") != reason
            or not isinstance(wait.get("symbol"), str)
            or not wait["symbol"]
            or type(wait.get("retry_seconds")) is not int
            or not 1 <= wait["retry_seconds"] <= 3600
            or type(wait.get("signal_expires_at_ms")) is not int
            or wait["signal_expires_at_ms"] <= 0
        ):
            return False
    return True


def _stage_clear(name, stage, result):
    from services.v2_directional_settlement import DirectionalSettlementStage

    if name == "settlement" and isinstance(stage, DirectionalSettlementStage):
        # Only this concrete stage separates committed cash from derived
        # analysis. No other stage or arbitrary nested error is exempted.
        return settlement_result_valid(result, status="CLEAR") and all(
            settled_result_valid(episode, record)
            for episode, record in result["settlements"].items()
        )
    return (
        isinstance(result, dict)
        and result.get("status") == "CLEAR"
        and not failed(result)
    )


class TradingPipeline:
    def __init__(
        self,
        *,
        runtime,
        market,
        regime,
        schedulers,
        protection,
        exits,
        settlement,
        followups,
        enable_entries=False,
    ):
        scope = runtime.scope
        if (
            scope is None
            or scope.environment != "SANDBOX"
            or type(enable_entries) is not bool
        ):
            raise ValueError("explicit testnet pipeline required")
        if runtime.execution.scope != scope or not isinstance(
            runtime.execution.submit, GuardedOpeningSubmit
        ):
            raise ValueError("venue-guarded runtime required")
        schedulers = tuple(schedulers)
        bindings = [(s.worker.scope.producer, s.worker.source) for s in schedulers]
        required = {("s6", "s3"), ("s8", "s3")}
        allowed = {
            (direction, source)
            for direction in ("s6", "s8")
            for source in ("s3", "tv_bridge", "quant")
        }
        if (
            len(bindings) != len(set(bindings))
            or not required <= set(bindings) <= allowed
            or any(
                (("s6", source) in bindings) != (("s8", source) in bindings)
                for source in ("tv_bridge", "quant")
            )
        ):
            raise ValueError("both formal directional consumers required")
        for scheduler in schedulers:
            if (
                scheduler.worker.runtime is not runtime
                or scheduler.worker.source not in {"s3", "tv_bridge", "quant"}
                or scheduler.worker.strategy_version != "directional-admission-v2-1"
            ):
                raise ValueError("single runtime and actual directional rules required")
        if (
            market.source.publisher.environment != scope.environment
            or getattr(regime, "environment", None) != scope.environment
            or not callable(getattr(regime, "run_once", None))
        ):
            raise ValueError("market environment mismatch")
        for stage in (protection, exits, settlement, followups):
            if getattr(stage, "scope", None) != scope or not callable(
                getattr(stage, "run_once", None)
            ):
                raise ValueError(
                    "bound protection, exit, settlement and T60 stages required"
                )
        self.runtime, self.scope, self.market, self.regime, self.schedulers = (
            runtime,
            scope,
            market,
            regime,
            schedulers,
        )
        self.protection, self.exits, self.settlement, self.followups = (
            protection,
            exits,
            settlement,
            followups,
        )
        self.enable_entries = enable_entries
        self.background_maintenance = None
        self.connect = runtime.data._connect
        self._progress_cache = {}
        # Process-local fairness, never trading authority. Each priority group
        # advances once when first attempted, including an interrupted pass.
        self._scheduler_heads = {}
        self._demand_backoff_until = 0.0

    def _scheduler_groups(self, tv_first):
        groups = (
            (
                ("tv", [s for s in self.schedulers if s.worker.source == "tv_bridge"]),
                (
                    "other",
                    [s for s in self.schedulers if s.worker.source != "tv_bridge"],
                ),
            )
            if tv_first
            else (("all", list(self.schedulers)),)
        )
        ordered = []
        for group, members in groups:
            if not members:
                continue
            head = self._scheduler_heads.get(group)
            identities = [(s.worker.scope.consumer, s.worker.source) for s in members]
            start = identities.index(head) if head in identities else 0
            members = members[start:] + members[:start]
            ordered.extend((group, member) for member in members)
        return ordered

    def _confirmed_capacity_decline(self, order_id):
        """Only a scoped, durable PREPARED->CANCELLED without a submit permit.

        A bare DENIED, stale evidence, transport failure or post-submit cancel
        is NOT enough to continue admissions. Database failure fails closed.
        """
        with self.connect() as conn:
            row = conn.execute(
                """SELECT e.evidence->>'account_risk_code'
                FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                JOIN v2_order_events e ON e.order_id=o.order_id AND e.version=o.version
                JOIN v2_order_events p ON p.order_id=o.order_id AND p.version=1
                WHERE o.order_id=%s
                AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1'
                AND o.leg='OPEN' AND o.status='CANCELLED' AND o.version=2
                AND o.exchange_order_id IS NULL AND e.status='CANCELLED' AND p.status='PREPARED'
                AND e.evidence->>'reason'='account risk denied'
                AND e.evidence->>'account_risk_code' IN
                    ('CAPITAL_PORTFOLIO_RISK_EXCEEDED','CAPITAL_SINGLE_TRADE_RISK_EXCEEDED')
                AND NOT EXISTS (SELECT 1 FROM v2_fills f WHERE f.order_id=o.order_id)""",
                (order_id, *asdict(self.scope).values()),
            ).fetchone()
        return row[0] if row else None

    @contextmanager
    def _ownership(self):
        """Release synchronously; closing a socket need not finish backend cleanup."""
        with self.connect() as owner:
            acquired = owner.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("v2-pipeline:" + self.scope.key,),
            ).fetchone()[0]
            owner.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    owner.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                        ("v2-pipeline:" + self.scope.key,),
                    )
                    owner.commit()

    def run_once(self, *, limit=10, stop_requested=None):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("bounded pipeline batch required")
        if stop_requested is not None and not callable(stop_requested):
            raise TypeError("callable stop check required")
        if drain_requested(stop_requested):
            return {"status": "ENTRY_BLOCKED", "drain_requested": True}
        with self._ownership() as acquired:
            if not acquired:
                return {"status": "BUSY"}
            identity = str(uuid4())
            key = StateKey(
                **asdict(self.scope),
                namespace="trading-pipeline-cycle-v1",
                key=identity,
            )
            store = BusinessState(self.connect)
            journal = {
                "cycle_id": identity,
                "account_scope": asdict(self.scope),
                "started_at_ms": self.runtime._now(),
                "status": "RUNNING",
                "phases": {},
            }
            version = 0

            def save():
                nonlocal version
                result = store.change(
                    key,
                    expected_version=version,
                    request_key=f"phase:{version + 1}",
                    payload=journal,
                    reason="TRADING_PIPELINE_CYCLE",
                )
                if result.code != "APPLIED":
                    raise ValueError("pipeline journal conflict")
                version += 1

            def phase(name, action):
                journal["phases"][name] = {"status": "STARTED"}
                save()  # If PG is unavailable, no following action is launched.
                started = time.perf_counter()
                try:
                    result = action()
                except Exception as exc:  # noqa: BLE001 - safe persistent stage failure
                    result = {"status": "BLOCKED", "error_code": type(exc).__name__}
                journal.setdefault("phase_elapsed_ms", {})[name] = round(
                    (time.perf_counter() - started) * 1000
                )
                journal["phases"][name] = result
                save()
                return result

            def draining():
                requested = drain_requested(stop_requested)
                if requested:
                    journal["drain_requested"] = True
                    journal["evaluation_stop_reason"] = "STOP_REQUESTED"
                return requested

            def finish_drain():
                journal.update(
                    status="ENTRY_BLOCKED",
                    finished_at_ms=self.runtime._now(),
                    entry_dispatch_enabled=self.enable_entries,
                    entries={},
                )
                save()
                return journal

            save()
            settings = resolve()

            def load_policy():
                nonlocal settings
                selected = PolicyStore(self.connect, self.scope).read()
                settings = selected.values
                if selected.version:
                    for scheduler in self.schedulers:
                        current = json.loads(scheduler.worker.config_json)
                        scheduler.worker.max_delay_ms = settings[
                            "entry.decision_lifetime_ms"
                        ]
                        scheduler.worker.config_json = canonical(
                            {
                                **current,
                                "policy": settings,
                                "policy_version": selected.version,
                                "policy_digest": selected.digest,
                            }
                        )
                return {
                    "status": "CURRENT",
                    "version": selected.version,
                    "digest": selected.digest,
                }

            configuration = phase("configuration", load_policy)
            recovered = phase(
                "recovery",
                lambda: self.runtime.tick(
                    overdue_ms=settings["scheduler.recovery_overdue_ms"], limit=limit
                ),
            )
            safety = []
            protection_coverage = None
            for name, stage in (
                ("protection", self.protection),
                ("exits", self.exits),
                ("settlement", self.settlement),
                ("followups", self.followups),
            ):
                after_protection = getattr(stage, "run_after_protection", None)
                action = stage.run_once
                if name == "exits" and callable(after_protection):
                    action = lambda run=after_protection, proof=protection_coverage: (
                        run(proof)
                    )
                result = phase(name, action)
                if (
                    name == "protection"
                    and isinstance(result, dict)
                    and result.get("status") == "CLEAR"
                    and not failed(result)
                ):
                    protection_coverage = result.get("coverage")
                # T60 followups are post-close research, not position safety.
                # Waiting for a closed candle (or a research fetch failure)
                # must not stop live signal consumption. Keep its result in
                # the cycle journal without weakening the other three gates.
                if name != "followups":
                    stage_clear = _stage_clear(name, stage, result)
                    clearance = getattr(stage, "entry_clearance", None)
                    if (
                        name == "settlement"
                        and not stage_clear
                        and all(safety)
                        and callable(clearance)
                    ):
                        # Accounting lag is not itself an unknown position.
                        # The concrete stage must prove fresh reconciled facts,
                        # terminal fills and retained risk reservations. The
                        # original settlement result remains visible/auditable.
                        entry_proof = phase(
                            "settlement_entry_clearance",
                            lambda check=clearance, pending=result, proof=protection_coverage: (
                                check(pending, proof)
                            ),
                        )
                        stage_clear = (
                            isinstance(entry_proof, dict)
                            and entry_proof.get("status")
                            == "ENTRY_CLEAR_PENDING_INCOME"
                            and entry_proof.get("risk_reservations_retained") is True
                            and entry_proof.get("execution_authorized") is False
                            and not failed(entry_proof)
                        )
                    safety.append(stage_clear)

            # Expiry is bookkeeping, not admission: keep draining stale input
            # even while a safety stage blocks opening new positions.
            def maintain_signals():
                results = {}
                for s in self.schedulers:
                    name = s.worker.scope.producer + ":" + s.worker.source
                    try:
                        if self.background_maintenance is not None:
                            # The housekeeper owns expiry, discovery reconciliation
                            # and historical counts. Never repeat that IO here.
                            results[name] = self.background_maintenance.snapshot(s)
                            journal.setdefault("expiry_metrics", {})[name] = {
                                "status": "DELEGATED",
                                "foreground_query": False,
                                "full_reconciliation": False,
                            }
                            continue
                        results[name] = s.expire_pending(
                            limit=settings["scheduler.expiry_batch"],
                            reconcile_seconds=settings[
                                "scheduler.discovery_reconcile_seconds"
                            ],
                            budget_ms=max(
                                1,
                                min(
                                    5000,
                                    settings["scheduler.evaluation_budget_ms"]
                                    // (8 * len(self.schedulers)),
                                ),
                            ),
                        )
                        journal.setdefault("expiry_metrics", {})[name] = dict(
                            s.last_expiry_metrics
                        )
                    except Exception as exc:  # noqa: BLE001 - diagnostic, not order authority
                        results[name] = {
                            "status": "UNAVAILABLE",
                            "error_code": type(exc).__name__,
                        }
                return results

            if draining():
                return finish_drain()
            phase("signal_maintenance", maintain_signals)
            if draining():
                return finish_drain()
            # Market failure must not prevent recovery/PM/exit/settlement above.
            market = phase("market", self.market.run_once)
            market_ok = (
                isinstance(market, dict)
                and market.get("status") in {"ACKNOWLEDGED", "CURRENT"}
                and not failed(market)
            )
            regime = phase(
                "regime",
                self.regime.run_once
                if market_ok
                else lambda: {"status": "BLOCKED", "error_code": "MARKET_NOT_CURRENT"},
            )
            regime_ok = (
                isinstance(regime, dict)
                and regime.get("status") in {"CLEAR", "PROJECTED"}
                and not failed(regime)
            )
            can_admit = (
                all(safety)
                and not failed(recovered)
                and not failed(configuration)
                and market_ok
                and regime_ok
            )

            def prepared_orders():
                with self.connect() as conn:
                    return conn.execute(
                        """SELECT o.order_id::text FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                        AND i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1'
                        AND o.leg='OPEN' AND o.status='PREPARED' ORDER BY o.updated_at,o.order_id LIMIT %s""",
                        (*asdict(self.scope).values(), limit),
                    ).fetchall()

            entries = {}
            deadline = (
                time.monotonic() + settings["scheduler.evaluation_budget_ms"] / 1000
            )

            def dispatch_prepared():
                nonlocal can_admit
                if draining():
                    can_admit = False
                    return False
                if not self.enable_entries:
                    return True
                for (order_id,) in prepared_orders():
                    if draining():
                        can_admit = False
                        return False
                    result = phase(
                        "dispatch:" + order_id,
                        lambda oid=order_id: (
                            "DRAINED"
                            if draining()
                            else self.runtime.execution.dispatch(oid)
                        ),
                    )
                    entries[order_id] = result
                    if result == "DRAINED":
                        can_admit = False
                        return False
                    capacity = None
                    if (
                        result == "DENIED"
                        and settings["scheduler.continue_after_capacity_denial"]
                    ):
                        try:
                            capacity = self._confirmed_capacity_decline(order_id)
                        except Exception as exc:  # noqa: BLE001 - fail closed with safe diagnostic
                            journal["capacity_check_error"] = type(exc).__name__
                    if capacity:
                        journal.setdefault("capacity_declines", {})[order_id] = capacity
                        # Expected local risk refusal is audited, not a broken
                        # exchange path. Other consumers may have smaller orders.
                        # Bound attempts and wall time; never retry this order.
                        if (
                            len(entries)
                            >= min(limit, settings["scheduler.max_capacity_declines"])
                            or time.monotonic() >= deadline
                        ):
                            journal["admission_stop_reason"] = (
                                "CAPACITY_EVALUATION_BUDGET"
                            )
                            return False
                        continue
                    if result not in ("ACKNOWLEDGED", "FILLED"):
                        can_admit = False
                        journal["phases"]["entry_dispatch"] = {
                            "status": "BLOCKED",
                            "error_code": "OPENING_DISPATCH_UNSUCCESSFUL",
                        }
                    # Any possibly accepted opening stops admissions and retains
                    # immediate reconciliation/protection, including exceptions.
                    if result not in ("DENIED", "REJECTED", "CANCELLED", "PREPARED"):
                        phase(
                            "after_dispatch_recovery",
                            lambda oid=order_id: self.runtime.execution.recover(oid),
                        )
                        protected = phase(
                            "after_dispatch_protection", self.protection.run_once
                        )
                        if (
                            not isinstance(protected, dict)
                            or protected.get("status") != "CLEAR"
                            or failed(protected)
                            or failed(journal["phases"]["after_dispatch_recovery"])
                        ):
                            can_admit = False
                    return False
                return True

            measured = []
            if can_admit and dispatch_prepared():
                plan = self._scheduler_groups(settings["scheduler.tv_first"])
                schedulers = [target for _, target in plan]
                journal["scheduler_order"] = [
                    s.worker.scope.producer + ":" + s.worker.source for s in schedulers
                ]
                scheduling = journal["scheduler_allocation"] = {
                    name: {"status": "NOT_ATTEMPTED"}
                    for name in journal["scheduler_order"]
                }
                advanced_groups = set()
                weights = []
                # Advisory probes target an eighth of the evaluation budget,
                # with per-statement timeouts. Connection/setup can still
                # overrun; backoff prevents repeated starvation, not a hard
                # wall-clock SLA. None of this changes admission authority.
                probe_started = time.monotonic()
                probe_deadline = min(
                    deadline,
                    probe_started + settings["scheduler.evaluation_budget_ms"] / 8000,
                )
                for target in schedulers:
                    demand = None
                    demand_error = None
                    probe_status = "DISABLED"
                    probe_budget = max(
                        1,
                        min(
                            5000,
                            settings["scheduler.evaluation_budget_ms"]
                            // (8 * len(schedulers)),
                        ),
                    )
                    before_probe = time.monotonic()
                    try:
                        if settings["scheduler.backlog_weighted_budget"]:
                            if before_probe < self._demand_backoff_until:
                                probe_status = "BACKOFF"
                            elif before_probe >= probe_deadline:
                                probe_status = "BUDGET_SKIPPED"
                            else:
                                probe_budget = min(
                                    probe_budget,
                                    max(1, int((probe_deadline - before_probe) * 1000)),
                                )
                                demand = target.demand(
                                    budget_ms=probe_budget,
                                    limit=settings["scheduler.max_demand_weight"],
                                )
                                if type(demand) is not int or demand < 0:
                                    raise ValueError("INVALID_DEMAND")
                                probe_status = "OBSERVED_CAPPED"
                    except Exception as exc:  # noqa: BLE001 - advisory only, retain a safe diagnostic
                        demand_error = type(exc).__name__
                        demand = None
                        probe_status = "UNAVAILABLE"
                    probe_elapsed = round((time.monotonic() - before_probe) * 1000)
                    if demand_error or probe_elapsed > probe_budget:
                        # Also covers connection/setup time outside SQL timeout.
                        # One overrun must not recur for every peer/every cycle.
                        self._demand_backoff_until = (
                            time.monotonic()
                            + settings["scheduler.discovery_reconcile_seconds"]
                        )
                    weights.append(
                        max(
                            1, min(demand or 0, settings["scheduler.max_demand_weight"])
                        )
                    )
                    journal.setdefault("scheduler_demand", {})[
                        target.worker.scope.producer + ":" + target.worker.source
                    ] = {
                        "live_actionable": demand,
                        "count_is_capped": True,
                        "status": probe_status,
                        "elapsed_ms": probe_elapsed,
                        "budget_ms": probe_budget,
                        "weight": weights[-1],
                        "error_code": demand_error,
                    }
                # Append at most one reclaim turn behind ALL first turns. The
                # original deadline, priority groups and next-cycle rotation
                # remain authoritative; an empty/failed lane is never polled.
                turns = [
                    (index, target, False) for index, target in enumerate(schedulers)
                ]
                prepared_observed = False
                for turn_index, (index, scheduler, reclaim) in enumerate(turns):
                    if reclaim and prepared_observed:
                        journal["scheduler_reclaim_skipped"] = "PREPARED_OBSERVED"
                        break
                    if draining():
                        can_admit = False
                        break
                    remaining = int((deadline - time.monotonic()) * 1000)
                    if remaining <= 0:
                        journal["evaluation_stop_reason"] = "CYCLE_BUDGET_EXHAUSTED"
                        break

                    # Half equal-share, half demand-weighted. A low-volume TV
                    # source cannot repeatedly spend its whole tiny share on
                    # discovery while the high-volume source always progresses.
                    # Unused time is automatically redistributed to later peers.
                    pending_weights = (
                        [weights[position] for position, _, _ in turns[turn_index:]]
                        if reclaim
                        else weights[index:]
                    )
                    allowance = max(
                        1,
                        (
                            remaining // len(pending_weights)
                            + remaining * weights[index] // sum(pending_weights)
                        )
                        // 2,
                    )
                    group = plan[index][0]
                    if not reclaim and group not in advanced_groups:
                        peers = [target for key, target in plan if key == group]
                        next_worker = peers[1 % len(peers)].worker
                        self._scheduler_heads[group] = (
                            next_worker.scope.consumer,
                            next_worker.source,
                        )
                        advanced_groups.add(group)
                    allocation = scheduling[
                        scheduler.worker.scope.producer + ":" + scheduler.worker.source
                    ]
                    previous_budget = allocation.get("budget_ms", 0) if reclaim else 0
                    allocation.update(
                        status="STARTED", budget_ms=previous_budget + allowance
                    )
                    if reclaim:
                        journal.setdefault("scheduler_reclaim_order", []).append(
                            scheduler.worker.scope.producer
                            + ":"
                            + scheduler.worker.source
                        )

                    def schedule_and_measure(
                        target=scheduler, budget=allowance, end=None
                    ):
                        # Saving the phase journal can itself use the final
                        # milliseconds. Do not launch a new run after expiry.
                        if end is not None:
                            budget = min(budget, int((end - time.monotonic()) * 1000))
                        requested = draining()
                        if requested:
                            budget = 0
                        if budget <= 0:
                            target.last_run_metrics = {"processed": 0, "elapsed_ms": 0}
                            target.last_run_diagnostics = {
                                "stage": "COMPLETE",
                                "stop_reason": (
                                    "STOP_REQUESTED"
                                    if requested
                                    else "BUDGET_EXHAUSTED"
                                ),
                                "failures": [],
                            }
                        try:
                            processed = (
                                {}
                                if budget <= 0
                                else target.run_once(
                                    limit=settings["scheduler.signal_batch"],
                                    hot_only=self.background_maintenance is not None,
                                    interval_seconds=settings[
                                        "scheduler.retry_seconds"
                                    ],
                                    lease_seconds=settings["scheduler.lease_seconds"],
                                    budget_ms=budget,
                                    stop_on_prepared=self.enable_entries,
                                    reconcile_seconds=settings[
                                        "scheduler.discovery_reconcile_seconds"
                                    ],
                                    stop_requested=stop_requested,
                                )
                            )
                        finally:
                            target_name = (
                                target.worker.scope.producer
                                + ":"
                                + target.worker.source
                            )
                            journal.setdefault("scheduler_metrics", {})[target_name] = {
                                key: round(value)
                                for key, value in getattr(
                                    target, "last_run_metrics", {}
                                ).items()
                            }
                            journal.setdefault("scheduler_diagnostics", {})[
                                target_name
                            ] = dict(getattr(target, "last_run_diagnostics", {}))
                        # Historical catch-up counts can be expensive. They are
                        # diagnostic, never a prerequisite for dispatch/protection.
                        return processed

                    name = (
                        scheduler.worker.scope.producer
                        if scheduler.worker.source == "s3"
                        else scheduler.worker.scope.producer
                        + (
                            ":tv"
                            if scheduler.worker.source == "tv_bridge"
                            else ":quant"
                        )
                    )
                    scheduler_deadline = min(
                        deadline, time.monotonic() + allowance / 1000
                    )
                    metric_key = (
                        scheduler.worker.scope.producer + ":" + scheduler.worker.source
                    )
                    combined = dict(journal["phases"].get(name, {})) if reclaim else {}
                    totals = (
                        dict(journal.get("scheduler_metrics", {}).get(metric_key, {}))
                        if reclaim
                        else {}
                    )
                    elapsed = journal["phase_elapsed_ms"].get(name, 0) if reclaim else 0
                    passes = allocation.get("passes", 0) if reclaim else 0
                    while True:
                        result = phase(
                            name,
                            lambda end=scheduler_deadline: schedule_and_measure(
                                end=end,
                            ),
                        )
                        passes += 1
                        if isinstance(result, dict):
                            combined.update(result)
                            prepared_observed = (
                                prepared_observed or "PREPARED" in result.values()
                            )
                        elapsed += journal["phase_elapsed_ms"][name]
                        metric_key = (
                            scheduler.worker.scope.producer
                            + ":"
                            + scheduler.worker.source
                        )
                        for metric, value in journal["scheduler_metrics"][
                            metric_key
                        ].items():
                            totals[metric] = totals.get(metric, 0) + value
                        journal.setdefault("scheduler_passes", {}).setdefault(
                            metric_key, []
                        ).append(
                            {
                                "round": "RECLAIM" if reclaim else "FIRST",
                                "metrics": dict(
                                    journal["scheduler_metrics"][metric_key]
                                ),
                                "diagnostics": dict(
                                    journal["scheduler_diagnostics"][metric_key]
                                ),
                            }
                        )
                        can_admit = can_admit and not failed(result) and not draining()
                        declines_before = len(journal.get("capacity_declines", {}))
                        proceed = can_admit and dispatch_prepared()
                        # A proven local capacity cancellation consumed no venue
                        # capacity. Drain the next candidate within this same
                        # consumer's budget instead of waiting a whole cycle.
                        # Accepted/ambiguous submissions and all other failures
                        # still stop immediately; dispatch enforces attempt caps.
                        if (
                            not proceed
                            or reclaim
                            or len(journal.get("capacity_declines", {}))
                            == declines_before
                            or time.monotonic() >= scheduler_deadline
                        ):
                            break
                    journal["phases"][name] = combined
                    journal["phase_elapsed_ms"][name] = elapsed
                    journal["scheduler_metrics"][metric_key] = totals
                    allocation.update(
                        status="FAILED" if not can_admit else "FINISHED",
                        elapsed_ms=elapsed,
                        overrun_ms=max(0, elapsed - allocation["budget_ms"]),
                        passes=passes,
                        processed=totals.get("processed", 0),
                        work_status=(
                            "PROCESSED" if totals.get("processed", 0) else "NO_PROGRESS"
                        ),
                    )
                    if not reclaim:
                        measured.append((name, scheduler))
                    if not proceed:
                        journal["evaluation_stop_reason"] = (
                            "STOP_REQUESTED"
                            if journal.get("drain_requested")
                            else (
                                "SAFETY_BLOCKED" if not can_admit else "DISPATCH_YIELD"
                            )
                        )
                        break
                    diagnostic = journal["scheduler_diagnostics"][metric_key]
                    if (
                        not reclaim
                        and totals.get("processed", 0) > 0
                        and diagnostic.get("stage") == "COMPLETE"
                        and diagnostic.get("stop_reason")
                        in {"BUDGET_EXHAUSTED", "BATCH_LIMIT"}
                        and _reclaim_failures_are_expected(diagnostic, combined)
                        and "PREPARED" not in combined.values()
                    ):
                        turns.append((index, scheduler, True))
                for allocation in scheduling.values():
                    if allocation["status"] == "NOT_ATTEMPTED":
                        allocation["reason"] = journal.get(
                            "evaluation_stop_reason", "NOT_REACHED"
                        )
            for name, scheduler in measured:
                if draining():
                    can_admit = False
                    break
                try:
                    if self.background_maintenance is not None:
                        report = self.background_maintenance.snapshot(scheduler)
                        journal.setdefault("background_maintenance", {})[name] = report
                        if "progress" in report:
                            journal["phases"][name]["progress"] = report["progress"]
                            journal["phases"][name]["progress_observed_at_ms"] = report[
                                "progress_observed_at_ms"
                            ]
                            journal["phases"][name]["progress_cached"] = True
                        continue
                    now_ms = self.runtime._now()
                    previous = self._progress_cache.get(name)
                    if previous is None or not (
                        0
                        <= now_ms - previous[0]
                        < settings["scheduler.progress_seconds"] * 1000
                    ):
                        previous = (now_ms, scheduler.progress())
                        self._progress_cache[name] = previous
                    journal["phases"][name]["progress"] = previous[1]
                    journal["phases"][name]["progress_observed_at_ms"] = previous[0]
                    journal["phases"][name]["progress_cached"] = previous[0] != now_ms
                except Exception as exc:  # noqa: BLE001 - diagnostic only, safe code
                    journal["phases"][name]["progress"] = {
                        "error_code": type(exc).__name__
                    }
            journal.update(
                status="CYCLE_COMPLETE" if can_admit else "ENTRY_BLOCKED",
                entry_dispatch_enabled=self.enable_entries,
                finished_at_ms=self.runtime._now(),
                entries=entries,
            )
            save()
            return journal
