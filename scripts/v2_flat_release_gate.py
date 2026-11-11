"""Read-only SANDBOX release evidence; never a deployment or trading permit.

The caller owns fencing/stopping and must call again after every execution
process has exited, before replacing the release link. REST and PostgreSQL are
not one atomic snapshot. This helper never drains, stops, resumes or persists.
An already constructed inventory retains its transport in memory across stop.
"""

from dataclasses import asdict
from uuid import UUID, uuid4

from v2_core.account_inventory import (
    COMBINED_ROUTES,
    ROUTES,
    AccountInventory,
    orders,
    positions,
)
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest


class FlatGateError(RuntimeError):
    """Fixed codes only; underlying exception text never leaves the gate."""


def _require(condition, code):
    if not condition:
        raise FlatGateError(code)


def _baseline(scope, baseline):
    _require(
        isinstance(scope, AccountScope) and scope.environment == "SANDBOX",
        "SANDBOX_SCOPE_REQUIRED",
    )
    try:
        _require(isinstance(baseline, dict), "INVALID_ROUTE_BASELINE")
        tenant, registry = (
            str(UUID(baseline[name])) for name in ("tenant", "registry")
        )
        _require(
            all(
                type(baseline[name]) is int and baseline[name] > 0
                for name in ("binding", "epoch")
            )
            and type(baseline["request_epoch"]) is int
            and baseline["request_epoch"] == baseline["epoch"]
            and baseline["pending"] is False,
            "INVALID_ROUTE_BASELINE",
        )
        _require(
            tuple(baseline[name] for name in ("exchange", "account", "product"))
            == (scope.exchange, scope.account_id, scope.product),
            "BASELINE_SCOPE_MISMATCH",
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        raise FlatGateError("INVALID_ROUTE_BASELINE") from None
    return tenant, registry


def _snapshot(connect, scope, baseline, identity, query_timeout_ms, route_role):
    """Two indexed/scoped SELECTs, in one read-only consistent snapshot."""
    tenant, registry = identity
    account = tuple(asdict(scope).values())
    try:
        with connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute(
                "SELECT set_config('statement_timeout',%s,true),"
                "set_config('lock_timeout','250ms',true)",
                (f"{query_timeout_ms}ms",),
            )
            conn.execute("SET LOCAL jit=off")
            conn.execute("SET LOCAL max_parallel_workers_per_gather=0")
            if route_role is not None:
                # Fixed existing role only; no credential columns or grants.
                conn.execute("SET LOCAL ROLE trade_v2_execution_operator")
            routes = conn.execute(
                """SELECT r.target_registry::text,r.binding_version,r.epoch,
                    r.worker_token::text,r.phase,a.exchange,a.account_id,
                    a.environment,a.product,a.version,
                    EXISTS(SELECT 1 FROM v2_account_retirements x
                        WHERE x.tenant_id=a.tenant_id AND x.registry_id=a.registry_id),
                    COALESCE((SELECT max(q.epoch) FROM v2_execution_route_events q
                        WHERE q.tenant_id=r.tenant_id AND q.environment=r.environment),0)
                FROM v2_execution_routes r JOIN v2_tenant_accounts a
                    ON a.tenant_id=r.tenant_id AND a.registry_id=r.target_registry
                WHERE r.tenant_id=%s AND r.environment=%s""",
                (tenant, scope.environment),
            ).fetchall()
            if route_role is not None:
                conn.execute("RESET ROLE")
            _require(len(routes) == 1, "UNIQUE_ROUTE_REQUIRED")
            row = routes[0]
            _require(
                row[:3] == (registry, baseline["binding"], baseline["epoch"])
                and tuple(row[5:9]) == account
                and row[9] == baseline["binding"]
                and row[10] is False,
                "ROUTE_BINDING_CHANGED",
            )
            _require(row[4] in {"ACTIVE", "DRAINING"}, "ROUTE_PHASE_UNSAFE")
            _require(
                isinstance(row[3], str) and str(UUID(row[3])) == row[3],
                "ROUTE_WORKER_UNVERIFIED",
            )
            _require(
                type(row[11]) is int and row[11] == baseline["epoch"],
                "CONCURRENT_ROUTE_REQUEST",
            )
            counts = conn.execute(
                """SELECT
                (SELECT count(*) FROM v2_orders o JOIN v2_trade_intents i
                    ON i.intent_id=o.episode_id
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED')),
                (SELECT count(*) FROM v2_episodes e JOIN v2_trade_intents i
                    ON i.intent_id=e.episode_id
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND e.status NOT IN ('SETTLED','ABORTED')),
                (SELECT count(*) FROM v2_trade_intents i
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND i.status NOT IN ('REJECTED','EXPIRED','CANCELLED')
                    AND NOT EXISTS(SELECT 1 FROM v2_episodes e WHERE e.episode_id=i.intent_id))
                """,
                account * 3,
            ).fetchone()
            _require(
                isinstance(counts, (tuple, list))
                and len(counts) == 3
                and all(type(value) is int and value >= 0 for value in counts),
                "INVALID_LOCAL_COUNTS",
            )
            local = dict(
                zip(
                    ("nonterminal_orders", "unsettled_episodes", "unstarted_intents"),
                    counts,
                    strict=True,
                )
            )
            _require(not any(counts), "LOCAL_RELEASE_WORK_PENDING")
        return {"phase": row[4], "worker_token": row[3], "counts": local}
    except FlatGateError:
        raise
    except Exception:  # noqa: BLE001 - no SQL/parameters or connection strings
        raise FlatGateError("DATABASE_CHECK_UNAVAILABLE") from None


def _venue(observation, scope, observation_id, *, started, now, max_duration_ms):
    _require(isinstance(observation, dict), "INVALID_INVENTORY_PROOF")
    _require(
        observation.get("account_scope") == asdict(scope)
        and observation.get("observation_id") == observation_id,
        "INVENTORY_SCOPE_MISMATCH",
    )
    _require(
        observation.get("status") == "CLEAR_FOR_RECOVERY_CHECKS"
        and observation.get("execution_authorized") is False
        and observation.get("blockers") == []
        and observation.get("failures") == {}
        and observation.get("failure_details") == {},
        "INVENTORY_NOT_CLEAR",
    )
    _require(
        observation.get("excluded_position_symbols") == [],
        "POSITION_EXCLUSIONS_FORBIDDEN",
    )
    first, last = observation.get("started_at_ms"), observation.get("finished_at_ms")
    _require(
        type(first) is int
        and type(last) is int
        and started <= first <= last <= now
        and last - first <= max_duration_ms,
        "INVENTORY_TIME_UNVERIFIED",
    )
    raw = observation.get("responses")
    _require(isinstance(raw, dict), "INCOMPLETE_INVENTORY_PROOF")
    wanted = {
        name for name, _ in (COMBINED_ROUTES if "account_config" in raw else ROUTES)
    }
    _require(set(raw) == wanted, "INCOMPLETE_INVENTORY_PROOF")
    _require(
        observation.get("response_digests")
        == {key: digest(canonical({"response": value})) for key, value in raw.items()},
        "INVENTORY_DIGEST_MISMATCH",
    )
    actual = {
        "positions_before": positions(raw["positions_before"]),
        "positions_after": positions(raw["positions_after"]),
        "account_positions": positions(raw["account"]["positions"]),
        "ordinary_orders": orders(raw["ordinary_orders"], conditional=False),
        "conditional_orders": orders(raw["conditional_orders"], conditional=True),
    }
    _require(not any(actual.values()), "VENUE_NOT_FLAT")
    summary = observation.get("summary")
    _require(
        isinstance(summary, dict)
        and all(
            summary.get(key) == []
            for key in ("positions", "ordinary_orders", "conditional_orders")
        )
        and summary.get("excluded_positions", []) == [],
        "INVENTORY_SUMMARY_MISMATCH",
    )
    return {key: len(value) for key, value in actual.items()}, last


def verify_flat(
    *,
    connect,
    inventory,
    scope,
    baseline,
    clock_ms,
    max_age_ms=5000,
    query_timeout_ms=3000,
    route_role=None,
):
    """Return sanitized current proof, or raise FlatGateError with a fixed code.

    ``baseline`` is the existing release protocol's database("capture") result.
    Accept ACTIVE before stop and DRAINING after stop, never a different epoch
    or account. Caller must establish process-exit proof separately; this
    function intentionally cannot make its observational result an atomic lock.
    """
    identity = _baseline(scope, baseline)
    _require(callable(connect) and callable(clock_ms), "EXPLICIT_GATE_PORTS_REQUIRED")
    _require(
        type(max_age_ms) is int
        and 1 <= max_age_ms <= 60000
        and type(query_timeout_ms) is int
        and 1 <= query_timeout_ms <= 10000,
        "BOUNDED_GATE_TIMING_REQUIRED",
    )
    _require(
        route_role is None or route_role == "trade_v2_execution_operator",
        "FIXED_ROUTE_ROLE_REQUIRED",
    )
    _require(
        isinstance(inventory, AccountInventory)
        and inventory.scope == scope
        and (
            getattr(inventory.request, "account_id", None),
            getattr(inventory.request, "environment", None),
        )
        == (scope.account_id, scope.environment),
        "INVENTORY_TRANSPORT_SCOPE_MISMATCH",
    )
    _require(inventory.excluded_position_symbols == (), "POSITION_EXCLUSIONS_FORBIDDEN")
    try:
        before = _snapshot(
            connect, scope, baseline, identity, query_timeout_ms, route_role
        )
        started = clock_ms()
        _require(type(started) is int and started >= 0, "GATE_CLOCK_INVALID")
        identifier = str(uuid4())
        observation = inventory.collect(identifier)
        checked = clock_ms()
        _require(type(checked) is int and checked >= started, "GATE_CLOCK_INVALID")
        venue, finished = _venue(
            observation,
            scope,
            identifier,
            started=started,
            now=checked,
            max_duration_ms=inventory.max_duration_ms,
        )
        after = _snapshot(
            connect, scope, baseline, identity, query_timeout_ms, route_role
        )
        _require(
            before["worker_token"] == after["worker_token"], "ROUTE_WORKER_CHANGED"
        )
        now = clock_ms()
        _require(
            type(now) is int and 0 <= now - finished <= max_age_ms,
            "FLAT_EVIDENCE_STALE",
        )
        return {
            "status": "FLAT_VERIFIED",
            "same_binding": True,
            "environment": scope.environment,
            "route_epoch": baseline["epoch"],
            "route_phase": after["phase"],
            "venue_counts": venue,
            "local_counts": after["counts"],
            "observed_at_ms": now,
            "inventory_finished_at_ms": finished,
            "atomic_snapshot": False,
            "execution_authorized": False,
        }
    except FlatGateError:
        raise
    except Exception:  # noqa: BLE001 - not even the exception type exposes an SDK payload
        raise FlatGateError("FLAT_CHECK_UNAVAILABLE") from None
