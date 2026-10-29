"""Offline candidate binding tests; real PG cases use disposable QA only."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_jev_coordinator import MemoryDatabase, MemoryState

from services import v2_jev_coordinator as coordination
from services import v2_jev_entry_context as subject
from services.v2_jev_reference import CONTROL_KEY, PUBLIC_SCOPE
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.jev_advisory import (
    AdvisoryPolicy,
    JevAdvisory,
    _result,
    parse_response,
    prepare_request,
)
from v2_core.jev_entry_gate import JevEntryGatePolicy
from v2_core.producer import market_envelope
from v2_core.state import BusinessState, StateKey, StateSnapshot
from v2_core.strategy import StrategyDecision, StrategyScope

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-entry-gate", "SANDBOX", "FUTURES")
POLICY = JevEntryGatePolicy(enabled=True, max_wait_ms=30000)


def admission_case(now):
    return {
        "decision": StrategyDecision(
            "OPEN",
            "DIRECTIONAL_MARKET_GATES_PASSED",
            side="BUY",
            quantity="1.25",
            features_json=canonical(
                {"admission_authorized": True, "execution_authorized": False}
            ),
        ),
        "signal_id": str(uuid4()),
        "consumer": StrategyScope(**asdict(SCOPE), producer="s6").consumer,
        "signal": {
            "symbol": "BTCUSDT",
            "observed_at": now,
            "expires_at_ms": now + 60000,
            "signal": "PUMP_START",
            "features": {"strength": 90},
        },
        "context": {
            "account_scope": asdict(SCOPE),
            "environment": "SANDBOX",
            "symbol": "BTCUSDT",
            "assembled_at": now,
            "valid_until_ms": now + 15000,
            "sources": {
                "s3": market_envelope(
                    "s3",
                    "SANDBOX",
                    "BTCUSDT",
                    now,
                    {
                        "15m": {
                            "close": "100",
                            "ema20": "99",
                            "rsi": "65",
                            "atr_pct": "1.5",
                        },
                        "1h": {"close": "100", "ema20": "98", "chg": "2"},
                    },
                )
            },
        },
        "config": {
            "strategy": "S6",
            "source": "s3",
            "account_scope": asdict(SCOPE),
            "mode": "TESTNET_ADMISSION_ONLY",
        },
    }


def refresh(case, now, *, new_market=False):
    case["context"]["assembled_at"] = now
    case["context"]["valid_until_ms"] = now + 15000
    if new_market:
        previous = case["context"]["sources"]["s3"]
        frames = deepcopy(previous["features"])
        frames["15m"]["close"] = "101"
        case["context"]["sources"]["s3"] = market_envelope(
            "s3", "SANDBOX", "BTCUSDT", now, frames
        )


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(subject, "BusinessState", MemoryState)
    monkeypatch.setattr(coordination, "BusinessState", MemoryState)
    db = MemoryDatabase()
    db.rows[CONTROL_KEY.identity] = StateSnapshot(
        1, canonical(asdict(AdvisoryPolicy(enabled=True))), False
    )
    queued = []

    def submit(features):
        assert db.active_statements is None, "model work enqueued before binding commit"
        queued.append(deepcopy(features))

    reference = SimpleNamespace(
        submit=submit,
        lookup=lambda *_: pytest.fail("5s in-memory cache is not entry authority"),
    )
    service = subject.JevEntryContext(db.connect, reference, scope=SCOPE, policy=POLICY)
    return SimpleNamespace(
        db=db,
        queued=queued,
        reference=reference,
        service=service,
        args=admission_case(db.now),
    )


def binding(case):
    key = case.service.binding_key(case.args["consumer"], case.args["signal_id"])
    row = case.db.rows[key.identity]
    return key, row, json.loads(row.payload_json)


def model_response(*, trend="0.8", chase="0.2"):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "trend_supported": {"type": "noul", "noul": trend},
            "chase_risk": {"type": "noul", "noul": chase},
            "liquidity_fragile": {"type": "noul", "noul": "1"},
        },
        "usage": {"input_tokens": 900, "output_tokens": 50},
    }


def claim(case):
    _, _, bound = binding(case)
    prepared = prepare_request(
        bound["public_features"], now_ms=case.db.now, max_age_ms=60000
    )
    coordinator = coordination.JevCoordinator(
        case.db.connect, scope=PUBLIC_SCOPE, enabled=True
    )
    grant = coordinator.claim(
        request_key=prepared["request_key"],
        request=prepared["request"],
        now_ms=case.db.now,
        deadline_ms=case.db.now + 1500,
    )
    return coordinator, prepared, grant


def publish(case, *, failure=None, trend="0.8", chase="0.2"):
    coordinator, prepared, grant = claim(case)
    assert grant["status"] == "GRANTED"
    case.db.now += 100
    result = _result(
        "FALLBACK" if failure else "ADVISORY_READY",
        failure or "MODEL_OBSERVED",
        prepared,
        now_ms=case.db.now,
        expires_at_ms=case.db.now + 30000,
        response=None
        if failure
        else parse_response(model_response(trend=trend, chase=chase)),
    )
    assert coordinator.complete(
        request_key=prepared["request_key"],
        claim_id=grant["claim_id"],
        result=result,
        now_ms=case.db.now,
        expires_at_ms=result["expires_at_ms"],
        charged_input_tokens=None if failure else 900,
    )
    return result


def assert_skip(result, reason=None):
    assert result.status == "SKIP"
    assert result.decision.action == "IGNORED"
    assert result.decision.side is None and result.decision.quantity is None
    if reason is not None:
        assert result.reason == reason
    assert "SECRET" not in result.evidence_json


def test_first_candidate_commits_binding_before_queue_and_defers(case):
    result = case.service.assess(**case.args)
    assert result.status == "PENDING" and result.decision is None
    _, row, bound = binding(case)
    assert row.version == 1
    assert bound["deadline_ms"] == case.db.now + 30000
    assert bound["deadline_ms"] > case.args["context"]["valid_until_ms"]
    assert len(case.queued) == 1
    assert bound["public_features"] == case.queued[0]
    assert bound["public_features"]["spread_bps"] is None
    assert "account_scope" not in case.queued[0]


def test_model_finished_before_next_ten_second_cycle_can_pass_after_restart(case):
    first = case.db.now
    assert case.service.assess(**case.args).status == "PENDING"
    _, saved, bound = binding(case)
    publish(case)
    case.db.now = first + 20000
    refresh(case.args, case.db.now, new_market=True)
    # Account admission is freshly recalculated outside this service. Its new
    # quantity remains authoritative, while the model's original public input
    # is still within the explicitly bound 30s review window.
    case.args["decision"] = replace(case.args["decision"], quantity="0.9")
    restarted = subject.JevEntryContext(
        case.db.connect, case.reference, scope=SCOPE, policy=POLICY
    )
    result = restarted.assess(**case.args)
    assert result.status == "PASS"
    assert result.decision is case.args["decision"]
    assert result.decision.quantity == "0.9"
    assert binding(case)[1] == saved
    assert binding(case)[2]["request_key"] == bound["request_key"]
    assert len(case.queued) == 1
    evidence = json.loads(result.evidence_json)
    assert evidence["timing"]["started_at_ms"] == first
    assert evidence["timing"]["deadline_ms"] == first + 30000
    assert evidence["binding_version"] == 1


def test_missing_result_cannot_reset_deadline_or_model_snapshot(case):
    first = case.db.now
    case.service.assess(**case.args)
    _, saved, bound = binding(case)
    case.db.now += 12000
    refresh(case.args, case.db.now, new_market=True)
    restarted = subject.JevEntryContext(
        case.db.connect, case.reference, scope=SCOPE, policy=POLICY
    )
    assert restarted.assess(**case.args).status == "PENDING"
    assert case.queued[1] == case.queued[0] == bound["public_features"]
    case.db.now = first + 30000
    refresh(case.args, case.db.now)
    assert_skip(restarted.assess(**case.args), "JEV_REVIEW_DEADLINE_EXPIRED")
    assert len(case.queued) == 2 and binding(case)[1] == saved


def test_late_success_after_review_cutoff_is_skipped(case):
    first = case.db.now
    case.service.assess(**case.args)
    publish(case)
    case.db.now = first + 30000
    refresh(case.args, case.db.now)
    assert_skip(case.service.assess(**case.args), "JEV_REVIEW_DEADLINE_EXPIRED")


@pytest.mark.parametrize("failure", ["MODEL_TIMEOUT", "MODEL_UNAVAILABLE"])
def test_known_model_failure_terminally_skips_this_signal(case, failure):
    case.service.assess(**case.args)
    publish(case, failure=failure)
    case.db.now += 10000
    refresh(case.args, case.db.now)
    assert_skip(case.service.assess(**case.args), "JEV_RESULT_UNAVAILABLE")
    assert len(case.queued) == 1


def test_expired_ambiguous_claim_is_never_resubmitted(case):
    case.service.assess(**case.args)
    _, _, grant = claim(case)
    assert grant["status"] == "GRANTED"
    case.db.now += 10000
    refresh(case.args, case.db.now)
    assert_skip(case.service.assess(**case.args), "JEV_RESULT_UNAVAILABLE")
    assert len(case.queued) == 1


@pytest.mark.parametrize("window", ["minute", "day"])
def test_exhausted_budget_skips_before_enqueuing(case, window):
    minute, day = coordination._window_starts(case.db.now)
    namespace, at, kind, limit = (
        (coordination.MINUTE_NAMESPACE, minute, "minute", 5)
        if window == "minute"
        else (coordination.DAY_NAMESPACE, day, "utc8-day", 500000)
    )
    key = coordination._budget_key(namespace, at)
    payload = coordination._budget(None, kind=kind, start=at, limit=limit)
    payload["used"] = limit
    case.db.rows[key.identity] = StateSnapshot(1, canonical(payload), False)
    assert_skip(case.service.assess(**case.args), "JEV_RESULT_UNAVAILABLE")
    assert case.queued == []


def test_ready_result_does_not_need_new_budget(case):
    case.service.assess(**case.args)
    publish(case)
    minute, _ = coordination._window_starts(case.db.now)
    key = coordination._budget_key(coordination.MINUTE_NAMESPACE, minute)
    payload = json.loads(case.db.rows[key.identity].payload_json)
    payload["used"] = payload["limit"]
    case.db.rows[key.identity] = StateSnapshot(2, canonical(payload), False)
    assert case.service.assess(**case.args).status == "PASS"


def test_fresh_pg_kill_switch_is_required_even_with_cached_success(case):
    case.service.assess(**case.args)
    publish(case)
    case.db.rows[CONTROL_KEY.identity] = StateSnapshot(
        2, canonical(asdict(AdvisoryPolicy(enabled=False))), False
    )
    assert_skip(case.service.assess(**case.args), "JEV_RESULT_UNAVAILABLE")


@pytest.mark.parametrize("mode", ["absent", "deleted", "malformed"])
def test_control_failure_is_not_default_allow(case, mode):
    if mode == "absent":
        del case.db.rows[CONTROL_KEY.identity]
    elif mode == "deleted":
        case.db.rows[CONTROL_KEY.identity] = StateSnapshot(2, "{}", True)
    else:
        case.db.rows[CONTROL_KEY.identity] = StateSnapshot(2, '{"enabled":1}', False)
    assert_skip(case.service.assess(**case.args))
    assert not case.queued


def test_default_disabled_bypasses_only_optional_gate_without_db(case):
    def forbidden():
        pytest.fail("disabled optional gate touched PG")

    service = subject.JevEntryContext(
        forbidden, case.reference, scope=SCOPE, policy=JevEntryGatePolicy()
    )
    result = service.assess(**case.args)
    assert result.status == "PASS" and result.decision is case.args["decision"]
    assert not case.queued


@pytest.mark.parametrize("reason", ["INSUFFICIENT_BUDGET", "ADVERSE_FUNDING"])
def test_risk_reject_never_creates_binding_or_model_work(case, reason):
    original = StrategyDecision("IGNORED", reason)
    case.args["decision"] = original
    case.db.connect = lambda: pytest.fail("risk rejection reached PG")
    result = case.service.assess(**case.args)
    assert result.status == "VETO" and result.decision is original
    assert len(case.db.rows) == 1 and not case.queued


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"admission_authorized": False, "execution_authorized": False},
        {"admission_authorized": True, "execution_authorized": True},
    ],
)
def test_unauthorized_open_never_creates_binding_or_calls_model(case, payload):
    case.args["decision"] = replace(
        case.args["decision"], features_json=canonical(payload)
    )
    assert_skip(case.service.assess(**case.args), "JEV_ORIGINAL_ADMISSION_INVALID")
    assert len(case.db.rows) == 1 and not case.queued


@pytest.mark.parametrize(
    "change",
    ["config", "policy", "model_policy", "signal", "consumer", "account", "direction"],
)
def test_bound_candidate_cannot_be_reinterpreted(case, change):
    case.service.assess(**case.args)
    _, saved, _ = binding(case)
    if change == "config":
        case.args["config"]["policy_version"] = 2
    elif change == "policy":
        case.service = subject.JevEntryContext(
            case.db.connect,
            case.reference,
            scope=SCOPE,
            policy=replace(POLICY, min_trend_support="0.7"),
        )
    elif change == "model_policy":
        case.db.rows[CONTROL_KEY.identity] = StateSnapshot(
            2,
            canonical(asdict(AdvisoryPolicy(enabled=True, requests_per_minute=4))),
            False,
        )
    elif change == "signal":
        case.args["signal"]["expires_at_ms"] += 10000
    elif change == "consumer":
        case.args["consumer"] = "[]"
    elif change == "account":
        case.args["context"]["account_scope"]["account_id"] = "different-account"
    else:
        case.args["decision"] = replace(case.args["decision"], side="SELL")
    assert_skip(case.service.assess(**case.args))
    assert saved in case.db.rows.values()
    assert len(case.queued) == 1


@pytest.mark.parametrize(
    "change", ["deadline", "request_key", "snapshot", "deleted", "version"]
)
def test_binding_tampering_or_tombstone_never_restarts_review(case, change):
    case.service.assess(**case.args)
    key, row, bound = binding(case)
    if change == "deadline":
        bound["deadline_ms"] += 1
    elif change == "request_key":
        bound["request_key"] = "0" * 64
    elif change == "snapshot":
        bound["public_features"]["price"] = "200"
    case.db.rows[key.identity] = StateSnapshot(
        2 if change == "version" else row.version,
        "{}" if change == "deleted" else canonical(bound),
        change == "deleted",
    )
    assert_skip(case.service.assess(**case.args))
    assert len(case.queued) == 1


@pytest.mark.parametrize("change", ["model", "request", "status", "result"])
def test_persisted_result_still_needs_strict_proof_validation(case, change):
    case.service.assess(**case.args)
    publish(case)
    _, _, bound = binding(case)
    key = StateKey(
        **asdict(PUBLIC_SCOPE),
        namespace=coordination.REQUEST_NAMESPACE,
        key=bound["request_key"],
    )
    row = case.db.rows[key.identity]
    record = json.loads(row.payload_json)
    if change == "model":
        record["result"]["model"] = "unapproved-model"
    elif change == "request":
        record["request"]["state"]["symbol"] = "ETHUSDT"
    elif change == "status":
        record["status"] = "APPROVED"
    else:
        record["result"] = None
    case.db.rows[key.identity] = StateSnapshot(row.version, canonical(record), False)
    assert_skip(case.service.assess(**case.args))


def test_model_veto_produces_terminal_ignored_not_scheduler_failure(case):
    case.service.assess(**case.args)
    publish(case, chase="0.9")
    result = case.service.assess(**case.args)
    assert result.status == "VETO" and result.decision.action == "IGNORED"
    assert result.reason == "JEV_CHASE_RISK_HIGH"
    from services.v2_trading_pipeline import failed

    assert not failed({"signal": "DEFERRED"})
    assert not failed({"signal": result.decision.action})


def test_db_failure_does_not_leak_or_default_allow(case):
    def unavailable():
        raise RuntimeError("DATABASE_SECRET")

    case.service.connect = unavailable
    assert_skip(case.service.assess(**case.args), "JEV_REVIEW_UNAVAILABLE")
    assert not case.queued


@pytest.mark.parametrize("age_ms", [60000, 65965, 81467])
def test_fresh_account_does_not_hide_expired_public_model_input(case, age_ms):
    original = case.args["context"]["sources"]["s3"]
    case.args["context"]["sources"]["s3"] = market_envelope(
        "s3", "SANDBOX", "BTCUSDT", case.db.now - age_ms, original["features"]
    )
    assert case.args["context"]["assembled_at"] == case.db.now
    result = case.service.assess(**case.args)
    assert_skip(result, "JEV_MARKET_SNAPSHOT_EXPIRED")
    assert not case.queued
    key = case.service.binding_key(case.args["consumer"], case.args["signal_id"])
    assert key.identity not in case.db.rows
    assert case.args["context"]["sources"]["s3"]["observed_at"] == case.db.now - age_ms


def test_queue_error_is_single_signal_skip_with_binding_preserved(case):
    def unavailable(_):
        raise RuntimeError("QUEUE_SECRET")

    case.reference.submit = unavailable
    assert_skip(case.service.assess(**case.args), "JEV_REVIEW_UNAVAILABLE")
    assert binding(case)[1].version == 1


def test_concurrent_candidates_share_first_binding_and_one_provider_claim(case):
    # Another thread may own the fake DB's global transaction marker while our
    # enqueue is outside its own transaction. The single-call test above proves
    # commit-before-enqueue; do not confuse unrelated transaction ownership.
    case.reference.submit = lambda features: case.queued.append(deepcopy(features))
    other = subject.JevEntryContext(
        case.db.connect, case.reference, scope=SCOPE, policy=POLICY
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda service: service.assess(**case.args), [case.service, other])
        )
    assert all(result.status == "PENDING" for result in results)
    assert binding(case)[1].version == 1
    assert len(case.queued) == 2 and case.queued[0] == case.queued[1]
    assert claim(case)[2]["status"] == "GRANTED"
    assert claim(case)[2]["status"] == "BUSY"


@pytest.mark.parametrize("wait_ms", [5000, 10000, 19999])
def test_enabled_window_must_allow_existing_ten_second_scheduler(case, wait_ms):
    with pytest.raises(ValueError, match="WINDOW_TOO_SHORT"):
        subject.JevEntryContext(
            case.db.connect,
            case.reference,
            scope=SCOPE,
            policy=replace(POLICY, max_wait_ms=wait_ms),
        )


def scoped_policy(case, policy, version=1, *, scope=SCOPE):
    key = subject.policy_key(scope)
    case.db.rows[key.identity] = StateSnapshot(
        version, canonical(asdict(policy)), False
    )
    return key


def dynamic(case, *, scope=SCOPE):
    return subject.JevEntryContext(case.db.connect, case.reference, scope=scope)


@pytest.mark.parametrize("interval,expected", [(15, "PENDING"), (16, "SKIP")])
def test_deployed_review_window_keeps_the_original_thirty_second_bound(
    case, interval, expected
):
    scoped_policy(case, POLICY)
    service = subject.JevEntryContext(
        case.db.connect, case.reference, scope=SCOPE, interval_seconds=interval
    )
    result = service.assess(**case.args)
    assert result.status == expected
    if expected == "SKIP":
        assert result.reason == "JEV_GATE_POLICY_INVALID"
        assert case.queued == []


def test_dynamic_absent_policy_defaults_off_without_provider_control_or_queue(
    case, monkeypatch
):
    accessed = []

    class ObservedState(MemoryState):
        def read(self, key):
            accessed.append(key)
            assert key != CONTROL_KEY, "OFF read provider-wide control"
            return super().read(key)

    monkeypatch.setattr(subject, "BusinessState", ObservedState)
    result = dynamic(case).assess(**case.args)
    assert result.status == "PASS" and result.decision is case.args["decision"]
    assert subject.policy_key(SCOPE) in accessed
    assert json.loads(result.evidence_json)["gate_policy_version"] == 0
    assert not case.queued and len(case.db.rows) == 1


@pytest.mark.parametrize("profile_exists", [False, True])
@pytest.mark.parametrize("missing", ["public_envelope", "all_optional_context"])
def test_dynamic_off_is_transparent_without_jev_specific_context(
    case, monkeypatch, profile_exists, missing
):
    if profile_exists:
        scoped_policy(case, JevEntryGatePolicy())
    if missing == "public_envelope":
        case.args["context"]["sources"]["s3"] = {"snapshot_id": "legacy-valid-source"}
    else:
        case.args["context"] = {}
        case.args["config"] = {}
        case.args["signal"] = {}
        case.args["decision"] = replace(case.args["decision"], features_json="{}")

    def forbidden(*_, **__):
        pytest.fail("disabled gate parsed JEV-specific public features")

    monkeypatch.setattr(subject, "public_features", forbidden)
    result = dynamic(case).assess(**case.args)
    assert result.status == "PASS"
    assert result.reason == "JEV_GATE_DISABLED"
    assert result.decision is case.args["decision"]
    assert not case.queued
    assert not any(
        "clock_timestamp" in query for tx in case.db.transactions for query in tx
    )


def test_enabled_gate_still_rejects_missing_jev_public_envelope(case):
    scoped_policy(case, POLICY)
    case.args["context"]["sources"]["s3"] = {"snapshot_id": "legacy-valid-source"}
    assert_skip(dynamic(case).assess(**case.args))
    assert not case.queued


def test_dynamic_enable_then_disable_does_not_reopen_pending_signal(case):
    service = dynamic(case)
    assert service.assess(**case.args).status == "PASS"
    scoped_policy(case, POLICY)
    assert service.assess(**case.args).status == "PENDING"
    _, saved, _ = binding(case)
    scoped_policy(case, JevEntryGatePolicy(), version=2)
    assert_skip(service.assess(**case.args), "JEV_CANDIDATE_POLICY_CHANGED")
    assert binding(case)[1] == saved and len(case.queued) == 1
    new_signal = deepcopy(case.args)
    new_signal["signal_id"] = str(uuid4())
    assert service.assess(**new_signal).status == "PASS"
    assert len(case.queued) == 1


def test_dynamic_enabled_policy_survives_restart_and_preserves_version(case):
    scoped_policy(case, POLICY, version=4)
    assert dynamic(case).assess(**case.args).status == "PENDING"
    publish(case)
    case.db.now += 10000
    refresh(case.args, case.db.now, new_market=True)
    result = dynamic(case).assess(**case.args)
    assert result.status == "PASS"
    assert json.loads(result.evidence_json)["gate_policy_version"] == 4
    assert json.loads(result.evidence_json)["gate_policy_source"] == "ACCOUNT_PG"


@pytest.mark.parametrize("change_values", [True, False])
def test_dynamic_profile_revision_change_terminally_retires_binding(
    case, change_values
):
    scoped_policy(case, POLICY)
    assert dynamic(case).assess(**case.args).status == "PENDING"
    _, saved, bound = binding(case)
    scoped_policy(
        case,
        replace(POLICY, min_trend_support="0.7") if change_values else POLICY,
        version=2,
    )
    assert_skip(dynamic(case).assess(**case.args), "JEV_CANDIDATE_BINDING_INVALID")
    assert binding(case)[1] == saved
    assert binding(case)[2]["deadline_ms"] == bound["deadline_ms"]
    assert len(case.queued) == 1


@pytest.mark.parametrize("mode", ["malformed", "tombstone", "too_short", "short_ttl"])
def test_invalid_dynamic_policy_is_not_silently_disabled(case, mode):
    key = scoped_policy(case, POLICY)
    payload = asdict(POLICY)
    if mode == "malformed":
        payload["enabled"] = "true"
    elif mode == "too_short":
        payload["max_wait_ms"] = 5000
    elif mode == "short_ttl":
        payload["cache_ttl_ms"] = 1000
    case.db.rows[key.identity] = StateSnapshot(
        2, canonical(payload), mode == "tombstone"
    )
    assert_skip(dynamic(case).assess(**case.args), "JEV_GATE_POLICY_INVALID")
    assert not case.queued


def test_provider_result_ttl_must_span_the_configured_review_window(case):
    case.db.rows[CONTROL_KEY.identity] = StateSnapshot(
        2,
        canonical(asdict(AdvisoryPolicy(enabled=True, cache_ttl_ms=5000))),
        False,
    )
    assert_skip(case.service.assess(**case.args), "JEV_MODEL_TTL_TOO_SHORT")
    assert not case.queued


def test_explicit_override_also_rejects_too_short_model_cache(case):
    with pytest.raises(ValueError, match="TTL_SHORTER_THAN_REVIEW"):
        subject.JevEntryContext(
            case.db.connect,
            case.reference,
            scope=SCOPE,
            policy=replace(POLICY, cache_ttl_ms=1000),
        )


def test_dynamic_gate_policy_is_account_scoped_not_global_provider_policy(case):
    scoped_policy(case, POLICY)
    assert dynamic(case).assess(**case.args).status == "PENDING"
    publish(case)
    other_scope = replace(SCOPE, account_id="qa-entry-gate-b")
    other = deepcopy(case.args)
    other["signal_id"] = str(uuid4())
    other["consumer"] = StrategyScope(**asdict(other_scope), producer="s6").consumer
    other["config"]["account_scope"] = asdict(other_scope)
    other["context"]["account_scope"] = asdict(other_scope)
    service_b = dynamic(case, scope=other_scope)
    assert service_b.assess(**other).status == "PASS"
    scoped_policy(case, replace(POLICY, min_trend_support="0.9"), scope=other_scope)
    result_b = service_b.assess(**other)
    assert result_b.status == "VETO" and result_b.reason == "JEV_TREND_UNSUPPORTED"
    result_a = dynamic(case).assess(**case.args)
    assert result_a.status == "PASS"
    assert len(case.queued) == 1


def test_pg_binding_model_result_and_restart_use_existing_tables_only(database):
    now = time.time_ns() // 1000000
    args = admission_case(now)
    store = BusinessState(database)
    assert (
        store.change(
            CONTROL_KEY,
            expected_version=0,
            request_key=str(uuid4()),
            payload=asdict(AdvisoryPolicy(enabled=True)),
            reason="QA enable shared model reference",
        ).code
        == "APPLIED"
    )
    assert (
        store.change(
            subject.policy_key(SCOPE),
            expected_version=0,
            request_key=str(uuid4()),
            payload=asdict(POLICY),
            reason="QA enable account-scoped JEV entry filter",
        ).code
        == "APPLIED"
    )
    submitted = []
    reference = SimpleNamespace(
        submit=lambda features: submitted.append(deepcopy(features))
    )
    service = subject.JevEntryContext(database, reference, scope=SCOPE)
    assert service.assess(**args).status == "PENDING"
    key = service.binding_key(args["consumer"], args["signal_id"])
    original = store.read(key)
    calls = []

    def local_only_query(request, *, timeout_ms):
        calls.append(request)
        assert 0 < timeout_ms <= 1500
        return model_response()

    coordinator = coordination.JevCoordinator(
        database, scope=PUBLIC_SCOPE, enabled=True
    )
    advisory = JevAdvisory(
        coordinator, local_only_query, policy=AdvisoryPolicy(enabled=True)
    )
    assert advisory.evaluate(submitted[0])["status"] == "ADVISORY_READY"
    restarted = subject.JevEntryContext(database, reference, scope=SCOPE)
    result = restarted.assess(**args)
    assert result.status == "PASS" and result.decision is args["decision"]
    assert store.read(key) == original
    assert len(calls) == len(submitted) == 1
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
