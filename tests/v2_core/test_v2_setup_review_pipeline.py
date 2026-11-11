"""Pure setup-study orchestration tests: fake stores only, never PG/network."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from test_v2_daily_mover_job import (
    NOW,
    SCOPE,
    WINDOW,
    FakeStore,
    job,
    observed_catalog,
    setup_study_loader,
)

import services.v2_daily_mover_job as job_module
from services.v2_daily_mover_report import seal_report
from services.v2_setup_shadow_paths import enrich_setup_paths
from v2_core.quant_indicators import MINUTE

HOUR = 3_600_000


def sealed(value):
    result = {key: item for key, item in value.items() if key != "content_digest"}
    encoded = json.dumps(
        result,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return {**result, "content_digest": hashlib.sha256(encoded.encode()).hexdigest()}


def healthy_paths(study, reader, *, as_of_ms):
    # The healthy empty study needs no archive calls, but a configured callable
    # keeps the public enrichment boundary identical to a configured deployment.
    return enrich_setup_paths(study, reader or (lambda *_: []), as_of_ms=as_of_ms)


def configured_job(**kwargs):
    worker, store, market = job(**kwargs)
    worker.catalog_loader = observed_catalog
    worker.setup_path_reviewer = healthy_paths
    return worker, store, market


def with_observation(conn, scope, *, report, as_of_ms, prior=None):
    result = setup_study_loader(
        conn, scope, report=report, as_of_ms=as_of_ms, prior=prior
    )
    decided = report["window"]["start_ms"] + HOUR + 1000
    result["samples"] = [
        {
            "opportunity_ref": "f" * 64,
            "symbol": "OUTSIDERUSDT",
            "side": "BUY",
            "environment": scope.environment,
            "status": "BASELINE_MATCHED",
            "observed_ms": decided - 1000,
            "decided_ms": decided,
            "bar_close_ms": decided - 1000,
            "baseline_market_reason": "TREND_VOLUME_UNCONFIRMED",
            "alternatives": {
                "BREAKOUT_VOLUME075": {"market_reason": "CANDIDATE"},
                "PULLBACK_VOLUME075": {"market_reason": "TREND_NO_FRESH_SETUP"},
            },
        }
    ]
    return sealed(result)


@pytest.mark.parametrize("empty_ranking", [False, True])
def test_real_setup_path_reader_runs_after_all_pg_transactions_close(empty_ranking):
    messages, events = [], []
    worker, store, market = configured_job(
        notifier=lambda text: messages.append(text) or 123
    )
    worker.setup_path_reviewer = enrich_setup_paths
    if empty_ranking:
        market.items = []

        def forbidden_evidence(*args, **kwargs):
            pytest.fail("empty ranking cannot trigger symbol-scoped evidence")

        worker.evidence_loader = forbidden_evidence

    def load(conn, scope, *, report, as_of_ms, prior):
        assert isinstance(conn, FakeStore) and conn.reading
        assert conn.statements[-1] == (
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
            (),
        )
        assert prior is None
        assert report["window"] == WINDOW
        assert report["as_of_ms"] == as_of_ms == NOW
        if empty_ranking:
            assert report["rows"] == [] and report["account_evidence"] is None
        events.append("STUDY_READ")
        return with_observation(
            conn, scope, report=report, as_of_ms=as_of_ms, prior=prior
        )

    def load_cases(conn, scope, evidence, **kwargs):
        assert conn.reading
        return {"cases": [], "source": "BINANCE_USDM_SANDBOX"}

    def archive(symbol, start, end):
        assert not store.reading, "setup archive read must not hold the PG snapshot"
        assert symbol == "OUTSIDERUSDT", (
            "setup samples need not belong to the mover ranking"
        )
        events.append("ARCHIVE_READ")
        return [
            {"t": t, "o": "100", "h": "110", "l": "90", "c": "100", "v": "1"}
            for t in range(end - MINUTE, start - MINUTE, -MINUTE)
        ]

    worker.setup_study_loader = load
    worker.case_loader = load_cases
    worker.path_reader = archive
    result = worker.run_day(WINDOW)
    assert events == ["STUDY_READ", "ARCHIVE_READ"]
    assert result["accounts"][0]["status"] == "CREATED"
    assert result["orders_submitted"] is False
    assert len(messages) == 1
    report = store.saved_reports[-1]
    assert report["setup_path_review"]["cases"][0]["status"] == "OBSERVED"
    assert report["setup_path_review"]["coverage"]["all_windows_observed"] is True
    assert (
        report["setup_path_review"]["study_content_digest"]
        == report["setup_study"]["content_digest"]
    )
    assert report["account_day_review"]["status"] == "AVAILABLE"
    assert report["frozen_replay_review"]["status"] == "AVAILABLE"
    assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
    assert report == seal_report(report)
    assert report["review_components"]["pipeline"] == "daily-mover-review-pipeline-v5"
    assert (
        report["review_components"]["settlement_refresh"]
        == "daily-mover-settlement-refresh-v1"
    )
    assert (
        report["review_components"]["jev_participation"]
        == "daily-account-jev-participation-v1"
    )


@pytest.mark.parametrize("failure", ["study", "paths"])
def test_setup_failures_preserve_account_report_other_account_and_delivery(
    failure, capsys
):
    other = replace(SCOPE, account_id="qa-setup-other")
    messages, path_calls = [], []
    worker, store, _ = configured_job(
        scopes=(SCOPE, other), notifier=lambda text: messages.append(text) or 123
    )
    first_scope_key = None

    def load(conn, scope, **kwargs):
        nonlocal first_scope_key
        if scope == SCOPE:
            first_scope_key = kwargs["report"]["scope_key"]
            if failure == "study":
                raise RuntimeError("SECRET_SETUP_DB_PASSWORD")
        return setup_study_loader(conn, scope, **kwargs)

    def paths(study, reader, *, as_of_ms):
        assert not store.reading
        path_calls.append(study.get("scope_key"))
        if failure == "paths" and study.get("scope_key") == first_scope_key:
            raise RuntimeError("SECRET_SETUP_ARCHIVE_HOST")
        return healthy_paths(study, reader, as_of_ms=as_of_ms)

    worker.setup_study_loader = load
    worker.setup_path_reviewer = paths
    result = worker.run_day(WINDOW)
    failed, healthy = store.saved_reports
    assert len(path_calls) == len(messages) == len(result["accounts"]) == 2
    failed_key = "setup_study" if failure == "study" else "setup_path_review"
    assert failed[failed_key]["status"] == "UNKNOWN"
    assert failed[failed_key]["coverage"]["retryable"] is True
    if failure == "paths":
        assert failed["setup_study"]["reason"] != "REVIEW_READ_UNAVAILABLE"
    for report in (failed, healthy):
        assert report["account_evidence"] is not None
        assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
        assert report["account_day_review"]["status"] == "AVAILABLE"
        assert report["frozen_replay_review"]["status"] == "AVAILABLE"
        assert report["execution_review"]["status"] == "AVAILABLE"
        assert report == seal_report(report)
    assert healthy["setup_path_review"]["status"] == "NO_SAMPLES"
    assert healthy["optional_review_snapshot"]["retry_after_ms"] is None
    assert "SECRET_SETUP" not in json.dumps([result, store.saved_reports, messages])
    assert "SECRET_SETUP" not in capsys.readouterr().out


@pytest.mark.parametrize("review_key", ["setup_study", "setup_path_review"])
@pytest.mark.parametrize(
    "field,replacement",
    [
        ("scope_key", "1" * 64),
        ("account_environment", "LIVE"),
        ("as_of_ms", NOW - 1),
        ("window_start_ms", WINDOW["start_ms"] - 86_400_000),
        ("window_end_ms", WINDOW["end_ms"] - 86_400_000),
    ],
)
def test_setup_reviews_reject_validly_sealed_cross_account_time_bindings(
    review_key, field, replacement
):
    worker, store, _ = configured_job()

    def load(conn, scope, **kwargs):
        result = setup_study_loader(conn, scope, **kwargs)
        result[field] = replacement
        return sealed(result)

    def paths(study, reader, *, as_of_ms):
        result = healthy_paths(study, reader, as_of_ms=as_of_ms)
        result[field] = replacement
        return sealed(result)

    if review_key == "setup_study":
        worker.setup_study_loader = load
    else:
        worker.setup_path_reviewer = paths
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "CREATED"
    report = store.saved_reports[-1]
    review = report[review_key]
    assert review["status"] == "UNKNOWN"
    assert review["reason"] == (
        "REVIEW_READ_UNAVAILABLE"
        if review_key == "setup_study"
        else "SETUP_PATH_UNAVAILABLE"
    )
    assert report["account_day_review"]["status"] == "AVAILABLE"
    assert all(
        review[flag] is False
        for flag in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )


@pytest.mark.parametrize(
    "field", ["study_content_digest", "plan_digest", "input_digest"]
)
def test_path_review_must_bind_to_exact_same_study_content_plan_and_inputs(field):
    worker, store, _ = configured_job()

    def paths(study, reader, *, as_of_ms):
        result = healthy_paths(study, reader, as_of_ms=as_of_ms)
        result[field] = "2" * 64
        return sealed(result)

    worker.setup_path_reviewer = paths
    worker.run_day(WINDOW)
    report = store.saved_reports[-1]
    assert report["setup_path_review"]["reason"] == "SETUP_PATH_UNAVAILABLE"
    assert report["setup_study"]["reason"] != "REVIEW_READ_UNAVAILABLE"
    assert report["account_day_review"]["status"] == "AVAILABLE"


@pytest.mark.parametrize("review_key", ["setup_study", "setup_path_review"])
@pytest.mark.parametrize(
    "corruption",
    [
        "content_digest",
        "execution_authorized",
        "automatic_parameter_updates",
        "promotion_authorized",
        "version",
    ],
)
def test_setup_review_digest_version_and_permission_guards(review_key, corruption):
    worker, store, _ = configured_job()

    def corrupt(result):
        if corruption == "content_digest":
            return {**result, "content_digest": "0" * 64}
        return sealed(
            {
                **result,
                corruption: "previous-version" if corruption == "version" else True,
            }
        )

    def load(conn, scope, **kwargs):
        return corrupt(setup_study_loader(conn, scope, **kwargs))

    def paths(study, reader, *, as_of_ms):
        return corrupt(healthy_paths(study, reader, as_of_ms=as_of_ms))

    if review_key == "setup_study":
        worker.setup_study_loader = load
    else:
        worker.setup_path_reviewer = paths
    worker.run_day(WINDOW)
    report = store.saved_reports[-1]
    assert report[review_key]["status"] == "UNKNOWN"
    assert report["account_day_review"]["status"] == "AVAILABLE"


@pytest.mark.parametrize("failed_loader", ["setup_study_loader", "setup_path_reviewer"])
def test_setup_failures_retry_only_three_times_without_repeat_daily_notification(
    failed_loader,
):
    messages, calls = [], []
    worker, store, market = configured_job(
        notifier=lambda text: messages.append(text) or 123
    )
    clock = {"ms": max(NOW, WINDOW["end_ms"] + 6 * HOUR)}
    worker.clock = lambda: clock["ms"]

    def failed(*args, **kwargs):
        calls.append(clock["ms"])
        raise RuntimeError("SECRET_SETUP_TRANSIENT_ERROR")

    setattr(worker, failed_loader, failed)
    public_requests = None
    for attempt in range(1, 4):
        assert worker.run_day(WINDOW)["accounts"][0]["status"] == "CREATED"
        report = store.saved_reports[-1]
        assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
        assert report["evidence_snapshot"]["maturity"] == "MATURE"
        assert report["optional_review_snapshot"] == {
            "attempts": attempt,
            "status": "PARTIAL_UNKNOWN",
            "retry_after_ms": clock["ms"] + HOUR if attempt < 3 else None,
        }
        assert len(messages) == 1
        assert len(calls) == attempt
        public_requests = (
            market.requests if public_requests is None else public_requests
        )
        assert market.requests == public_requests
        assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
        clock["ms"] += HOUR
    clock["ms"] += 12 * HOUR
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert len(store.saved_reports) == len(calls) == 3
    assert len(messages) == 1
    assert "SECRET_SETUP" not in json.dumps(store.saved_reports)


@pytest.mark.parametrize("component", ["pipeline", "setup_study", "setup_paths"])
def test_changed_component_refreshes_once_without_telegram_or_market_duplicates(
    component,
):
    messages = []
    worker, store, market = configured_job(
        notifier=lambda text: messages.append(text) or 123
    )
    worker.run_day(WINDOW)
    identity, old = store.reports[(SCOPE.account_id, WINDOW["date"])]
    old = deepcopy(old)
    old["review_components"][component] = "old-method"
    old["optional_review_snapshot"] = {
        "attempts": 3,
        "status": "PARTIAL_UNKNOWN",
        "retry_after_ms": None,
    }
    store.reports[(SCOPE.account_id, WINDOW["date"])] = identity, seal_report(old)
    public_requests = market.requests
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "CREATED"
    assert store.saved_reports[-1]["review_components"] == job_module.REVIEW_COMPONENTS
    assert store.saved_reports[-1]["optional_review_snapshot"]["attempts"] == 1
    assert store.saved_reports[-1]["setup_path_review"]["status"] == "NO_SAMPLES"
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert len(store.saved_reports) == 2
    assert len(messages) == 1
    assert market.requests == public_requests


def test_existing_study_selection_is_passed_to_refresh_without_mutating_prior():
    priors = []
    worker, store, _ = configured_job()

    def load(conn, scope, **kwargs):
        priors.append(deepcopy(kwargs["prior"]))
        return setup_study_loader(conn, scope, **kwargs)

    worker.setup_study_loader = load
    worker.run_day(WINDOW)
    original = deepcopy(store.saved_reports[-1])
    worker.force_evidence_refresh = True
    worker.clock = lambda: NOW + HOUR
    worker.run_day(WINDOW)
    assert priors == [None, original["setup_study"]]
    assert store.saved_reports[0] == original
    assert store.saved_reports[-1]["setup_study"]["as_of_ms"] == NOW + HOUR


def test_missing_archive_configuration_is_nonretryable_and_does_not_fail_account_review():
    worker, store, _ = configured_job()
    worker.setup_path_reviewer = enrich_setup_paths
    worker.path_reader = None
    worker.clock = lambda: max(NOW, WINDOW["end_ms"] + 6 * HOUR)
    worker.run_day(WINDOW)
    report = store.saved_reports[-1]
    assert report["setup_path_review"]["reason"] == "PATH_READER_INVALID"
    assert report["setup_path_review"]["coverage"]["retryable"] is False
    assert report["setup_path_review"]["scope_key"] == report["scope_key"]
    assert report["optional_review_snapshot"]["retry_after_ms"] is None
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert report["account_day_review"]["status"] == "AVAILABLE"


def test_pending_setup_paths_get_six_hour_maturity_refresh_after_optional_retry_cap():
    messages, priors, archive_calls = [], [], []
    clock = {"ms": WINDOW["end_ms"] + MINUTE}
    worker, store, market = configured_job(
        notifier=lambda text: messages.append(text) or 123
    )
    worker.clock = lambda: clock["ms"]
    worker.setup_path_reviewer = enrich_setup_paths

    def load(conn, scope, **kwargs):
        prior = kwargs["prior"]
        priors.append(deepcopy(prior))
        result = with_observation(conn, scope, **kwargs)
        closed = WINDOW["end_ms"] - 15 * MINUTE
        result["samples"][0].update(
            observed_ms=closed, decided_ms=closed + 1000, bar_close_ms=closed
        )
        if prior is not None:
            # The orchestrator supplies the complete previous study; the loader
            # can preserve the original metadata selection and its input digest.
            result["selection"] = deepcopy(prior["selection"])
            result["input_digest"] = prior["input_digest"]
        return sealed(result)

    def cases(conn, scope, evidence, **kwargs):
        assert conn.reading
        return {"cases": [], "source": "BINANCE_USDM_SANDBOX"}

    def archive(symbol, start, end):
        assert not store.reading
        archive_calls.append((clock["ms"], symbol, start, end))
        return [
            {"t": t, "o": "100", "h": "101", "l": "99", "c": "100", "v": "1"}
            for t in range(start, end, MINUTE)
        ]

    worker.setup_study_loader = load
    worker.case_loader = cases
    worker.path_reader = archive
    for attempt in range(1, 4):
        assert worker.run_day(WINDOW)["accounts"][0]["status"] == "CREATED"
        report = store.saved_reports[-1]
        assert report["optional_review_snapshot"]["attempts"] == attempt
        assert report["setup_path_review"]["coverage"]["retryable"] is True
        assert (
            report["setup_path_review"]["cases"][0]["windows"]["240"]["status"]
            == "PENDING"
        )
        if attempt == 1:
            assert archive_calls == []
        if attempt < 3:
            clock["ms"] = report["evidence_snapshot"]["retry_after_ms"]
    public_requests = market.requests
    maturity_at = WINDOW["end_ms"] + 6 * HOUR
    assert report["optional_review_snapshot"]["retry_after_ms"] is None
    assert report["evidence_snapshot"]["retry_after_ms"] == maturity_at
    clock["ms"] = maturity_at - 1
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    clock["ms"] = maturity_at
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "CREATED"
    mature = store.saved_reports[-1]
    assert mature["evidence_snapshot"]["maturity"] == "MATURE"
    assert mature["setup_path_review"]["coverage"]["all_windows_observed"] is True
    assert mature["setup_path_review"]["coverage"]["retryable"] is False
    assert mature["evidence_snapshot"]["retry_after_ms"] is None
    assert mature["optional_review_snapshot"] == {
        "attempts": 3,
        "status": "COMPLETED",
        "retry_after_ms": None,
    }
    original_study = store.saved_reports[0]["setup_study"]
    assert priors[0] is None
    assert len(priors) == len(store.saved_reports) == 4
    assert all(
        prior["selection"] == original_study["selection"] for prior in priors[1:]
    )
    assert all(
        report["setup_study"]["samples"] == original_study["samples"]
        for report in store.saved_reports
    )
    assert len(messages) == 1
    assert market.requests == public_requests
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
