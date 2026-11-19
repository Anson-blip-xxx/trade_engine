"""Post-risk JEV research, with no authority over the frozen OPEN candidate.

``submit`` only validates/copies public facts and attempts an in-memory enqueue.
Its immutable decision receipt explicitly says SUBMITTED_NOT_DURABLE: a crash,
full queue or database failure is missing research coverage, never a model pass.
The worker persists a separate, version-bound binding and outcome using existing
business state, then reuses the existing JEV advisor and its shared budget.
Neither the queue nor a research result proves an order or settlement occurred.
"""

import json
import threading
from collections import deque
from contextlib import nullcontext
from dataclasses import asdict, replace
from uuid import UUID

from services.v2_jev_coordinator import (
    _FALLBACK_REASONS,
    CONTROL_KEY,
    REQUEST_NAMESPACE,
    _clock,
    _limit_transaction,
)
from services.v2_jev_reference import PUBLIC_SCOPE, public_features
from v2_core.account_risk import AccountScope
from v2_core.directional import supports_event
from v2_core.evidence import canonical, digest
from v2_core.jev_advisory import (
    RESERVE_TOKENS,
    SHADOW_CANDLE_MAX_AGE_MS,
    SHADOW_QUOTE_CONTRACT,
    STRUCTURE_INDICATOR_VERSION,
    AdvisoryPolicy,
    JevAdvisory,
    _cached,
    _decimal,
    _integer,
    _result,
    prepare_request,
)
from v2_core.runtime_policy import resolve
from v2_core.signal_fusion import validate_fusion, validated_public_features
from v2_core.state import BusinessState, StateKey
from v2_core.strategy import StrategyDecision

BINDING_NAMESPACE = "jev-shadow-binding-v1"
RESULT_NAMESPACE = "jev-shadow-result-v1"
RECEIPT_SCHEMA = "jev-shadow-receipt-v1"
_SAFE_REJECTION_REASONS = {
    "JEV_MARKET_SNAPSHOT_EXPIRED",
    "JEV_SUPPLEMENTAL_SNAPSHOT_EXPIRED",
    "JEV_INDICATOR_SNAPSHOT_EXPIRED",
    "JEV_SHADOW_CANDLE_SNAPSHOT_EXPIRED",
    "JEV_SHADOW_PUBLIC_QUOTE_EXPIRED",
}
_NEUTRAL = {
    "decision_effect": "NONE",
    "execution_authorized": False,
    "risk_override": False,
    "admission_authorized": False,
}


def _require(condition):
    if not condition:
        raise ValueError("JEV_SHADOW_IDENTITY_INVALID")


def _shadow_public_quote(features, context, now):
    """Freeze an existing public mark, not a new quote or a re-clocked candle.

    The account-context producer persisted this premiumIndex observation before
    returning the context. Submit cannot read PG: the worker must validate that
    exact immutable account-state reference before allowing a model request.
    No account balances, positions, sizing limits or other private facts enter
    the public feature projection.
    """
    sizing = context.get("directional", {}).get("sizing", {})
    if (
        features.get("indicator_version") != STRUCTURE_INDICATOR_VERSION
        or "sizing_mark_price" not in sizing
    ):
        return features, None
    evidence = context["account_evidence"]
    state_id = evidence["state_id"]
    _require(type(state_id) is str and str(UUID(state_id)) == state_id)
    _require(type(evidence["version"]) is int and evidence["version"] == 1)
    observed = _integer(evidence["sizing_mark_observed_at_ms"], 0, 2**53 - 1)
    _require(observed <= context["assembled_at"])
    if not 0 <= now - observed < 15000:
        raise ValueError("JEV_SHADOW_PUBLIC_QUOTE_EXPIRED")
    freshness = context["freshness_policy"]["s3"]
    _require(freshness["scope"] == "SYMBOL")
    candle_max_age = min(
        SHADOW_CANDLE_MAX_AGE_MS,
        _integer(freshness["max_age_ms"], 1, 300000),
    )
    if not 0 <= now - features["observed_at_ms"] < candle_max_age:
        raise ValueError("JEV_SHADOW_CANDLE_SNAPSHOT_EXPIRED")
    price = _decimal(sizing["sizing_mark_price"], "0.000000000001", "1000000000")
    return {
        **features,
        "public_quote_contract": SHADOW_QUOTE_CONTRACT,
        "candle_observed_at_ms": features["observed_at_ms"],
        "candle_price": features["price"],
        "observed_at_ms": observed,
        "price": price,
    }, {
        "state_id": state_id,
        "version": 1,
        "observed_at_ms": observed,
        "price": price,
    }


def _snapshot(store, key):
    row = store.read(key)
    if row is None:
        return None
    _require(not row.deleted and row.version >= 1)
    return {"version": row.version, "values": json.loads(row.payload_json)}


def _put_once(store, key, payload):
    """An existing version is reusable only when every frozen byte agrees."""
    row = _snapshot(store, key)
    if row is not None:
        _require(row == {"version": 1, "values": payload})
        return
    written = store.change(
        key,
        expected_version=0,
        request_key="freeze:" + digest(canonical(payload)),
        payload=payload,
        reason="JEV research only; no admission or execution authority",
    )
    _require(written.code in {"APPLIED", "ALREADY_APPLIED"} and written.version == 1)


class _FrozenDeadlineCoordinator:
    """Reject a delayed start rather than extend the immutable research window."""

    def __init__(self, delegate, frozen):
        self.delegate, self.frozen = delegate, frozen

    def lookup(self, **kwargs):
        return self.delegate.lookup(**kwargs)

    def claim(self, **kwargs):
        _require(
            kwargs["request_key"] == self.frozen["request_key"]
            and kwargs["request"] == self.frozen["request"]
        )
        if (
            not self.frozen["submitted_at_ms"]
            <= kwargs["now_ms"]
            < kwargs["deadline_ms"]
            <= self.frozen["deadline_ms"]
        ):
            return {"status": "BUSY"}
        return self.delegate.claim(**kwargs)

    def complete(self, **kwargs):
        return self.delegate.complete(**kwargs)


class JevShadow:
    """A bounded, default-OFF StrategyWorker entry_observer port.

    ``reference`` is the already configured JevReference, not another provider
    client. Its advisor uses the existing PUBLIC_SCOPE coordinator and budget.
    ``connect`` must open a dedicated bounded background connection. There is
    deliberately no lookup, assess, wait_pending, trade or settlement method.
    """

    def __init__(
        self,
        connect,
        reference,
        *,
        scope,
        clock_ms,
        stop,
        enabled=False,
        queue_size=8,
        max_wait_ms=2000,
        max_age_ms=60000,
    ):
        _require(
            callable(connect)
            and callable(getattr(reference, "advisor", None))
            and isinstance(scope, AccountScope)
            and (scope.exchange, scope.environment, scope.product)
            == ("BINANCE", "SANDBOX", "FUTURES")
            and callable(clock_ms)
            and callable(getattr(stop, "is_set", None))
            and type(enabled) is bool
        )
        self.connect, self.reference, self.scope = connect, reference, scope
        self.clock, self.stop, self.enabled = clock_ms, stop, enabled
        self.capacity = _integer(queue_size, 1, 128)
        self.max_wait_ms = _integer(max_wait_ms, 100, 300000)
        self.max_age_ms = _integer(max_age_ms, 1000, 300000)
        self.queue, self.pending = deque(), set()
        self.lock, self.wake = threading.Lock(), threading.Event()
        self.thread = None

    def _state_key(self, namespace, identity):
        return StateKey(**asdict(self.scope), namespace=namespace, key=identity)

    def _freeze(
        self,
        decision,
        *,
        signal_id,
        signal,
        context,
        config,
        consumer,
        now,
        strategy_version=None,
    ):
        _require(isinstance(decision, StrategyDecision) and decision.action == "OPEN")
        _require(
            strategy_version is None
            or (
                type(strategy_version) is str
                and 0 < len(strategy_version) <= 200
                and strategy_version == strategy_version.strip()
            )
        )
        original = json.loads(decision.features_json)
        _require(
            original.get("admission_authorized") is True
            and original.get("execution_authorized") is False
        )
        _require(type(signal_id) is str and str(UUID(signal_id)) == signal_id)
        parts = json.loads(consumer)
        _require(
            type(parts) is list
            and len(parts) == 5
            and parts[:4] == list(asdict(self.scope).values())
            and parts[4] in {"s6", "s8"}
        )
        strategy, source = parts[4].upper(), config["source"]
        _require(
            source in {"s3", "tv_bridge", "quant"}
            and config["strategy"] == strategy
            and config["account_scope"] == asdict(self.scope)
            and config["mode"] == "TESTNET_ADMISSION_ONLY"
            and context["account_scope"] == asdict(self.scope)
            and context["environment"] == "SANDBOX"
            and context["symbol"] == signal["symbol"]
            and signal.get("source", source) == source
            and signal.get("environment", "SANDBOX") == "SANDBOX"
            and supports_event(strategy, signal["signal"])
            and decision.side == {"S6": "BUY", "S8": "SELL"}[strategy]
        )
        for value in (
            now,
            signal["observed_at"],
            signal["expires_at_ms"],
            context["assembled_at"],
            context["valid_until_ms"],
        ):
            _integer(value, 0, 2**53 - 1)
        _require(
            signal["observed_at"] <= now < signal["expires_at_ms"]
            and context["assembled_at"] <= now < context["valid_until_ms"]
        )
        policy_version, policy_digest = (
            config.get("policy_version"),
            config.get("policy_digest"),
        )
        if policy_version is not None or policy_digest is not None:
            _integer(policy_version, 0, 2**53 - 1)
            _require(policy_digest == digest(canonical(config["policy"])))
        direction = "LONG" if decision.side == "BUY" else "SHORT"
        market = context["sources"]["s3"]
        features = public_features(
            market,
            target=signal["symbol"],
            direction=direction,
            context=context,
            now_ms=now,
        )
        features, quote_evidence = _shadow_public_quote(features, context, now)
        fusion = validate_fusion(
            signal, source=source, proof=context.get("signal_fusion"), now_ms=now
        )
        if fusion is not None:
            _require(not fusion["veto"] and fusion["direction"] == direction)
            features.update(validated_public_features(fusion, now))
        prepared = prepare_request(features, now_ms=now, max_age_ms=self.max_age_ms)
        deadline = min(
            now + self.max_wait_ms,
            signal["expires_at_ms"],
            context["valid_until_ms"],
            prepared["valid_until_ms"],
        )
        _require(now < deadline)
        return {
            "schema": "jev-shadow-candidate-v1",
            "scope": asdict(self.scope),
            "consumer": consumer,
            "signal_id": signal_id,
            "signal_digest": digest(canonical(signal)),
            "candidate_digest": digest(canonical(asdict(decision))),
            "strategy_version": strategy_version,
            "config_digest": digest(canonical(config)),
            "policy_version": policy_version,
            "policy_digest": policy_digest,
            "source": source,
            "symbol": signal["symbol"],
            "direction": direction,
            "market_digest": digest(canonical(market)),
            "public_quote_evidence": quote_evidence,
            "request_key": prepared["request_key"],
            "request": prepared["request"],
            "original_decision_at_ms": now,
            "submitted_at_ms": now,
            "signal_observed_at_ms": signal["observed_at"],
            "signal_expires_at_ms": signal["expires_at_ms"],
            "context_assembled_at_ms": context["assembled_at"],
            "context_valid_until_ms": context["valid_until_ms"],
            "deadline_ms": deadline,
            "max_age_ms": self.max_age_ms,
            "shadow_policy": {
                "version": 1,
                "enabled": True,
                "max_wait_ms": self.max_wait_ms,
                "max_age_ms": self.max_age_ms,
                "candle_max_age_ms": (
                    SHADOW_CANDLE_MAX_AGE_MS if quote_evidence is not None else None
                ),
            },
            "trade_link_status": "NOT_PROVEN_EXECUTED",
            **_NEUTRAL,
        }

    def submit(
        self,
        decision,
        *,
        signal_id,
        signal,
        context,
        config,
        consumer,
        now_ms=None,
        strategy_version=None,
    ):
        """Return a research receipt without any synchronous external IO."""
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "status": "INVALID_CANDIDATE",
            "coverage": "MISSING",
            "signal_id": None,
            "consumer": None,
            "candidate_digest": None,
            "strategy_version": strategy_version,
            "request_key": None,
            "binding_state_id": None,
            "result_state_id": None,
            "submitted_at_ms": None,
            "deadline_ms": None,
            "trade_link_status": "NOT_PROVEN_EXECUTED",
            **_NEUTRAL,
        }
        try:
            # These identifiers remain available even when the optional switch
            # is off; context/model validation never alters a baseline decision.
            receipt["signal_id"] = str(UUID(signal_id))
            receipt["consumer"] = consumer if type(consumer) is str else None
            if isinstance(decision, StrategyDecision):
                receipt["candidate_digest"] = digest(canonical(asdict(decision)))
            if not self.enabled or self.stop.is_set():
                receipt["status"] = "DISABLED" if not self.enabled else "STOPPED"
                return receipt
            now = self.clock() if now_ms is None else now_ms
            frozen = self._freeze(
                decision,
                signal_id=signal_id,
                signal=signal,
                context=context,
                config=config,
                consumer=consumer,
                now=now,
                strategy_version=strategy_version,
            )
            encoded = canonical(frozen)
            identity = digest(encoded)
            receipt.update(
                request_key=frozen["request_key"],
                submitted_at_ms=now,
                deadline_ms=frozen["deadline_ms"],
                binding_state_id=self._state_key(BINDING_NAMESPACE, identity).identity,
                result_state_id=self._state_key(RESULT_NAMESPACE, identity).identity,
            )
            if not self.lock.acquire(blocking=False):
                receipt["status"] = "QUEUE_BUSY"
                return receipt
            try:
                if self.stop.is_set():
                    receipt["status"] = "STOPPED"
                elif identity in self.pending:
                    self._start_worker()
                    self.wake.set()
                    receipt.update(
                        status="SUBMITTED_NOT_DURABLE",
                        coverage="MISSING_UNTIL_DURABLE_RESULT",
                    )
                elif len(self.queue) >= self.capacity:
                    receipt["status"] = "QUEUE_FULL"
                else:
                    self._start_worker()
                    self.queue.append((identity, encoded))
                    self.pending.add(identity)
                    self.wake.set()
                    receipt.update(
                        status="SUBMITTED_NOT_DURABLE",
                        coverage="MISSING_UNTIL_DURABLE_RESULT",
                    )
            finally:
                self.lock.release()
        except (KeyError, TypeError, ValueError) as exc:
            receipt.update(
                status="INVALID_CANDIDATE",
                coverage="MISSING",
                reason=(
                    str(exc)
                    if type(exc) is ValueError and str(exc) in _SAFE_REJECTION_REASONS
                    else "JEV_SHADOW_IDENTITY_INVALID"
                ),
            )
        except Exception:  # noqa: BLE001 - an optional observer never changes entry
            receipt.update(status="SUBMISSION_UNAVAILABLE", coverage="MISSING")
        return receipt

    def _start_worker(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(
                target=self._run, name="jev-shadow", daemon=True
            )
            self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            with self.lock:
                item = self.queue.popleft() if self.queue else None
                if item is None:
                    self.wake.clear()
            if item is None:
                self.wake.wait(0.1)
                continue
            identity, encoded = item
            try:
                self._process(identity, encoded)
            except Exception:  # noqa: BLE001, S110 - absent result remains missing coverage
                pass
            finally:
                with self.lock:
                    self.pending.discard(identity)

    def _runtime_matches(self, store, frozen):
        if frozen["policy_version"] is None:
            return True
        current = _snapshot(store, self._state_key("runtime-policy-v1", "active"))
        if current is None:
            return (
                frozen["policy_version"] == 0
                and digest(canonical(resolve())) == frozen["policy_digest"]
            )
        return (
            current["version"] == frozen["policy_version"]
            and digest(canonical(resolve(current["values"]["values"])))
            == frozen["policy_digest"]
        )

    def _verify_public_quote(self, conn, frozen):
        """Worker-only PK lookup: prove the public projection's source/version.

        The durable producer record is immutable version 1. Its full private
        payload stays local; only a digest and public response digest are bound
        to research evidence, and neither is included in the model request.
        """
        proof = frozen.get("public_quote_evidence")
        if proof is None:
            _require("public_quote_contract" not in frozen["request"]["state"])
            return None
        row = conn.execute(
            "SELECT version,payload,deleted,scope FROM v2_business_state WHERE state_id=%s",
            (proof["state_id"],),
        ).fetchone()
        _require(
            row is not None and row[0] == proof["version"] == 1 and row[2] is False
        )
        payload, state_scope = row[1], row[3]
        observation_id = payload["observation_id"]
        _require(str(UUID(observation_id)) == observation_id)
        key = self._state_key("directional-account-context-v1", observation_id)
        state = frozen["request"]["state"]
        observed = _integer(
            payload["market_observed_at_ms"]["premium_index"], 0, 2**53 - 1
        )
        started = _integer(payload["started_at_ms"], 0, 2**53 - 1)
        finished = _integer(payload["finished_at_ms"], started, started + 15000)
        response_digest = payload["response_digests"]["premium_index"]
        _require(
            state_scope == asdict(key)
            and key.identity == proof["state_id"]
            and payload["account_scope"] == frozen["scope"]
            and payload["symbol"] == frozen["symbol"] == state["symbol"]
            and payload["market_sources"]["contract_environment"] == "SANDBOX"
            and state["public_quote_contract"] == SHADOW_QUOTE_CONTRACT
            and observed == proof["observed_at_ms"] == state["observed_at_ms"]
            and 0 <= finished - observed <= 15000
            and finished
            <= frozen["context_assembled_at_ms"]
            <= frozen["original_decision_at_ms"]
            and 0 <= frozen["original_decision_at_ms"] - observed < 15000
            and _decimal(
                payload["sizing_budget_inputs"]["sizing_mark_price"],
                "0.000000000001",
                "1000000000",
            )
            == proof["price"]
            == state["price"]
            and type(response_digest) is str
            and len(response_digest) == 64
            and all(char in "0123456789abcdef" for char in response_digest)
        )
        if state["funding_rate"] is not None:
            _require(
                state["funding_observed_at_ms"] == observed
                and state["funding_rate"]
                == _decimal(payload["funding_rate"], "-1", "1")
            )
        return {
            "status": "VERIFIED",
            "state_id": proof["state_id"],
            "version": 1,
            "account_state_digest": digest(canonical(payload)),
            "premium_index_response_digest": response_digest,
        }

    def _process(self, identity, encoded):
        """Worker-only. Every database operation owns its bounded transaction."""
        _require(digest(encoded) == identity)
        frozen = json.loads(encoded)
        _require(frozen["scope"] == asdict(self.scope))
        prepared = prepare_request(
            frozen["request"]["state"],
            now_ms=frozen["submitted_at_ms"],
            max_age_ms=frozen["max_age_ms"],
        )
        _require(
            prepared["request_key"] == frozen["request_key"]
            and prepared["request"] == frozen["request"]
        )
        binding_key = self._state_key(BINDING_NAMESPACE, identity)
        result_key = self._state_key(RESULT_NAMESPACE, identity)
        with self.connect() as conn, conn.transaction():
            _limit_transaction(conn)
            store, now = BusinessState(lambda: nullcontext(conn)), _clock(conn)
            control = _snapshot(store, CONTROL_KEY)
            try:
                quote_verification = self._verify_public_quote(conn, frozen)
            except (KeyError, TypeError, ValueError):
                quote_verification = {"status": "INVALID"}
            quote_valid = (
                quote_verification is None or quote_verification["status"] == "VERIFIED"
            )
            prior = _snapshot(store, binding_key)
            if prior is None:
                binding = {
                    "schema": "jev-shadow-binding-v1",
                    "candidate": frozen,
                    "model_control": control,
                    "public_quote_verification": quote_verification,
                }
                _put_once(store, binding_key, binding)
            else:
                binding = prior["values"]
                _require(prior["version"] == 1 and binding["candidate"] == frozen)
                quote_valid = (
                    quote_valid
                    and binding.get("public_quote_verification") == quote_verification
                )
            done = _snapshot(store, result_key)
            if done is not None:
                _require(
                    done["version"] == 1
                    and done["values"]["binding_digest"] == digest(canonical(binding))
                )
                return
            policy_unchanged = control == binding[
                "model_control"
            ] and self._runtime_matches(store, frozen)
        policy = AdvisoryPolicy(**control["values"]) if control else AdvisoryPolicy()
        status, reason = "MISSING", "MODEL_RESULT_MISSING"
        evaluated = None
        if not quote_valid:
            status, reason = "MISSING", "PUBLIC_QUOTE_EVIDENCE_INVALID"
        elif not policy_unchanged:
            status, reason = "POLICY_CHANGED", "FROZEN_POLICY_CHANGED"
        elif not policy.enabled or self.stop.is_set():
            status, reason = (
                "DISABLED",
                "MODEL_DISABLED" if not policy.enabled else "STOPPED",
            )
        elif not frozen["submitted_at_ms"] <= now < frozen["deadline_ms"]:
            status, reason = "LATE", "SHADOW_DEADLINE_EXPIRED"
        else:
            remaining = frozen["deadline_ms"] - max(now, self.clock())
            if remaining < 100:
                status, reason = "LATE", "SHADOW_DEADLINE_EXPIRED"
            else:
                effective = replace(
                    policy,
                    timeout_ms=min(policy.timeout_ms, remaining),
                    max_age_ms=min(policy.max_age_ms, frozen["max_age_ms"]),
                    cache_ttl_ms=min(policy.cache_ttl_ms, frozen["max_age_ms"]),
                )
                advisor = self.reference.advisor(effective)
                evaluated = JevAdvisory(
                    _FrozenDeadlineCoordinator(advisor.coordinator, frozen),
                    advisor.query,
                    policy=effective,
                    clock_ms=advisor.clock,
                ).evaluate(prepared["request"]["state"])
        with self.connect() as conn, conn.transaction():
            _limit_transaction(conn)
            store, recorded_at = BusinessState(lambda: nullcontext(conn)), _clock(conn)
            shared_key = StateKey(
                **asdict(PUBLIC_SCOPE),
                namespace=REQUEST_NAMESPACE,
                key=frozen["request_key"],
            )
            shared = _snapshot(store, shared_key) if evaluated is not None else None
            result, clocks = None, None
            if evaluated is not None:
                try:
                    status, reason, result, clocks = self._classify(
                        shared, prepared, frozen, policy, recorded_at
                    )
                except (KeyError, TypeError, ValueError):
                    status, reason, result, clocks = (
                        "INVALID_RESULT",
                        "MODEL_RESULT_IDENTITY_INVALID",
                        None,
                        None,
                    )
                if _snapshot(
                    store, CONTROL_KEY
                ) != control or not self._runtime_matches(store, frozen):
                    status, reason = "POLICY_CHANGED", "MODEL_OR_RUNTIME_POLICY_CHANGED"
                elif status == "MISSING" and type(evaluated) is dict:
                    # Fixed internal advisory codes only; provider bodies and
                    # arbitrary messages are never retained as research facts.
                    known = {
                        "BUSY",
                        "BUDGET_EXHAUSTED",
                        "TOKEN_BUDGET_PACED",
                        "COORDINATOR_UNAVAILABLE",
                        "PUBLICATION_UNAVAILABLE",
                        "INVALID_OR_STALE_MARKET",
                        "CLOCK_INVALID",
                    }
                    if evaluated.get("reason") in known:
                        reason = evaluated["reason"]
            payload = {
                "schema": "jev-shadow-result-v1",
                "binding_state_id": binding_key.identity,
                "binding_version": 1,
                "binding_digest": digest(canonical(binding)),
                "signal_id": frozen["signal_id"],
                "consumer": frozen["consumer"],
                "candidate_digest": frozen["candidate_digest"],
                "request_key": frozen["request_key"],
                "status": status,
                "reason": reason,
                "coverage": "OBSERVED" if status == "OBSERVED" else "MISSING",
                "original_decision_at_ms": frozen["original_decision_at_ms"],
                "deadline_ms": frozen["deadline_ms"],
                "recorded_at_ms": recorded_at,
                "shared_result_state_id": shared_key.identity if shared else None,
                "shared_result_version": shared["version"] if shared else None,
                "model_result": result,
                "model_clocks": clocks,
                "replay_semantics": "RESEARCH_ONLY_NOT_HISTORICAL_AUTHORIZATION",
                "available_at_original_decision": bool(
                    clocks
                    and clocks["completed_at_ms"] <= frozen["original_decision_at_ms"]
                ),
                "trade_link_status": "NOT_PROVEN_EXECUTED",
                **_NEUTRAL,
            }
            _put_once(store, result_key, payload)

    @staticmethod
    def _classify(shared, prepared, frozen, policy, now):
        if shared is None:
            return "MISSING", "MODEL_RESULT_MISSING", None, None
        record = shared["values"]
        _require(
            record.get("schema") == "jev-pg-coordinator-v1"
            and record.get("request") == prepared["request"]
            and record.get("reserved_input_tokens") == RESERVE_TOKENS
            and str(UUID(record["claim_id"])) == record["claim_id"]
        )
        policy_prepared = prepare_request(
            prepared["request"]["state"],
            now_ms=frozen["submitted_at_ms"],
            max_age_ms=min(policy.max_age_ms, frozen["max_age_ms"]),
        )
        prepared = {
            **prepared,
            "valid_until_ms": min(
                prepared["valid_until_ms"],
                policy_prepared["valid_until_ms"],
                _integer(
                    record["valid_until_ms"], prepared["observed_at_ms"] + 1, 2**53 - 1
                ),
            ),
        }
        claimed = _integer(record["claimed_at_ms"], prepared["observed_at_ms"], now)
        deadline = _integer(
            record["deadline_ms"],
            claimed + 1,
            min(claimed + 2000, prepared["valid_until_ms"]),
        )
        if record.get("status") == "CLAIMED":
            return (
                ("LATE" if now >= deadline else "MISSING"),
                "MODEL_RESULT_MISSING",
                None,
                None,
            )
        _require(record.get("status") == "COMPLETED" and shared["version"] == 2)
        completed = _integer(record["completed_at_ms"], claimed, now)
        result = record["result"]
        produced = _integer(result["produced_at_ms"], claimed, completed)
        clocks = {
            "claimed_at_ms": claimed,
            "model_deadline_ms": deadline,
            "produced_at_ms": produced,
            "completed_at_ms": completed,
            "expires_at_ms": result["expires_at_ms"],
        }
        if result.get("status") == "ADVISORY_READY":
            checked = _cached(result, prepared, produced, policy.cache_ttl_ms)
            _require(
                produced < deadline
                and record["charged_input_tokens"] == checked["usage"]["input_tokens"]
            )
            if (
                completed >= frozen["deadline_ms"]
                or now >= frozen["deadline_ms"]
                or result["expires_at_ms"] <= frozen["original_decision_at_ms"]
            ):
                return "LATE", "MODEL_OUTSIDE_FROZEN_WINDOW", checked, clocks
            return "OBSERVED", "MODEL_OBSERVED", checked, clocks
        _require(
            result["reason"] in _FALLBACK_REASONS
            and result
            == _result(
                "FALLBACK",
                result["reason"],
                prepared,
                now_ms=produced,
                expires_at_ms=result["expires_at_ms"],
            )
        )
        if result["reason"] in {"MODEL_LATE_RESPONSE", "DEADLINE_EXPIRED"}:
            return "LATE", result["reason"], result, clocks
        return "MISSING", result["reason"], result, clocks
