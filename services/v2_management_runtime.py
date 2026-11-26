"""Management-only account loop: recovery, native protection and frozen exits.

No strategy, model, projection, settlement or notification is called here. This
is an explicitly selected alternative to the legacy pipeline, not permission
to start another account writer. The legacy pipeline lock remains exclusive;
concurrent entry/settlement workers require a separate ownership protocol.
"""

import json
from dataclasses import asdict, dataclass
from uuid import uuid4

from services.v2_trading_pipeline import _stage_clear, failed
from v2_core.account_registry import uid
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.runtime import DataRuntime
from v2_core.scheduling import diagnostic_code, drain_requested
from v2_core.session_lock import session_advisory_lock
from v2_core.state import BusinessState, StateKey
from v2_core.venue_readiness import GuardedOpeningSubmit

SCHEMA = "management-supervision-v1"
REASON = "TESTNET_TRADING_DAEMON_CYCLE"
PHASES = ("recovery", "protection", "exits")


@dataclass(frozen=True)
class ManagementBinding:
    scope: AccountScope
    tenant_id: str
    registry_id: str
    binding_version: int
    epoch: int
    worker_token: str

    def __post_init__(self):
        if type(self.scope) is not AccountScope or (
            self.scope.exchange,
            self.scope.environment,
            self.scope.product,
        ) != ("BINANCE", "SANDBOX", "FUTURES"):
            raise ValueError("MANAGEMENT_SANDBOX_SCOPE_REQUIRED")
        for name in ("tenant_id", "registry_id", "worker_token"):
            if uid(getattr(self, name)) != getattr(self, name):
                raise ValueError("MANAGEMENT_BINDING_INVALID")
        if any(
            type(value) is not int or value < 1
            for value in (self.binding_version, self.epoch)
        ):
            raise ValueError("MANAGEMENT_BINDING_INVALID")

    @property
    def guard_args(self):
        return {
            "tenant_id": self.tenant_id,
            "environment": self.scope.environment,
            "registry_id": self.registry_id,
            "binding_version": self.binding_version,
            "epoch": self.epoch,
            "worker_token": self.worker_token,
        }

    @property
    def digest(self):
        return digest(canonical(asdict(self)))


class ManagementSupervisor:
    def __init__(
        self, runtime, protection, exits, *, binding, guard, clock_ms, limit=10
    ):
        if (
            type(binding) is not ManagementBinding
            or not isinstance(runtime, DataRuntime)
            or runtime.scope != binding.scope
            or runtime.execution.scope != binding.scope
            or runtime.projectors
            or not isinstance(runtime.execution.submit, GuardedOpeningSubmit)
            or runtime.execution.submit.enabled is not False
            or runtime.execution.submit.readiness.scope != binding.scope
            or not callable(guard)
            or getattr(guard, "_args", None) != binding.guard_args
            or not callable(clock_ms)
            or type(limit) is not int
            or not 1 <= limit <= 20
            or any(
                getattr(stage, "scope", None) != binding.scope
                or not callable(getattr(stage, "run_once", None))
                for stage in (protection, exits)
            )
        ):
            raise ValueError("EXPLICIT_MANAGEMENT_ONLY_DEPENDENCIES_REQUIRED")
        self.runtime, self.protection, self.exits = runtime, protection, exits
        self.binding, self.guard, self.clock, self.limit = (
            binding,
            guard,
            clock_ms,
            limit,
        )
        self.scope, self.connect = binding.scope, runtime.data._connect
        self.store = BusinessState(self.connect)
        # Reuse the controlled diagnostic retention allowlist, but never the
        # legacy daemon's latest key or its looser STARTING liveness semantics.
        self.key = StateKey(
            **asdict(self.scope),
            namespace="testnet-trading-daemon-v1",
            key="management-latest",
        )

    def _now(self):
        value = self.clock()
        if type(value) is not int or not 0 <= value <= 2**53 - 1:
            raise ValueError("MANAGEMENT_CLOCK_INVALID")
        return value

    def _check_guard(self):
        if getattr(self.guard, "_args", None) != self.binding.guard_args:
            raise ValueError("MANAGEMENT_BINDING_CHANGED")
        # Check current ownership before each stage, but do not pin route
        # FOR SHARE across network calls or the entire cycle. Each signed
        # mutation must independently reacquire its real transport guard.
        with self.guard(operation="MANAGE"):
            pass
        if self.runtime.execution.submit.enabled is not False:
            raise ValueError("MANAGEMENT_OPENING_MUST_REMAIN_DISABLED")

    def _save(self, payload, previous):
        write = self.store.change(
            self.key,
            expected_version=previous.version if previous else 0,
            request_key=payload["cycle_id"] + ":" + payload["status"],
            payload=payload,
            reason=REASON,
        )
        if write.code != "APPLIED":
            raise ValueError("MANAGEMENT_CHECKPOINT_CONFLICT")
        return self.store.read(self.key)

    def run_once(self, *, stop_requested=None):
        if stop_requested is not None and not callable(stop_requested):
            raise TypeError("callable stop check required")
        if drain_requested(stop_requested):
            return {"status": "STOPPED", "execution_authorized": False}
        with session_advisory_lock(
            self.connect, "v2-pipeline:" + self.scope.key
        ) as acquired:
            if not acquired:
                # A contender must not overwrite the real owner's heartbeat.
                return {"status": "BUSY", "execution_authorized": False}
            journal = {
                "schema": SCHEMA,
                "cycle_id": str(uuid4()),
                "account_scope": asdict(self.scope),
                "binding_digest": self.binding.digest,
                "started_at_ms": self._now(),
                "status": "STARTED",
                "phases": {},
                "entry_dispatch_enabled": False,
                "execution_authorized": False,
            }
            # Invalidate prior success durably BEFORE any stage. Only two
            # bounded summaries per cycle; no repeated large inventory blobs.
            current = self._save(journal, self.store.read(self.key))
            coverage = None
            all_clear = True
            for name, action in (
                ("recovery", lambda: self.runtime.recover_once(self.limit)),
                ("protection", self.protection.run_once),
                ("exits", self.exits.run_once),
            ):
                if drain_requested(stop_requested):
                    journal["status"] = "STOPPED"
                    all_clear = False
                    break
                try:
                    self._check_guard()
                except Exception as exc:  # noqa: BLE001 - ownership failure stops all stages
                    journal["phases"][name] = {
                        "status": "BLOCKED",
                        "error_code": diagnostic_code(exc),
                    }
                    all_clear = False
                    break
                started = self._now()
                try:
                    after = getattr(self.exits, "run_after_protection", None)
                    result = (
                        after(coverage)
                        if name == "exits" and callable(after)
                        else action()
                    )
                    clear = (
                        isinstance(result, dict) and not failed(result)
                        if name == "recovery"
                        else _stage_clear(name, getattr(self, name), result)
                    )
                    journal["phases"][name] = {
                        "status": "CLEAR" if clear else "BLOCKED"
                    }
                    if not clear:
                        journal["phases"][name]["error_code"] = (
                            "MANAGEMENT_STAGE_NOT_CLEAR"
                        )
                    if name == "protection" and clear:
                        coverage = result.get("coverage")
                except Exception as exc:  # noqa: BLE001 - one failed stage cannot skip protection
                    clear = False
                    journal["phases"][name] = {
                        "status": "BLOCKED",
                        "error_code": diagnostic_code(exc),
                    }
                finished = self._now()
                if finished < started:
                    raise ValueError("MANAGEMENT_CLOCK_REGRESSED")
                journal["phases"][name].update(
                    started_at_ms=started, finished_at_ms=finished
                )
                all_clear = all_clear and clear
            if journal["status"] != "STOPPED":
                journal["status"] = "CLEAR" if all_clear else "BLOCKED"
            journal["finished_at_ms"] = self._now()
            if journal["finished_at_ms"] < journal["started_at_ms"]:
                raise ValueError("MANAGEMENT_CLOCK_REGRESSED")
            self._save(journal, current)
            return journal

    def health(self, *, max_age_ms=30000):
        """Current diagnostic completion, never an admission/dispatch permit."""
        if type(max_age_ms) is not int or not 1000 <= max_age_ms <= 300000:
            raise ValueError("BOUNDED_MANAGEMENT_HEALTH_AGE_REQUIRED")
        unavailable = {
            "status": "UNAVAILABLE",
            "reason": "MANAGEMENT_COMPLETION_UNAVAILABLE",
            "execution_authorized": False,
        }
        try:
            saved = self.store.read(self.key)
            if saved is None or saved.deleted:
                return unavailable
            payload = json.loads(saved.payload_json)
            # No old completed row lookup: STARTED/BLOCKED supersedes success.
            with self.connect() as conn:
                original = conn.execute(
                    "SELECT payload,deleted,reason FROM v2_state_history "
                    "WHERE state_id=%s AND version=%s",
                    (self.key.identity, saved.version),
                ).fetchone()
            now = self._now()
            if (
                original != (payload, False, REASON)
                or payload.get("schema") != SCHEMA
                or payload.get("account_scope") != asdict(self.scope)
                or payload.get("binding_digest") != self.binding.digest
                or payload.get("status") != "CLEAR"
                or payload.get("execution_authorized") is not False
                or payload.get("entry_dispatch_enabled") is not False
                or type(payload.get("started_at_ms")) is not int
                or type(payload.get("finished_at_ms")) is not int
                or not 0 <= payload["started_at_ms"] <= payload["finished_at_ms"] <= now
                or now - payload["started_at_ms"] > max_age_ms
                or type(payload.get("phases")) is not dict
                or set(payload["phases"]) != set(PHASES)
            ):
                return unavailable
            previous = payload["started_at_ms"]
            for phase in PHASES:
                result = payload["phases"][phase]
                if (
                    type(result) is not dict
                    or result.get("status") != "CLEAR"
                    or failed(result)
                    or type(result.get("started_at_ms")) is not int
                    or type(result.get("finished_at_ms")) is not int
                    or not previous
                    <= result["started_at_ms"]
                    <= result["finished_at_ms"]
                    <= payload["finished_at_ms"]
                ):
                    return unavailable
                previous = result["finished_at_ms"]
            self._check_guard()
            return {
                "status": "CURRENT",
                "reason": "MANAGEMENT_COMPLETED",
                "cycle_id": payload["cycle_id"],
                "started_at_ms": payload["started_at_ms"],
                "finished_at_ms": payload["finished_at_ms"],
                "execution_authorized": False,
            }
        except Exception:  # noqa: BLE001 - unavailable storage/identity is not fresh management
            return unavailable
