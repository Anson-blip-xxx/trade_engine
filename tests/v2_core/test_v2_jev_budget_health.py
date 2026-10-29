"""Read-only real-PG quota checks and fake-only Telegram notifications."""

from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_position_alert_details import event, notifier
from test_v2_testnet_watchdog import Stop

from services import v2_jev_budget_health as subject
from services.v2_jev_coordinator import (
    DAY_NAMESPACE,
    _budget,
    _budget_key,
    _clock,
    _window_starts,
)
from services.v2_jev_entry_context import policy_key
from services.v2_jev_reference import CONTROL_KEY
from services.v2_testnet_watchdog import HealthWatchdog
from v2_core.account_risk import AccountScope
from v2_core.jev_advisory import RESERVE_TOKENS, AdvisoryPolicy
from v2_core.jev_entry_gate import JevEntryGatePolicy
from v2_core.state import BusinessState
from v2_core.telegram import health_notification_details

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-jev-health", "SANDBOX", "FUTURES")
DAY = 1792771200000  # 2026-10-24 00:00 UTC+8


def diagnostic(**changes):
    return {
        "status": "EXHAUSTED",
        "observed_at_ms": DAY + 1000,
        "day_start_ms": DAY,
        "reset_at_ms": DAY + 86400000,
        "used_tokens": 492241,
        "limit_tokens": 500000,
        "reservation_tokens": RESERVE_TOKENS,
        "unavailable_action": "BASELINE",
        **changes,
    }


@pytest.mark.parametrize("action", ["BASELINE", "SKIP"])
def test_dm_dedup_and_explicit_local_budget_not_provider_balance(action):
    data = diagnostic(
        unavailable_action=action, status="CURRENT", secret="DO_NOT_RENDER"
    )
    notify, connection = notifier(details_for=lambda _: data, now=DAY + 1000)
    active = {subject.CODE}
    watchdog = HealthWatchdog(
        lambda: frozenset(active),
        notify=notify,
        stop=Stop(),
        interval_seconds=10,
    )
    for _ in range(5):
        watchdog.run_once()
    assert len(connection.calls) == 1
    sent = connection.calls[0]
    assert sent["chat_id"] == "321"
    assert "492241 / 500000" in sent["text"]
    assert "8192" in sent["text"] and "UTC+8" in sent["text"]
    assert "不代表供应商余额不足" in sent["text"]
    assert "不记为模型通过" in sent["text"] and "DO_NOT_RENDER" not in sent["text"]
    assert ("可走 BASELINE" if action == "BASELINE" else "跳过需要模型意见") in sent[
        "text"
    ]
    active.clear()
    watchdog.run_once()
    watchdog.run_once()
    assert len(connection.calls) == 2
    assert "不代表所有环节均已恢复" in connection.calls[-1]["text"]


@pytest.mark.parametrize("state", ["AVAILABLE", "DISABLED"])
def test_known_recovery_clears_only_budget_incident(state):
    findings = {"POSITION_SAFETY_BLOCKED": {"some": "evidence"}}
    subject.apply_budget_health(findings, {"active": [subject.CODE]}, {"status": state})
    assert findings == {"POSITION_SAFETY_BLOCKED": {"some": "evidence"}}


def test_unknown_never_invents_exhaustion_or_recovery():
    findings = {}
    subject.apply_budget_health(findings, {}, {"status": "UNKNOWN"})
    assert findings == {}
    subject.apply_budget_health(
        findings, {"active": [subject.CODE]}, {"status": "UNKNOWN"}
    )
    assert findings[subject.CODE]["awaiting_fresh_read"] is True
    assert findings[subject.CODE]["details"]["status"] == "UNAVAILABLE"


def previous_exhaustion():
    findings = {}
    subject.apply_budget_health(findings, {}, diagnostic())
    return {"active": [subject.CODE], "findings": findings}


def test_same_day_missing_budget_is_not_recovery_even_after_unknown_reads():
    previous = previous_exhaustion()
    missing = diagnostic(status="AVAILABLE", used_tokens=0, budget_row_present=False)
    for _ in range(3):
        findings = {}
        result = subject.apply_budget_health(findings, previous, missing)
        assert result["status"] == "UNKNOWN"
        assert findings[subject.CODE]["awaiting_fresh_read"] is True
        assert findings[subject.CODE]["budget_day_start_ms"] == DAY
        previous = {"active": [subject.CODE], "findings": findings}


def test_empty_new_utc8_day_is_real_budget_recovery_not_model_success():
    findings = {}
    fresh = diagnostic(
        status="AVAILABLE",
        used_tokens=0,
        budget_row_present=False,
        day_start_ms=DAY + 86400000,
    )
    result = subject.apply_budget_health(findings, previous_exhaustion(), fresh)
    assert result["status"] == "AVAILABLE" and findings == {}


@pytest.mark.parametrize("status", ["UNKNOWN", "NOT_CONFIGURED"])
def test_missing_configuration_or_unreadable_store_keeps_confirmed_incident(status):
    findings = {}
    subject.apply_budget_health(findings, previous_exhaustion(), {"status": status})
    assert subject.CODE in findings
    assert findings[subject.CODE]["budget_day_start_ms"] == DAY


@pytest.mark.parametrize(
    "previous",
    [
        None,
        {"active": None},
        {"active": [subject.CODE], "findings": None},
        {"active": [subject.CODE], "findings": {subject.CODE: {"details": None}}},
    ],
)
def test_malformed_old_budget_detail_never_masks_other_findings(previous):
    findings = {"POSITION_SAFETY_BLOCKED": {"details": "preserved"}}
    subject.apply_budget_health(findings, previous, {"status": "UNKNOWN"})
    assert findings["POSITION_SAFETY_BLOCKED"] == {"details": "preserved"}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"status": []},
        diagnostic(status="CURRENT", used_tokens=True),
        diagnostic(status="CURRENT", unavailable_action=[]),
        diagnostic(status="CURRENT", unavailable_action="SECRET"),
        diagnostic(status="CURRENT", day_start_ms=DAY + 1),
        diagnostic(status="CURRENT", observed_at_ms=DAY + 86400000),
        diagnostic(status="CURRENT", limit_tokens=0),
        diagnostic(status="CURRENT", used_tokens="SECRET"),
        diagnostic(
            status="CURRENT",
            observed_at_ms=DAY + 86400000 * 10000000 + 1000,
            day_start_ms=DAY + 86400000 * 10000000,
            reset_at_ms=DAY + 86400000 * 10000001,
        ),
    ],
)
def test_bad_details_still_send_sanitized_base_alert(bad):
    notify, connection = notifier(details_for=lambda _: bad)
    assert notify(event(subject.CODE))
    assert "SECRET" not in connection.calls[-1]["text"]
    assert health_notification_details(subject.CODE, bad) == {"status": "UNAVAILABLE"}


def write(database, key, payload):
    store = BusinessState(database)
    previous = store.read(key)
    result = store.change(
        key,
        expected_version=previous.version if previous else 0,
        request_key=str(uuid4()),
        payload=payload,
        reason="QA_ONLY",
    )
    assert result.code == "APPLIED"


def setup_budget(
    database, *, used=492241, tokens=500000, limit=500000, action="BASELINE"
):
    write(
        database,
        policy_key(SCOPE),
        asdict(JevEntryGatePolicy(enabled=True, unavailable_action=action)),
    )
    write(
        database,
        CONTROL_KEY,
        asdict(AdvisoryPolicy(enabled=True, tokens_per_day=tokens)),
    )
    with database() as conn:
        _, day = _window_starts(_clock(conn))
    key = _budget_key(DAY_NAMESPACE, day)
    write(
        database,
        key,
        {**_budget(None, kind="utc8-day", start=day, limit=limit), "used": used},
    )
    return key


@pytest.mark.parametrize(
    "used,limit,expected",
    [
        (492241, 500000, "EXHAUSTED"),
        (491808, 500000, "AVAILABLE"),
        (491809, 500000, "EXHAUSTED"),
        (90000, 95000, "EXHAUSTED"),
        (0, 500000, "AVAILABLE"),
    ],
)
def test_read_bounded_shared_budget_without_writes(database, used, limit, expected):
    key = setup_budget(database, used=used, limit=limit)
    before = BusinessState(database).read(key)
    result = subject.read_budget_health(database, SCOPE)
    assert result["status"] == expected
    assert result["used_tokens"] == used and result["limit_tokens"] == limit
    assert result["reset_at_ms"] - result["day_start_ms"] == 86400000
    assert BusinessState(database).read(key) == before
    assert subject.read_budget_health(
        database, replace(SCOPE, account_id="qa-other")
    ) == {"status": "NOT_CONFIGURED"}


def test_hard_entry_ceiling_matches_500k_even_with_bigger_control(database):
    setup_budget(database, tokens=1000000, limit=1000000)
    result = subject.read_budget_health(database, SCOPE)
    assert result["status"] == "EXHAUSTED" and result["limit_tokens"] == 500000


def test_planned_pacing_pause_is_not_hard_exhaustion_alarm(database, monkeypatch):
    key = setup_budget(database, used=100000)
    day = int(key.key)
    write(
        database,
        CONTROL_KEY,
        asdict(AdvisoryPolicy(enabled=True, token_budget_pacing=True)),
    )
    monkeypatch.setattr(subject, "_clock", lambda conn: day + 1000)
    result = subject.read_budget_health(database, SCOPE)
    assert result["status"] == "AVAILABLE" and result["used_tokens"] == 100000
    findings = {}
    subject.apply_budget_health(findings, {}, result)
    assert findings == {}


def test_empty_day_is_available_not_a_write_or_model_success(database):
    write(database, policy_key(SCOPE), asdict(JevEntryGatePolicy(enabled=True)))
    write(database, CONTROL_KEY, asdict(AdvisoryPolicy(enabled=True)))
    with database() as conn:
        _, day = _window_starts(_clock(conn))
    result = subject.read_budget_health(database, SCOPE)
    assert result["status"] == "AVAILABLE" and result["used_tokens"] == 0
    assert BusinessState(database).read(_budget_key(DAY_NAMESPACE, day)) is None
    assert result["budget_row_present"] is False
    previous = previous_exhaustion()
    previous["findings"][subject.CODE]["budget_day_start_ms"] = day
    findings = {}
    assert (
        subject.apply_budget_health(findings, previous, result)["status"] == "UNKNOWN"
    )
    assert subject.CODE in findings


def test_disabled_and_invalid_controls_do_not_fabricate_exhaustion(database):
    assert subject.read_budget_health(database, SCOPE) == {"status": "NOT_CONFIGURED"}
    write(database, policy_key(SCOPE), asdict(JevEntryGatePolicy(enabled=True)))
    assert subject.read_budget_health(database, SCOPE) == {"status": "UNKNOWN"}
    write(database, CONTROL_KEY, {"raw_error": "SECRET"})
    assert subject.read_budget_health(database, SCOPE) == {"status": "UNKNOWN"}


def test_database_failure_is_bounded_unknown_without_error_text():
    def unavailable():
        raise ConnectionError("SECRET_DSN")

    assert subject.read_budget_health(unavailable, SCOPE) == {"status": "UNKNOWN"}
    with pytest.raises(ValueError, match="SANDBOX"):
        subject.read_budget_health(unavailable, replace(SCOPE, environment="LIVE"))
