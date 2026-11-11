"""Strict model-health alert details; fake Telegram and existing DM/dedup only."""

from copy import deepcopy

import pytest
from test_v2_position_alert_details import event, notifier
from test_v2_testnet_watchdog import Stop

from services.v2_jev_budget_health import MODEL_CODE, apply_model_health
from services.v2_testnet_watchdog import HealthWatchdog
from v2_core.jev_health import fold_model_health, model_health_diagnostic
from v2_core.telegram import health_notification_details

NOW = 1800000000000


def details(**changes):
    return {
        "status": "CURRENT",
        "observed_at_ms": NOW,
        "latest_result_at_ms": NOW - 1000,
        "last_failure_at_ms": NOW - 1000,
        "last_success_at_ms": None,
        "consecutive_failures": 3,
        "recent_requests": 3,
        "recent_failures": 3,
        "window_ms": 1800000,
        "reason": "CONSECUTIVE_FAILURES",
        "latest_reason": "MODEL_LOCAL_STARTUP_UNAVAILABLE",
        **changes,
    }


@pytest.mark.parametrize("success", [None, NOW - 100000, NOW - 1000])
def test_current_model_health_uses_chinese_dm_and_keeps_risk_boundaries(success):
    data = details(last_success_at_ms=success)
    data.update(raw_error="SECRET_PROVIDER_ERROR", prompt="SECRET_PROMPT")
    before = deepcopy(data)
    notify, tg = notifier(details_for=lambda _: data, now=NOW)
    assert notify(event(MODEL_CODE))
    assert len(tg.calls) == 1 and tg.calls[0]["chat_id"] == "321"
    text = tg.calls[0]["text"]
    assert "本地调用进程启动失败" in text
    assert "连续失败：3 次" in text and "3 / 3 次失败" in text
    assert "供应商余额" in text and "不是策略获利证明" in text
    assert "原风控、持仓保护和平仓" in text
    assert "UTC+8" in text and "SECRET" not in text
    assert ("暂无已记录的成功结果" in text) is (success is None)
    assert data == before
    projected = health_notification_details(MODEL_CODE, data)
    assert "raw_error" not in projected and "prompt" not in projected


@pytest.mark.parametrize(
    "reason,label",
    [
        ("MODEL_CONFIGURATION_UNAVAILABLE", "客户端配置不可用"),
        ("MODEL_UNAVAILABLE", "模型服务不可用"),
        ("MODEL_RESPONSE_INVALID", "格式校验"),
        ("MODEL_UNKNOWN_FAILURE", "未分类调用失败"),
        ("MODEL_LATE_RESPONSE", "超过有效时限"),
        ("PROVIDER_REQUEST_REJECTED", "供应商拒绝请求"),
        ("MODEL_TIMEOUT", "模型调用超时"),
        ("TOKEN_RESERVATION_EXCEEDED", "超过本次预留"),
        ("DEADLINE_EXPIRED", "调用期限已过"),
    ],
)
def test_failure_reason_mapping_never_passes_raw_errors(reason, label):
    notify, tg = notifier(details_for=lambda _: details(latest_reason=reason), now=NOW)
    assert notify(event(MODEL_CODE))
    assert label in tg.calls[0]["text"]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "SECRET",
        {"status": []},
        details(observed_at_ms=True),
        details(observed_at_ms=253402214400001),
        details(latest_result_at_ms=NOW + 1),
        details(last_failure_at_ms=NOW - 2000),
        details(last_success_at_ms=True),
        details(last_success_at_ms=NOW),
        details(last_success_at_ms=-1),
        details(consecutive_failures=True),
        details(consecutive_failures=0),
        details(consecutive_failures=1000001),
        details(recent_requests=21),
        details(recent_failures=4),
        details(recent_failures=0),
        details(window_ms=59999),
        details(window_ms=86400001),
        details(latest_reason="SECRET"),
        details(latest_reason=[]),
        details(reason="SECRET"),
        details(reason=[]),
        details(reason="RECENT_REQUESTS_WITHOUT_SUCCESS", recent_requests=4),
        details(latest_result_at_ms=NOW - 1800000, last_failure_at_ms=NOW - 1800000),
    ],
)
def test_malformed_details_do_not_leak_or_suppress_base_alert(bad):
    assert health_notification_details(MODEL_CODE, bad) == {"status": "UNAVAILABLE"}
    notify, tg = notifier(details_for=lambda _: bad, now=NOW)
    assert notify(event(MODEL_CODE))
    text = tg.calls[0]["text"]
    assert "详情：暂时无法读取" in text and "SECRET" not in text
    assert "JEV 最近完成" in text


@pytest.mark.parametrize("offset", [-120001, 1])
def test_freshness_rechecks_diagnostic_against_notification_clock(offset):
    diagnostic = details(
        observed_at_ms=NOW + offset,
        latest_result_at_ms=NOW + offset - 1000,
        last_failure_at_ms=NOW + offset - 1000,
    )
    notify, tg = notifier(details_for=lambda _: diagnostic, now=NOW)
    assert notify(event(MODEL_CODE))
    text = tg.calls[0]["text"]
    assert "模型诊断已过期" in text and "连续失败：" not in text
    assert "不能据此判断已恢复" in text


def test_real_projection_and_window_failure_label_round_trip():
    snapshot = None
    for index in range(3):
        snapshot = fold_model_health(
            snapshot,
            status="FALLBACK",
            reason="MODEL_TIMEOUT",
            completed_at_ms=NOW - 3000 + index * 1000,
        )
    diagnostic = model_health_diagnostic(snapshot, now_ms=NOW, failure_threshold=4)
    assert diagnostic["reason"] == "RECENT_REQUESTS_WITHOUT_SUCCESS"
    findings = {"POSITION_SAFETY_BLOCKED": {"independent": True}}
    apply_model_health(findings, {}, diagnostic)
    assert findings["POSITION_SAFETY_BLOCKED"] == {"independent": True}
    raw = findings[MODEL_CODE]["details"]
    assert health_notification_details(MODEL_CODE, raw)["status"] == "CURRENT"
    notify, tg = notifier(details_for=lambda _: raw, now=NOW)
    assert notify(event(MODEL_CODE))
    assert "观察窗口内保留完成记录均未成功" in tg.calls[0]["text"]


def test_existing_watchdog_retries_delivery_deduplicates_and_routes_recovery_dm():
    def failed_details(_):
        raise RuntimeError("SECRET_DB_DSN")

    notify, tg = notifier(details_for=failed_details, now=NOW)
    current = {MODEL_CODE}
    watchdog = HealthWatchdog(
        lambda: frozenset(current),
        notify=notify,
        stop=Stop(),
        interval_seconds=10,
    )
    tg.fail = True
    watchdog.run_once()
    assert not watchdog.reported
    tg.fail = False
    watchdog.run_once()
    assert watchdog.reported == {MODEL_CODE}
    watchdog.run_once()
    assert len(tg.calls) == 2
    current.clear()
    watchdog.run_once()
    assert len(tg.calls) == 3 and not watchdog.reported
    assert "不代表所有环节均已恢复" in tg.calls[-1]["text"]
    assert all(call["chat_id"] == "321" for call in tg.calls)
    assert all("SECRET" not in call["text"] for call in tg.calls)
