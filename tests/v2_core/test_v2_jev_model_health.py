"""No network: bounded diagnostic projection, point reads and incident recovery."""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from services import v2_jev_budget_health as health
from services.v2_jev_coordinator import (
    DAY_NAMESPACE,
    _budget,
    _budget_key,
    _clock,
    _window_starts,
)
from services.v2_jev_entry_context import policy_key
from services.v2_jev_reference import CONTROL_KEY
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.jev_advisory import AdvisoryPolicy
from v2_core.jev_entry_gate import JevEntryGatePolicy
from v2_core.jev_health import (
    MAX_RECENT_RESULTS,
    MODEL_HEALTH_KEY,
    MODEL_HEALTH_POLICY_KEY,
    fold_model_health,
    model_health_diagnostic,
    model_health_policy,
)
from v2_core.state import BusinessState

database = database_fixture

NOW = 1793924160000
SCOPE = AccountScope("BINANCE", "qa-model-health", "SANDBOX", "FUTURES")


def fold(previous=None, *, at=NOW, success=False, reason=None):
    return fold_model_health(
        previous,
        status="ADVISORY_READY" if success else "FALLBACK",
        reason=reason
        or ("MODEL_OBSERVED" if success else "MODEL_LOCAL_STARTUP_UNAVAILABLE"),
        completed_at_ms=at,
    )


def failures(count=3):
    value = None
    for i in range(count):
        value = fold(value, at=NOW - (count - i - 1) * 1000)
    return value


def diagnostic(value=None, **kwargs):
    return model_health_diagnostic(value or failures(), now_ms=NOW, **kwargs)


def previous_incident():
    findings = {}
    health.apply_model_health(findings, {}, diagnostic())
    return {"active": [health.MODEL_CODE], "findings": findings}


def test_projection_is_pure_bounded_and_keeps_last_success_after_trimming():
    value = fold(at=NOW - 100000, success=True)
    original = deepcopy(value)
    for i in range(50):
        value = fold(value, at=NOW - (49 - i) * 1000)
    assert len(value["recent_results"]) == MAX_RECENT_RESULTS
    assert value["last_success_at_ms"] == original["last_success_at_ms"]
    assert value["consecutive_failures"] == 50
    assert original["recent_results"] == [
        {
            "completed_at_ms": NOW - 100000,
            "status": "ADVISORY_READY",
            "reason": "MODEL_OBSERVED",
        }
    ]
    assert diagnostic(value)["status"] == "DEGRADED"
    assert value["execution_authorized"] is False


def test_fold_does_not_share_mutable_result_rows_with_previous_snapshot():
    previous = fold(at=NOW - 1000)
    value = fold(previous)
    value["recent_results"][0]["reason"] = "MODEL_TIMEOUT"
    assert previous["recent_results"][0]["reason"] == "MODEL_LOCAL_STARTUP_UNAVAILABLE"


@pytest.mark.parametrize(
    "reason",
    [
        "MODEL_UNKNOWN_FAILURE",
        "MODEL_LOCAL_STARTUP_UNAVAILABLE",
        "MODEL_TIMEOUT",
        "PROVIDER_REQUEST_REJECTED",
        "MODEL_RESPONSE_INVALID",
        "DEADLINE_EXPIRED",
        "MODEL_UNAVAILABLE",
        "MODEL_LATE_RESPONSE",
        "MODEL_CONFIGURATION_UNAVAILABLE",
        "TOKEN_RESERVATION_EXCEEDED",
    ],
)
def test_only_normalized_completion_reasons_are_retained(reason):
    result = diagnostic(fold(reason=reason))
    assert result["status"] == "WATCHING" and result["latest_reason"] == reason
    assert result["last_success_at_ms"] is None


@pytest.mark.parametrize(
    "status,reason",
    [
        ("FALLBACK", "TOKEN_BUDGET_PACED"),
        ("CLAIMED", "MODEL_IN_FLIGHT"),
        ("FALLBACK", "SECRET_PROVIDER_BODY"),
        ("ADVISORY_READY", "MODEL_UNKNOWN_FAILURE"),
        ("FALLBACK", "MODEL_OBSERVED"),
        ("FALLBACK", []),
    ],
)
def test_unclaimed_budget_failures_and_untrusted_errors_are_not_model_results(
    status, reason
):
    with pytest.raises(ValueError):
        fold_model_health(None, status=status, reason=reason, completed_at_ms=NOW)


@pytest.mark.parametrize("bad", [-1, True, "1", None, 2**53])
def test_bad_timestamps_cannot_create_or_clear_health(bad):
    with pytest.raises(ValueError):
        fold(at=bad)


def test_clock_regression_future_or_corrupt_projection_is_not_reset():
    with pytest.raises(ValueError, match="REGRESSED"):
        fold(failures(), at=NOW - 1, success=True)
    with pytest.raises(ValueError, match="FUTURE"):
        model_health_diagnostic(failures(), now_ms=NOW - 1)
    with pytest.raises(ValueError, match="SNAPSHOT"):
        fold({"raw_error": "SECRET"})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.update(consecutive_failures=0),
        lambda v: v.update(consecutive_failures=1000000),
        lambda v: v.update(last_failure_at_ms=None),
        lambda v: v.update(last_success_at_ms=NOW + 1),
        lambda v: v.update(observed_at_ms=NOW + 1),
        lambda v: v.update(recent_results=[]),
        lambda v: v.update(execution_authorized=True),
        lambda v: v["recent_results"][0].update(raw_prompt="SECRET"),
        lambda v: v["recent_results"][0].update(completed_at_ms=NOW + 1),
    ],
)
def test_inconsistent_projection_is_rejected(mutation):
    value = failures()
    mutation(value)
    with pytest.raises(ValueError):
        diagnostic(value)


def test_no_requests_is_not_model_success_or_alarm():
    assert model_health_diagnostic(None, now_ms=NOW)["status"] == "NO_OBSERVATIONS"
    stale = model_health_diagnostic(failures(), now_ms=NOW + 1800000)
    assert stale["status"] == "IDLE" and stale["recent_requests"] == 0
    findings = {}
    health.apply_model_health(findings, {}, stale)
    assert findings == {}


def test_sparse_but_consecutive_recent_failure_still_triggers():
    value = fold(at=NOW - 4000000)
    value = fold(value, at=NOW - 2000000)
    value = fold(value, at=NOW)
    result = diagnostic(value)
    assert result["status"] == "DEGRADED"
    assert result["recent_requests"] == 1
    assert result["consecutive_failures"] == 3


def test_thresholds_are_independent_configurable_diagnostics():
    assert diagnostic(failures(2))["status"] == "WATCHING"
    assert diagnostic(failures(3))["status"] == "DEGRADED"
    result = diagnostic(failures(3), failure_threshold=4, minimum_recent_requests=4)
    assert result["status"] == "WATCHING"
    result = diagnostic(failures(3), failure_threshold=4)
    assert result["reason"] == "RECENT_REQUESTS_WITHOUT_SUCCESS"
    assert model_health_policy({"failure_threshold": 7})["failure_threshold"] == 7
    assert model_health_policy(None)["activity_window_ms"] == 1800000


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"failure_threshold": True},
        {"failure_threshold": 1},
        {"failure_threshold": 21},
        {"activity_window_ms": 59999},
        {"activity_window_ms": 86400001},
        {"minimum_recent_requests": 0},
        {"minimum_recent_requests": 21},
        {"tokens_per_day": 1000000},
        {"arbitrary": "SECRET"},
    ],
)
def test_invalid_diagnostic_config_is_not_silently_defaulted(payload):
    with pytest.raises(ValueError):
        model_health_policy(payload)


@pytest.mark.parametrize(
    "status",
    ["UNKNOWN", "NO_OBSERVATIONS", "IDLE", "WATCHING", "DISABLED", "NOT_CONFIGURED"],
)
def test_no_observations_or_disabled_control_never_fabricates_recovery(status):
    findings = {"POSITION_SAFETY_BLOCKED": {"keep": True}}
    prior = previous_incident()
    health.apply_model_health(findings, prior, {"status": status})
    assert findings[health.MODEL_CODE]["awaiting_fresh_success"] is True
    assert findings[health.MODEL_CODE]["last_failure_at_ms"] == NOW
    assert findings["POSITION_SAFETY_BLOCKED"] == {"keep": True}


def test_new_success_resets_failures_and_clears_only_model_incident():
    value = fold(failures(), at=NOW + 1000, success=True)
    result = model_health_diagnostic(value, now_ms=NOW + 1000)
    assert result["status"] == "HEALTHY" and result["consecutive_failures"] == 0
    findings = {health.CODE: {"keep": True}, "POSITION_SAFETY_BLOCKED": {"keep": True}}
    health.apply_model_health(findings, previous_incident(), result)
    assert health.MODEL_CODE not in findings
    assert len(findings) == 2


def test_older_success_does_not_clear_a_newer_incident():
    result = diagnostic(fold(at=NOW - 1, success=True))
    findings = {}
    health.apply_model_health(findings, previous_incident(), result)
    assert findings[health.MODEL_CODE]["awaiting_fresh_success"] is True


def test_model_fault_and_budget_available_are_distinct_and_no_prompts_are_copied():
    findings = {}
    result = diagnostic()
    result["prompt"] = "SECRET"
    budget = {"status": "AVAILABLE", "model_health": result}
    assert health.apply_budget_health(findings, {}, budget) == budget
    assert health.MODEL_CODE in findings and health.CODE not in findings
    assert "SECRET" not in str(findings)


class Rows:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.states, self.queries = {}, []
        self.fail_on = None

    def transaction(self):
        return nullcontext(self)

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if query.startswith("SET "):
            return Rows(None)
        if "extract(epoch FROM clock_timestamp())" in query:
            return Rows((NOW,))
        assert (
            query
            == "SELECT version,payload,deleted,scope FROM v2_business_state WHERE state_id=%s"
        )
        assert len(params) == 1
        if params[0] == self.fail_on:
            raise ConnectionError("SECRET_DSN")
        return Rows(self.states.get(params[0]))

    def put(self, key, value):
        self.states[key.identity] = (1, value, False, asdict(key))


def connection():
    conn = Connection()
    conn.put(
        policy_key(SCOPE),
        asdict(JevEntryGatePolicy(enabled=True, unavailable_action="BASELINE")),
    )
    conn.put(CONTROL_KEY, asdict(AdvisoryPolicy(enabled=True)))
    _, day = _window_starts(NOW)
    conn.put(
        _budget_key(DAY_NAMESPACE, day),
        _budget(None, kind="utc8-day", start=day, limit=500000),
    )
    return conn


def test_reader_uses_only_fixed_primary_keys_and_preserves_model_budget_distinction():
    conn = connection()
    conn.put(MODEL_HEALTH_KEY, failures())
    before = deepcopy(conn.states)
    result = health.read_budget_health(lambda: nullcontext(conn), SCOPE)
    assert result["status"] == "AVAILABLE"
    assert result["model_health"]["status"] == "DEGRADED"
    ids = [params[0] for _, params in conn.queries if params]
    assert len(ids) == 5 and ids[-2:] == [
        MODEL_HEALTH_POLICY_KEY.identity,
        MODEL_HEALTH_KEY.identity,
    ]
    assert conn.states == before
    assert any(
        query == "SET LOCAL statement_timeout='100ms'" for query, _ in conn.queries
    )
    assert any(query == "SET LOCAL lock_timeout='50ms'" for query, _ in conn.queries)


def test_dynamic_health_policy_point_read_and_invalid_config_are_independent_of_budget():
    conn = connection()
    conn.put(MODEL_HEALTH_KEY, failures())
    conn.put(
        MODEL_HEALTH_POLICY_KEY, {"failure_threshold": 4, "minimum_recent_requests": 4}
    )
    assert (
        health.read_budget_health(lambda: nullcontext(conn), SCOPE)["model_health"][
            "status"
        ]
        == "WATCHING"
    )
    conn.put(MODEL_HEALTH_POLICY_KEY, {"failure_threshold": True})
    result = health.read_budget_health(lambda: nullcontext(conn), SCOPE)
    assert result["status"] == "AVAILABLE" and result["model_health"] == {
        "status": "UNKNOWN"
    }


def test_model_read_failure_retains_previous_alarm_without_erasing_budget():
    conn = connection()
    conn.fail_on = MODEL_HEALTH_KEY.identity
    result = health.read_budget_health(lambda: nullcontext(conn), SCOPE)
    assert result["status"] == "AVAILABLE" and result["model_health"] == {
        "status": "UNKNOWN"
    }
    findings = {}
    health.apply_budget_health(findings, previous_incident(), result)
    assert findings[health.MODEL_CODE]["awaiting_fresh_success"] is True
    assert "SECRET" not in str(result)


def test_missing_projection_is_no_observations_not_success_and_scopes_stay_closed():
    conn = connection()
    result = health.read_budget_health(lambda: nullcontext(conn), SCOPE)
    assert result["model_health"]["status"] == "NO_OBSERVATIONS"
    assert health.read_budget_health(
        lambda: nullcontext(conn), replace(SCOPE, account_id="other")
    ) == {"status": "NOT_CONFIGURED"}
    with pytest.raises(ValueError):
        health.read_budget_health(
            lambda: nullcontext(conn), replace(SCOPE, environment="LIVE")
        )


def test_real_pg_projection_reader_remains_read_only_and_config_is_independent(
    database,
):
    from test_v2_jev_budget_health import SCOPE as DB_SCOPE
    from test_v2_jev_budget_health import setup_budget, write

    budget_key = setup_budget(database, used=1000)
    with database() as conn:
        now = _clock(conn)
    projection = None
    for i in range(3):
        projection = fold(projection, at=now - (2 - i) * 1000)
    write(database, MODEL_HEALTH_KEY, projection)
    store = BusinessState(database)
    before = store.read(MODEL_HEALTH_KEY), store.read(budget_key)
    result = health.read_budget_health(database, DB_SCOPE)
    assert result["status"] == "AVAILABLE"
    assert result["model_health"]["status"] == "DEGRADED"
    assert (store.read(MODEL_HEALTH_KEY), store.read(budget_key)) == before
    write(database, MODEL_HEALTH_POLICY_KEY, {"failure_threshold": "SECRET"})
    result = health.read_budget_health(database, DB_SCOPE)
    assert result["status"] == "AVAILABLE"
    assert result["model_health"] == {"status": "UNKNOWN"}
    assert "SECRET" not in str(result)


def test_real_pg_failed_model_select_savepoint_preserves_budget(database, monkeypatch):
    from test_v2_jev_budget_health import SCOPE as DB_SCOPE
    from test_v2_jev_budget_health import setup_budget

    setup_budget(database, used=1000)
    original = health._read

    def unreadable(store, key):
        if key == MODEL_HEALTH_KEY:
            # A real SQL error aborts its transaction. The optional health
            # savepoint must isolate it from the preceding quota observation.
            with store._connect() as conn:
                conn.execute("SELECT 1/0")
        return original(store, key)

    monkeypatch.setattr(health, "_read", unreadable)
    result = health.read_budget_health(database, DB_SCOPE)
    assert result["status"] == "AVAILABLE"
    assert result["model_health"] == {"status": "UNKNOWN"}


@pytest.mark.parametrize("condition", ["fresh", "stale", "wrong_account", "deleted"])
def test_real_business_details_provider_round_trips_model_diagnostic_to_dm(condition):
    from test_v2_position_alert_details import SCOPE as ALERT_SCOPE
    from test_v2_position_alert_details import event, notifier

    from services.v2_trading_health import TradingHealth

    def no_database():
        pytest.fail("the notification projection must use only the fake state store")

    business = TradingHealth(
        no_database, account_id=ALERT_SCOPE.account_id, clock_ms=lambda: NOW
    )
    findings = {}
    health.apply_model_health(findings, {}, diagnostic())
    findings[health.MODEL_CODE]["details"]["provider_body"] = "SECRET_PROVIDER_BODY"
    payload = {
        "account_scope": asdict(ALERT_SCOPE),
        "observed_at_ms": NOW,
        "findings": findings,
    }
    if condition == "stale":
        payload["observed_at_ms"] = NOW - 120001
    elif condition == "wrong_account":
        payload["account_scope"]["account_id"] = "different-account"
    snapshot = SimpleNamespace(
        payload_json=json.dumps(payload), deleted=condition == "deleted"
    )
    reads = []

    def read(key):
        reads.append(key)
        assert key == business.key
        return snapshot

    business.store = SimpleNamespace(read=read)
    notify, tg = notifier(details_for=business.notification_details, now=NOW)
    assert notify(event(health.MODEL_CODE))
    assert reads == [business.key]
    assert len(tg.calls) == 1 and tg.calls[0]["chat_id"] == "321"
    text = tg.calls[0]["text"]
    assert "SECRET" not in text
    if condition == "fresh":
        assert "连续失败：3 次" in text
        assert "3 / 3 次失败" in text
        assert "本地调用进程启动失败" in text
        assert "暂无已记录的成功结果" in text
        assert "详情：暂时无法读取" not in text
    elif condition == "stale":
        assert "模型诊断已过期" in text and "连续失败：" not in text
    else:
        assert "详情：暂时无法读取" in text and "连续失败：" not in text
