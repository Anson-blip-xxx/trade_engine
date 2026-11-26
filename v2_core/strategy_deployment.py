"""PG-owned strategy deployment generations, fenced inside OPEN authorization.

Deployment publication and entry validation share the existing account-risk
transaction lock, including the first publication when no state row exists.
Call ``require_strategy_opening`` only while holding the intent root lock, in
the SAME transaction as preparation or the submission CAS. The typed venue
adapter also rechecks it before the initial POST: a known-unsent request may
still be denied. This does not retract a sent request or grant a second permit.
CLOSE, protection and query recovery must not call the entry fence.
"""

from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID

from v2_core.account_risk import AccountRiskDenied, AccountScope, _lock
from v2_core.evidence import canonical
from v2_core.ingress import identity
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_registry import (
    DeploymentBinding,
    StrategyRegistry,
    StrategySpec,
    strategy_identifier,
)

NAMESPACE = "strategy-deployment-v1"
_REASON = "STRATEGY_DEPLOYMENT_PUBLISHED"
STRATEGY_ENTRY_DENIALS = frozenset(
    {
        "STRATEGY_DEPLOYMENT_MISSING",
        "STRATEGY_DEPLOYMENT_INVALID",
        "STRATEGY_DEPLOYMENT_PROOF_REQUIRED",
        "STRATEGY_DEPLOYMENT_PROOF_INVALID",
        "STRATEGY_DEPLOYMENT_MISMATCH",
        "STRATEGY_ENTRY_DISABLED",
        "STRATEGY_ENTRY_EXPIRED",
    }
)
_PROOF_FIELDS = frozenset(
    {
        "scope",
        "instance_id",
        "spec",
        "config_digest",
        "generation",
        "mode",
        "binding_digest",
    }
)


def _key(scope, instance_id):
    return StateKey(**asdict(scope), namespace=NAMESPACE, key=instance_id)


def _restore(value):
    if type(value) is not dict or set(value) != {
        "scope",
        "instance_id",
        "spec",
        "config_json",
        "generation",
        "mode",
    }:
        raise ValueError("STRATEGY_DEPLOYMENT_FIELDS_REQUIRED")
    if type(value["scope"]) is not dict or type(value["spec"]) is not dict:
        raise ValueError("STRATEGY_DEPLOYMENT_FIELDS_REQUIRED")
    return DeploymentBinding(
        **{
            **value,
            "scope": AccountScope(**value["scope"]),
            "spec": StrategySpec(**value["spec"]),
        }
    )


def _copy(binding):
    if type(binding) is not DeploymentBinding:
        raise TypeError("STRATEGY_TYPED_BINDING_REQUIRED")
    return _restore(asdict(binding))


def deployment_snapshot(binding):
    """Detached proof; the exact evidence configuration supplies its content."""
    binding = _copy(binding)
    return {
        "scope": asdict(binding.scope),
        "instance_id": binding.instance_id,
        "spec": asdict(binding.spec),
        "config_digest": binding.config_digest,
        "generation": binding.generation,
        "mode": binding.mode,
        "binding_digest": binding.binding_digest,
    }


def _stored_payload(binding):
    return {
        "schema": NAMESPACE,
        "binding": asdict(binding),
        "binding_digest": binding.binding_digest,
    }


def _read_current(conn, key):
    """Never interpret a removed/corrupt managed binding as an absent legacy one."""
    row = conn.execute(
        "SELECT version,payload,deleted,scope FROM v2_business_state WHERE state_id=%s",
        (key.identity,),
    ).fetchone()
    if row is None:
        if conn.execute(
            "SELECT 1 FROM v2_state_history WHERE state_id=%s LIMIT 1",
            (key.identity,),
        ).fetchone():
            raise AccountRiskDenied("STRATEGY_DEPLOYMENT_INVALID")
        return None
    history = conn.execute(
        "SELECT payload,deleted,expected_version,reason FROM v2_state_history "
        "WHERE state_id=%s AND version=%s",
        (key.identity, row[0]),
    ).fetchone()
    try:
        if (
            type(row[0]) is not int
            or row[2] is not False
            or row[3] != asdict(key)
            or history is None
            or history != (row[1], False, row[0] - 1, _REASON)
            or type(row[1]) is not dict
            or set(row[1]) != {"schema", "binding", "binding_digest"}
            or row[1]["schema"] != NAMESPACE
        ):
            raise ValueError("invalid stored deployment")
        binding = _restore(row[1]["binding"])
        if (
            binding.generation != row[0]
            or _key(binding.scope, binding.instance_id) != key
            or row[1] != _stored_payload(binding)
        ):
            raise ValueError("invalid stored deployment identity")
        return binding
    except (ValueError, TypeError, KeyError, RecursionError):
        raise AccountRiskDenied("STRATEGY_DEPLOYMENT_INVALID") from None


class StrategyDeployments:
    """Explicit, idempotent operator publication; no implicit activation."""

    def __init__(self, connect, registry):
        if not callable(connect) or type(registry) is not StrategyRegistry:
            raise TypeError("STRATEGY_DEPLOYMENT_PORTS_REQUIRED")
        self.connect, self.registry = connect, registry

    def publish(self, binding, *, expected_generation, request_id):
        binding = _copy(binding)
        self.registry.resolve_binding(binding)
        if (
            type(expected_generation) is not int
            or not 0 <= expected_generation < 2**53 - 1
            or binding.generation != expected_generation + 1
        ):
            raise ValueError("STRATEGY_DEPLOYMENT_NEXT_GENERATION_REQUIRED")
        identity(request_id)
        key = _key(binding.scope, binding.instance_id)
        with self.connect() as conn:
            _lock(conn, binding.scope.key)
            current = _read_current(conn, key)
            result = BusinessState(lambda: nullcontext(conn)).change(
                key,
                expected_version=expected_generation,
                request_key="publish:" + request_id,
                payload=_stored_payload(binding),
                reason=_REASON,
            )
            if result.code == "STALE":
                return {
                    "status": "STALE",
                    "generation": 0 if current is None else current.generation,
                    "binding_digest": None
                    if current is None
                    else current.binding_digest,
                    "execution_authorized": False,
                }
            # Old request replays return their original generation. The state
            # store does not overwrite any later publication in this case.
            return {
                "status": result.code,
                "generation": binding.generation,
                "binding_digest": binding.binding_digest,
                "execution_authorized": False,
            }

    def read(self, scope, instance_id):
        if type(scope) is not AccountScope:
            raise TypeError("STRATEGY_TYPED_SCOPE_REQUIRED")
        scope = AccountScope(**asdict(scope))
        strategy_identifier(instance_id)
        with self.connect() as conn:
            _lock(conn, scope.key)
            binding = _read_current(conn, _key(scope, instance_id))
        if binding is not None:
            self.registry.resolve_binding(binding)
        return binding


def require_strategy_opening(conn, episode):
    """Validate an intent's immutable binding under the caller's root lock.

    There is deliberately no fallback once an instance has a deployment record
    or an intent claims the new plan/proof schema. Unmanaged legacy producers
    remain compatible; this is not an account-wide producer allowlist.
    """
    episode = str(UUID(str(episode)))
    row = conn.execute(
        "SELECT i.exchange,i.account_id,i.environment,i.product,i.producer,"
        "i.strategy_version,i.config_digest,i.payload,e.strategy_version,"
        "e.config_digest,e.config,e.snapshot FROM v2_trade_intents i "
        "LEFT JOIN v2_decision_evidence e USING(evidence_ref) WHERE i.intent_id=%s",
        (episode,),
    ).fetchone()
    if row is None:
        raise AccountRiskDenied("STRATEGY_DEPLOYMENT_PROOF_INVALID")
    # Construct the scoped key before AccountScope validation so explicitly
    # unmanaged legacy accounting contracts keep their original behavior.
    scope_data = dict(
        zip(("exchange", "account_id", "environment", "product"), row[:4], strict=True)
    )
    _lock(conn, canonical(scope_data))
    key = StateKey(**scope_data, namespace=NAMESPACE, key=row[4])
    current = _read_current(conn, key)
    snapshot = row[11]
    if type(snapshot) is not dict:
        raise AccountRiskDenied("STRATEGY_DEPLOYMENT_PROOF_INVALID")
    has_proof = type(snapshot) is dict and "strategy_deployment" in snapshot
    has_plan = type(snapshot) is dict and "strategy_plan" in snapshot
    if current is None and not has_proof and not has_plan:
        return None
    if not has_proof:
        raise AccountRiskDenied("STRATEGY_DEPLOYMENT_PROOF_REQUIRED")
    proof = snapshot["strategy_deployment"]
    try:
        if type(proof) is not dict or set(proof) != _PROOF_FIELDS:
            raise ValueError("invalid frozen deployment")
        frozen = _restore(
            {
                **{
                    name: proof[name]
                    for name in _PROOF_FIELDS - {"binding_digest", "config_digest"}
                },
                "config_json": canonical(row[10]),
            }
        )
        if proof != deployment_snapshot(frozen):
            raise ValueError("invalid frozen deployment digest")
    except (ValueError, TypeError, KeyError, RecursionError):
        raise AccountRiskDenied("STRATEGY_DEPLOYMENT_PROOF_INVALID") from None
    if (
        asdict(frozen.scope) != scope_data
        or frozen.instance_id != row[4]
        or frozen.spec.code_version != row[5]
        or row[5] != row[8]
        or frozen.config_digest != row[6]
        or row[6] != row[9]
        or type(row[7]) is not dict
        or row[7].get("config_digest") != frozen.config_digest
        or row[7].get("strategy_version") != frozen.spec.code_version
    ):
        raise AccountRiskDenied("STRATEGY_DEPLOYMENT_MISMATCH")
    from v2_core.strategy_plan import PLAN_SCHEMA

    if has_plan and frozen.spec.plan_schema != PLAN_SCHEMA:
        raise AccountRiskDenied("STRATEGY_DEPLOYMENT_PROOF_INVALID")
    plan = None
    if frozen.spec.plan_schema == PLAN_SCHEMA:
        from v2_core.strategy_plan_store import read_trade_plan

        try:
            plan = read_trade_plan(conn, episode)
            if plan is None or (
                plan.strategy_id != frozen.spec.strategy_id
                or plan.code_version != frozen.spec.code_version
                or plan.deployment_generation != frozen.generation
                or plan.config_digest != frozen.config_digest
                or plan.exit_handler != frozen.spec.exit_handler
            ):
                raise ValueError("invalid frozen plan binding")
        except (ValueError, TypeError, KeyError, RecursionError):
            raise AccountRiskDenied("STRATEGY_DEPLOYMENT_PROOF_INVALID") from None
    if current is None:
        raise AccountRiskDenied("STRATEGY_DEPLOYMENT_MISSING")
    if current.mode != "ACTIVE" or frozen.mode != "ACTIVE":
        raise AccountRiskDenied("STRATEGY_ENTRY_DISABLED")
    if current.binding_digest != frozen.binding_digest:
        raise AccountRiskDenied("STRATEGY_DEPLOYMENT_MISMATCH")
    if plan is not None:
        now_ms = conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]
        if not plan.entry_valid_at(now_ms):
            raise AccountRiskDenied("STRATEGY_ENTRY_EXPIRED")
    return deployment_snapshot(frozen)
