"""Research integration failures cannot consume evidence or duplicate daily TG."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest
from test_v2_daily_mover_job import (
    NOW,
    SCOPE,
    WINDOW,
    FakeStore,
    evidence_loader,
    job,
)

import services.v2_daily_mover_job as job_module
from services.v2_daily_mover_coverage import unavailable_snapshot
from services.v2_daily_mover_report import seal_report
from v2_core.evidence import canonical, digest


def observed_catalog(conn, *, environment, symbols, as_of_ms):
    assert isinstance(conn, FakeStore) and conn.reading
    assert conn.statements[-1] == ("SET TRANSACTION READ ONLY", ())
    return {
        **unavailable_snapshot(
            environment=environment, symbols=symbols, as_of_ms=as_of_ms
        ),
        "status": "OBSERVED",
        "reason": "CATALOG_OBSERVED",
        "observed_at_ms": as_of_ms - 1000,
        "rows": [
            {"symbol": symbol, "present": True, "status": "TRADING", "supported": True}
            for symbol in symbols
        ],
    }


def assert_sealed(value):
    assert value["content_digest"] == digest(
        canonical({key: item for key, item in value.items() if key != "content_digest"})
    )


@pytest.mark.parametrize("old_component", [None, *job_module.REVIEW_COMPONENTS])
def test_old_component_revision_refreshes_once_without_duplicate_daily_notification(
    old_component,
):
    messages, catalog_calls = [], []
    worker, store, market = job(notifier=lambda text: messages.append(text) or 123)

    def catalog(conn, **kwargs):
        catalog_calls.append(kwargs)
        return observed_catalog(conn, **kwargs)

    worker.catalog_loader = catalog
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "CREATED"
    assert len(messages) == len(store.saved_reports) == len(catalog_calls) == 1
    key = (SCOPE.account_id, WINDOW["date"])
    identity, current = store.reports[key]
    old = deepcopy(current)
    if old_component is None:
        del old["review_components"]
    else:
        old["review_components"][old_component] = "PREVIOUS_METHOD_VERSION"
    old.pop("coverage_review", None)
    old.pop("execution_review", None)
    old["optional_review_snapshot"] = {
        "attempts": 3,
        "status": "PARTIAL_UNKNOWN",
        "retry_after_ms": None,
    }
    store.reports[key] = identity, seal_report(old)
    request_count = market.requests

    refreshed = worker.run_day(WINDOW)
    assert refreshed["accounts"][0]["status"] == "CREATED"
    assert len(store.saved_reports) == len(catalog_calls) == 2
    assert len(messages) == 1
    assert market.requests == request_count
    latest = store.saved_reports[-1]
    assert latest["review_components"] == job_module.REVIEW_COMPONENTS
    assert latest["coverage_review"]["status"] == "OBSERVED_BOUNDED"
    assert latest["optional_review_snapshot"]["attempts"] == 1
    assert latest == seal_report(latest)

    cached = worker.run_day(WINDOW)
    assert cached["accounts"][0]["status"] == "EXISTING"
    assert len(store.saved_reports) == len(catalog_calls) == 2
    assert len(messages) == 1
    assert market.requests == request_count


def test_catalog_failure_does_not_lose_account_evidence_or_emit_exception_detail(
    capsys,
):
    messages, reads = [], []

    def evidence(conn, scope, **kwargs):
        reads.append(scope)
        return evidence_loader(conn, scope, **kwargs)

    def failed_catalog(conn, **kwargs):
        assert conn.reading
        raise RuntimeError("SECRET_CATALOG_DSN_PASSWORD_MUST_NOT_LEAK")

    worker, store, _ = job(
        notifier=lambda text: messages.append(text) or 123, evidence=evidence
    )
    worker.catalog_loader = failed_catalog
    result = worker.run_day(WINDOW)
    assert result["accounts"][0]["status"] == "CREATED"
    assert result["orders_submitted"] is False
    assert reads == [SCOPE]
    assert len(messages) == 1
    report = store.saved_reports[-1]
    assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
    assert set(report["account_evidence"]["symbols"]) == {"AAAUSDT", "BBBUSDT"}
    assert all(row["evidence"]["signal_count"] == 0 for row in report["rows"])
    coverage = report["coverage_review"]
    assert coverage["status"] == "PARTIAL_UNKNOWN"
    assert coverage["reason"] == "CATALOG_READ_UNAVAILABLE"
    assert coverage["snapshot"]["status"] == "UNKNOWN"
    assert coverage["current_listing_counts"] == {"UNKNOWN": 2}
    assert coverage["counts"]["NO_RETAINED_SIGNAL_UNKNOWN"] == 2
    assert report == seal_report(report)
    assert "SECRET_CATALOG" not in json.dumps([result, report, messages])
    assert "SECRET_CATALOG" not in capsys.readouterr().out
    # Failure remains a normal bounded report, not a retry storm on every tick.
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert reads == [SCOPE] and len(messages) == 1


def test_execution_review_failure_only_degrades_optional_review(monkeypatch, capsys):
    messages = []

    def failed_review(report):
        assert report["coverage_review"]["status"] == "OBSERVED_BOUNDED"
        assert report["account_evidence"]["as_of_ms"] == NOW
        raise RuntimeError("SECRET_EXECUTION_REVIEW_PASSWORD_MUST_NOT_LEAK")

    monkeypatch.setattr(job_module, "build_execution_review", failed_review)
    worker, store, _ = job(notifier=lambda text: messages.append(text) or 123)
    worker.catalog_loader = observed_catalog
    result = worker.run_day(WINDOW)
    assert result["accounts"][0]["status"] == "CREATED"
    assert len(messages) == 1 and not result["orders_submitted"]
    report = store.saved_reports[-1]
    assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
    assert report["coverage_review"]["status"] == "OBSERVED_BOUNDED"
    failed = report["execution_review"]
    assert failed["status"] == "UNKNOWN"
    assert failed["reason"] == "EXECUTION_REVIEW_UNAVAILABLE"
    assert all(
        failed[key] is False
        for key in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )
    assert_sealed(failed)
    assert report == seal_report(report)
    assert "SECRET_EXECUTION" not in json.dumps([result, report, messages])
    assert "SECRET_EXECUTION" not in capsys.readouterr().out


def test_coverage_builder_failure_does_not_block_execution_review_or_delivery(
    monkeypatch,
):
    messages = []

    def failed_review(*_):
        raise RuntimeError("SECRET_COVERAGE_BUILDER_MUST_NOT_LEAK")

    monkeypatch.setattr(job_module, "build_coverage_review", failed_review)
    worker, store, _ = job(notifier=lambda text: messages.append(text) or 123)
    worker.catalog_loader = observed_catalog
    result = worker.run_day(WINDOW)
    assert result["accounts"][0]["status"] == "CREATED"
    assert len(messages) == 1
    report = store.saved_reports[-1]
    assert report["coverage_review"]["reason"] == "COVERAGE_REVIEW_UNAVAILABLE"
    assert report["execution_review"]["reason"] != "EXECUTION_REVIEW_UNAVAILABLE"
    assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
    assert "SECRET_COVERAGE" not in json.dumps([result, report, messages])


@pytest.mark.parametrize("second_environment", ["SANDBOX", "LIVE"])
def test_catalog_snapshot_reused_only_within_same_environment(second_environment):
    second = replace(SCOPE, account_id="qa-mover-two", environment=second_environment)
    third = replace(SCOPE, account_id="qa-mover-three")
    catalog_calls, evidence_calls = [], []

    def catalog(conn, **kwargs):
        catalog_calls.append(deepcopy(kwargs))
        return observed_catalog(conn, **kwargs)

    def evidence(conn, scope, **kwargs):
        evidence_calls.append(scope)
        return evidence_loader(conn, scope, **kwargs)

    worker, store, _ = job(scopes=(SCOPE, second, third), evidence=evidence)
    worker.catalog_loader = catalog
    result = worker.run_day(WINDOW)
    expected_environments = ["SANDBOX"] + (
        ["LIVE"] if second_environment == "LIVE" else []
    )
    assert [call["environment"] for call in catalog_calls] == expected_environments
    assert all(call["symbols"] == ["AAAUSDT", "BBBUSDT"] for call in catalog_calls)
    assert evidence_calls == [SCOPE, second, third]
    assert len(store.saved_reports) == len(result["accounts"]) == 3
    assert len({report["scope_key"] for report in store.saved_reports}) == 3
    for scope, report in zip((SCOPE, second, third), store.saved_reports, strict=True):
        assert (
            report["account_evidence"]["account_scope"]["account_id"]
            == scope.account_id
        )
        assert report["coverage_review"]["snapshot"]["environment"] == scope.environment
        assert report["coverage_review"]["status"] == "OBSERVED_BOUNDED"
        assert report["coverage_review"]["market_execution_environment_match"] is (
            scope.environment == "LIVE"
        )
        assert report == seal_report(report)


def test_failed_shared_catalog_is_called_once_but_all_accounts_keep_their_evidence():
    second = replace(SCOPE, account_id="qa-mover-two")
    catalog_calls = []

    def failed_catalog(conn, **kwargs):
        catalog_calls.append(kwargs)
        raise RuntimeError("SECRET_SHARED_CATALOG_ERROR")

    worker, store, _ = job(scopes=(SCOPE, second))
    worker.catalog_loader = failed_catalog
    result = worker.run_day(WINDOW)
    assert len(catalog_calls) == 1
    assert len(result["accounts"]) == len(store.saved_reports) == 2
    for report in store.saved_reports:
        assert report["coverage_review"]["reason"] == "CATALOG_READ_UNAVAILABLE"
        assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
        assert len(report["account_evidence"]["symbols"]) == 2
        assert "SECRET_SHARED" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["catalog", "execution"])
def test_mature_optional_failures_retry_hourly_three_times_then_stop(
    failure, monkeypatch
):
    messages, failure_calls = [], []
    clock = {"ms": WINDOW["end_ms"] + 6 * 3_600_000 + 1000}
    worker, store, market = job(notifier=lambda text: messages.append(text) or 123)
    worker.clock = lambda: clock["ms"]
    worker.catalog_loader = observed_catalog

    def failing(*args, **kwargs):
        failure_calls.append(clock["ms"])
        raise RuntimeError("SECRET_TRANSIENT_OPTIONAL_FAILURE")

    if failure == "catalog":
        worker.catalog_loader = failing
    else:
        monkeypatch.setattr(job_module, "build_execution_review", failing)
    public_requests = None
    for attempt in range(1, 4):
        outcome = worker.run_day(WINDOW)
        assert outcome["accounts"][0]["status"] == "CREATED"
        report = store.saved_reports[-1]
        assert report["evidence_snapshot"]["maturity"] == "MATURE"
        assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
        assert report["optional_review_snapshot"] == {
            "attempts": attempt,
            "status": "PARTIAL_UNKNOWN",
            "retry_after_ms": clock["ms"] + 3_600_000 if attempt < 3 else None,
        }
        assert (
            report["evidence_snapshot"]["retry_after_ms"]
            == report["optional_review_snapshot"]["retry_after_ms"]
        )
        assert len(failure_calls) == attempt
        assert len(messages) == 1
        if public_requests is None:
            public_requests = market.requests
        assert market.requests == public_requests
        # Same-tick invocation is cached, not another optional attempt.
        assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
        assert len(failure_calls) == attempt
        clock["ms"] += 3_600_000

    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    clock["ms"] += 12 * 3_600_000
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert len(store.saved_reports) == len(failure_calls) == 3
    assert len(messages) == 1 and market.requests == public_requests
    assert "SECRET_TRANSIENT" not in json.dumps([store.saved_reports, messages])


def test_mature_catalog_recovers_on_next_hour_and_stops_optional_retries():
    clock = {"ms": WINDOW["end_ms"] + 6 * 3_600_000 + 1000}
    state = {"fail": True, "calls": 0}
    messages = []

    def catalog(conn, **kwargs):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("SECRET_INITIAL_CATALOG_FAILURE")
        return observed_catalog(conn, **kwargs)

    worker, store, _ = job(notifier=lambda text: messages.append(text) or 123)
    worker.clock = lambda: clock["ms"]
    worker.catalog_loader = catalog
    worker.run_day(WINDOW)
    assert store.saved_reports[-1]["optional_review_snapshot"]["attempts"] == 1
    clock["ms"] += 3_600_000 - 1
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert state["calls"] == 1
    state["fail"] = False
    clock["ms"] += 1
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "CREATED"
    report = store.saved_reports[-1]
    assert report["coverage_review"]["status"] == "OBSERVED_BOUNDED"
    assert report["execution_review"]["status"] == "AVAILABLE"
    assert report["optional_review_snapshot"] == {
        "attempts": 2,
        "status": "COMPLETED",
        "retry_after_ms": None,
    }
    assert report["evidence_snapshot"]["retry_after_ms"] is None
    clock["ms"] += 4 * 3_600_000
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert state["calls"] == len(store.saved_reports) == 2
    assert len(messages) == 1


def corrupt_review(result, corruption):
    """Return fresh malformed output without mutating the input account report."""
    secret = "SECRET_OPTIONAL_REVIEW_PAYLOAD_MUST_NOT_LEAK"
    if corruption == "not_dict":
        return [secret]
    result = deepcopy(result)
    result["untrusted_detail"] = secret
    if corruption == "set":
        result["unsupported"] = {secret}
    elif corruption == "object":
        result["unsupported"] = object()
    elif corruption == "decimal":
        result["unsupported"] = Decimal("1.25")
    elif corruption == "cycle":
        result["unsupported"] = result
    elif corruption == "wrong_version":
        result["version"] = "WRONG_OPTIONAL_REVIEW_VERSION"
    elif corruption == "wrong_digest":
        result["content_digest"] = "0" * 64
    elif corruption == "missing_digest":
        del result["content_digest"]
    elif corruption in {
        "execution_authorized",
        "automatic_parameter_updates",
        "promotion_authorized",
    }:
        result[corruption] = True
    elif corruption == "numeric_false_flag":
        result["execution_authorized"] = 0
    elif corruption == "missing_flag":
        del result["promotion_authorized"]
    elif corruption == "nan":
        result["unsupported"] = float("nan")
    elif corruption == "infinity":
        result["unsupported"] = float("inf")
    elif corruption == "negative_infinity":
        result["unsupported"] = float("-inf")
    elif corruption == "oversized":
        result["unsupported"] = "X" * (512 * 1024)
    elif corruption == "mixed_key_types":
        result["unsupported"] = {1: secret, "1": "collision"}
    else:
        raise AssertionError("UNRECOGNIZED_CORRUPTION_CASE")
    # For representable mutations, provide a *correct* hash so flag/version/size
    # checks are independently tested rather than masked by a digest mismatch.
    if corruption not in {"wrong_digest", "missing_digest"}:
        try:
            encoded = json.dumps(
                {
                    key: value
                    for key, value in result.items()
                    if key != "content_digest"
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            result["content_digest"] = digest(encoded)
        except (TypeError, ValueError):
            pass
    return result


@pytest.mark.parametrize("component", ["coverage", "execution"])
@pytest.mark.parametrize(
    "corruption",
    [
        "not_dict",
        "set",
        "object",
        "decimal",
        "cycle",
        "wrong_version",
        "wrong_digest",
        "missing_digest",
        "execution_authorized",
        "automatic_parameter_updates",
        "promotion_authorized",
        "numeric_false_flag",
        "missing_flag",
        "nan",
        "infinity",
        "negative_infinity",
        "oversized",
        "mixed_key_types",
    ],
)
def test_invalid_optional_builder_payload_is_locally_quarantined_for_both_accounts(
    component, corruption, monkeypatch, capsys
):
    messages, builder_calls = [], []
    second = replace(SCOPE, account_id="qa-mover-two")
    attribute = f"build_{component}_review"
    original = getattr(job_module, attribute)

    def broken(report, *args, **kwargs):
        builder_calls.append(report["scope_key"])
        return corrupt_review(original(report, *args, **kwargs), corruption)

    monkeypatch.setattr(job_module, attribute, broken)
    worker, store, _ = job(
        scopes=(SCOPE, second), notifier=lambda text: messages.append(text) or 123
    )
    worker.catalog_loader = observed_catalog
    result = worker.run_day(WINDOW)
    assert len(result["accounts"]) == len(store.saved_reports) == len(messages) == 2
    assert all(account["status"] == "CREATED" for account in result["accounts"])
    assert len(set(builder_calls)) == 2
    assert result["orders_submitted"] is False
    for scope, report in zip((SCOPE, second), store.saved_reports, strict=True):
        failed = report[f"{component}_review"]
        assert failed["status"] == "UNKNOWN"
        assert failed["reason"] == f"{component.upper()}_REVIEW_UNAVAILABLE"
        assert all(
            failed[flag] is False
            for flag in (
                "execution_authorized",
                "automatic_parameter_updates",
                "promotion_authorized",
            )
        )
        assert_sealed(failed)
        healthy = report[
            "execution_review" if component == "coverage" else "coverage_review"
        ]
        assert healthy["status"] == (
            "AVAILABLE" if component == "coverage" else "OBSERVED_BOUNDED"
        )
        assert (
            report["account_evidence"]["account_scope"]["account_id"]
            == scope.account_id
        )
        assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
        assert report["optional_review_snapshot"]["status"] == "PARTIAL_UNKNOWN"
        assert report == seal_report(report)
    serialized = json.dumps([result, store.saved_reports, messages], allow_nan=False)
    assert "SECRET_OPTIONAL_REVIEW" not in serialized
    assert "SECRET_OPTIONAL_REVIEW" not in capsys.readouterr().out
    # New revisions are valid cacheable research documents, not recurring TG spam.
    assert all(
        item["status"] == "EXISTING" for item in worker.run_day(WINDOW)["accounts"]
    )
    assert len(messages) == len(builder_calls) == 2
