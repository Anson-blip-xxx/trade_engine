"""One account-bound lifecycle supervisor; default no new order dispatch.

Explicit PM/exit/settlement stages are mandatory. They must return CLEAR only
after their own evidence-based checks, never merely because a scan did not crash.
This composition is not a substitute for implementing or deploying those stages.
"""

from dataclasses import asdict
from uuid import uuid4

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


class TradingPipeline:
    def __init__(
        self,
        *,
        runtime,
        market,
        schedulers,
        protection,
        exits,
        settlement,
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
        if len(schedulers) != 2 or {s.worker.scope.producer for s in schedulers} != {
            "s6",
            "s8",
        }:
            raise ValueError("both formal directional consumers required")
        for scheduler in schedulers:
            if (
                scheduler.worker.runtime is not runtime
                or scheduler.worker.source != "s3"
                or scheduler.worker.strategy_version != "directional-admission-v2-1"
            ):
                raise ValueError("single runtime and actual directional rules required")
        if market.source.publisher.environment != scope.environment:
            raise ValueError("market environment mismatch")
        for stage in (protection, exits, settlement):
            if getattr(stage, "scope", None) != scope or not callable(
                getattr(stage, "run_once", None)
            ):
                raise ValueError(
                    "bound protection, exit and settlement stages required"
                )
        self.runtime, self.scope, self.market, self.schedulers = (
            runtime,
            scope,
            market,
            schedulers,
        )
        self.protection, self.exits, self.settlement = protection, exits, settlement
        self.enable_entries = enable_entries
        self.connect = runtime.data._connect

    def run_once(self, *, limit=10):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("bounded pipeline batch required")
        with self.connect() as owner:
            if not owner.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("v2-pipeline:" + self.scope.key,),
            ).fetchone()[0]:
                return {"status": "BUSY"}
            owner.commit()
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
                try:
                    result = action()
                except Exception as exc:  # noqa: BLE001 - safe persistent stage failure
                    result = {"status": "BLOCKED", "error_code": type(exc).__name__}
                journal["phases"][name] = result
                save()
                return result

            save()
            recovered = phase(
                "recovery", lambda: self.runtime.tick(overdue_ms=60000, limit=limit)
            )
            safety = []
            for name, stage in (
                ("protection", self.protection),
                ("exits", self.exits),
                ("settlement", self.settlement),
            ):
                result = phase(name, stage.run_once)
                safety.append(
                    isinstance(result, dict)
                    and result.get("status") == "CLEAR"
                    and not failed(result)
                )
            # Market failure must not prevent recovery/PM/exit/settlement above.
            market = phase("market", self.market.run_once)
            market_ok = (
                isinstance(market, dict)
                and market.get("status") in {"ACKNOWLEDGED", "CURRENT"}
                and not failed(market)
            )
            can_admit = all(safety) and not failed(recovered) and market_ok
            if can_admit:
                for scheduler in self.schedulers:
                    result = phase(
                        scheduler.worker.scope.producer,
                        lambda s=scheduler: s.run_once(limit=limit),
                    )
                    can_admit = can_admit and not failed(result)
            entries = {}
            if self.enable_entries and can_admit:
                with self.connect() as conn:
                    orders = conn.execute(
                        """SELECT o.order_id::text FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                        AND i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1'
                        AND o.leg='OPEN' AND o.status='PREPARED' ORDER BY o.updated_at,o.order_id LIMIT %s""",
                        (*asdict(self.scope).values(), limit),
                    ).fetchall()
                for (order_id,) in orders:
                    result = phase(
                        "dispatch:" + order_id,
                        lambda oid=order_id: self.runtime.execution.dispatch(oid),
                    )
                    entries[order_id] = result
                    # Any possibly accepted opening stops this cycle's admissions.
                    # Recover its fills and invoke PM immediately, even after timeout.
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
                        break
            journal.update(
                status="CYCLE_COMPLETE" if can_admit else "ENTRY_BLOCKED",
                entry_dispatch_enabled=self.enable_entries,
                finished_at_ms=self.runtime._now(),
                entries=entries,
            )
            save()
            return journal
