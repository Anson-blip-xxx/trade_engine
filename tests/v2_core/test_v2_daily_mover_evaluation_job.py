"""Optional history evaluation stays isolated from account reports and delivery."""

from copy import deepcopy
from dataclasses import replace

import pytest
from test_v2_daily_mover_flow import Store as FakeFlowStore
from test_v2_daily_mover_job import NOW, SCOPE, WINDOW, evidence_loader, job
from test_v2_mover_flow import bundle

from services.v2_daily_mover_flow import DailyMoverFlowReview, unavailable_review
from services.v2_daily_mover_job import DailyMoverJob
from services.v2_daily_mover_report import seal_report
from services.v2_mover_flow_evaluation import (
    FlowHistoryEvaluation,
    unavailable_evaluation,
)
from v2_core.evidence import canonical, digest


class Evaluator:
    def __init__(self, report=None, error=None):
        self.report = report or unavailable_evaluation("QA_HISTORY_SNAPSHOT_ONE")
        self.error = error
        self.calls = []

    def build(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if self.error is not None:
            raise self.error
        return deepcopy(self.report)


def test_disabled_evaluation_preserves_daily_report_and_delivery():
    messages = []
    worker, store, market = job(
        notifier=lambda message: messages.append(message) or 123
    )

    result = worker.run_day(WINDOW)

    report = store.saved_reports[0]
    assert report["flow_evaluation"] == unavailable_evaluation(
        "FLOW_EVALUATION_DISABLED"
    )
    assert len(report["rows"]) == 2
    assert report["account_evidence"]["symbols"]
    assert result["accounts"] == [{"status": "CREATED", "delivery": "SENT"}]
    assert result["orders_submitted"] is False
    assert len(messages) == 1 and market.requests == 5


@pytest.mark.parametrize("exception", [RuntimeError, TimeoutError, ValueError])
def test_evaluation_failure_is_redacted_and_does_not_block_report_or_dm(
    exception, capsys
):
    private_error = "qa-private-db-host-account-token-must-not-appear"
    messages = []
    evaluator = Evaluator(error=exception(private_error))
    worker, store, market = job(
        notifier=lambda message: messages.append(message) or 123
    )
    worker.flow_evaluator = evaluator

    result = worker.run_day(WINDOW)

    report = store.saved_reports[0]
    assert report["flow_evaluation"] == unavailable_evaluation(
        "FLOW_EVALUATION_UNAVAILABLE"
    )
    assert report["account_evidence"]["symbols"]
    assert report["evidence_snapshot"]["status"] == "READ_COMPLETED"
    assert len(report["rows"]) == 2 and report == seal_report(report)
    assert result["accounts"] == [{"status": "CREATED", "delivery": "SENT"}]
    assert result["orders_submitted"] is False
    assert len(evaluator.calls) == len(messages) == 1 and market.requests == 5
    captured = capsys.readouterr()
    exposed = (
        canonical(
            {"result": result, "reports": store.saved_reports, "messages": messages}
        )
        + captured.out
        + captured.err
    )
    assert private_error not in exposed


def test_same_evaluation_inputs_reuse_existing_report_without_account_reread():
    evidence_calls = []

    def load(conn, scope, **kwargs):
        evidence_calls.append(scope)
        return evidence_loader(conn, scope, **kwargs)

    evaluator = Evaluator()
    worker, store, market = job(evidence=load)
    worker.flow_evaluator = evaluator
    worker.run_day(WINDOW)
    first_report = deepcopy(store.saved_reports[0])
    request_count = market.requests

    result = worker.run_day(WINDOW)

    assert result["accounts"][0]["status"] == "EXISTING"
    assert evidence_calls == [SCOPE]
    assert len(evaluator.calls) == 2 and evaluator.calls[0] == evaluator.calls[1]
    assert store.saved_reports == [first_report]
    assert first_report["flow_evaluation"] == evaluator.report
    assert market.requests == request_count


def test_evaluation_digest_change_creates_immutable_revision_without_second_dm():
    messages = []
    evaluator = Evaluator()
    worker, store, market = job(
        notifier=lambda message: messages.append(message) or 123
    )
    worker.flow_evaluator = evaluator
    worker.run_day(WINDOW)
    original = deepcopy(store.saved_reports[0])
    request_count = market.requests
    evaluator.report = unavailable_evaluation("QA_HISTORY_SNAPSHOT_TWO")

    result = worker.run_day(WINDOW)

    assert result["accounts"] == [
        {"status": "CREATED", "delivery": "ALREADY_HANDLED_OR_DELIVERY_UNKNOWN"}
    ]
    assert len(store.saved_reports) == 2 and store.saved_reports[0] == original
    revised = store.saved_reports[1]
    assert revised["flow_evaluation"] == evaluator.report
    assert revised["content_digest"] != original["content_digest"]
    assert revised["market_digest"] == original["market_digest"]
    assert revised["account_evidence"] == original["account_evidence"]
    assert all(report == seal_report(report) for report in store.saved_reports)
    assert len(messages) == 1 and market.requests == request_count
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert len(store.saved_reports) == 2 and len(messages) == 1


def test_evaluation_runs_once_before_all_account_reads_with_public_inputs_only():
    other = replace(SCOPE, account_id="qa-private-mover-two")
    sequence = []
    calls = []
    expected_flow = unavailable_review("QA_SHARED_PUBLIC_FLOW")
    expected_evaluation = unavailable_evaluation("QA_SHARED_HISTORY")

    def load(conn, scope, **kwargs):
        sequence.append(("account", scope.account_id))
        return evidence_loader(conn, scope, **kwargs)

    class Reviewer:
        def build(self, **kwargs):
            assert set(kwargs) == {"window", "precursor"}
            sequence.append("flow")
            return deepcopy(expected_flow)

    class PublicEvaluator:
        def build(self, **kwargs):
            assert not store.reading and not store.saved_reports
            assert set(kwargs) == {"window", "precursor", "flow_review", "as_of_ms"}
            assert kwargs["window"] == WINDOW and kwargs["as_of_ms"] == NOW
            assert kwargs["flow_review"] == expected_flow
            assert kwargs["precursor"]["fixed_cohort"]
            assert kwargs["precursor"]["cases"]
            calls.append(deepcopy(kwargs))
            sequence.append("evaluation")
            return deepcopy(expected_evaluation)

    _, store, market = job(scopes=(SCOPE, other))
    worker = DailyMoverJob(
        store,
        market,
        clock_ms=lambda: NOW,
        evidence_loader=load,
        flow_reviewer=Reviewer(),
        flow_evaluator=PublicEvaluator(),
    )

    result = worker.run_day(WINDOW)

    assert sequence == [
        "flow",
        "evaluation",
        ("account", SCOPE.account_id),
        ("account", other.account_id),
    ]
    assert len(calls) == 1 and len(result["accounts"]) == 2
    assert market.requests == 5
    public_input = canonical(calls[0])
    assert all(scope.account_id not in public_input for scope in (SCOPE, other))
    assert all(
        key not in public_input
        for key in ('"account_scope"', '"account_evidence"', '"trades"', '"net_pnl"')
    )
    assert all(
        report["flow_evaluation"] == expected_evaluation
        for report in store.saved_reports
    )
    assert [
        report["account_evidence"]["account_scope"]["account_id"]
        for report in store.saved_reports
    ] == [SCOPE.account_id, other.account_id]


def test_legacy_report_without_evaluation_is_upgraded_without_duplicate_delivery():
    messages = []
    worker, store, market = job(
        notifier=lambda message: messages.append(message) or 123
    )
    worker.run_day(WINDOW)
    identity, original = store.existing(SCOPE, WINDOW["date"])
    legacy = deepcopy(original)
    legacy.pop("flow_evaluation")
    legacy = seal_report(legacy)
    store.reports[(SCOPE.account_id, WINDOW["date"])] = (identity, legacy)
    store.saved_reports[0] = deepcopy(legacy)
    worker.flow_evaluator = Evaluator()
    request_count = market.requests

    result = worker.run_day(WINDOW)

    assert result["accounts"][0]["status"] == "CREATED"
    assert len(store.saved_reports) == 2 and store.saved_reports[0] == legacy
    assert store.saved_reports[1]["flow_evaluation"] == worker.flow_evaluator.report
    assert len(messages) == 1 and market.requests == request_count


@pytest.mark.parametrize(
    "malformation",
    [
        "none",
        "list",
        "missing_digest",
        "incorrect_digest",
        "wrong_version",
        "wrong_status",
        "execution_authorized",
        "automatic_parameter_updates",
        "promotion_authorized",
        "oversized",
    ],
)
def test_malformed_evaluation_is_isolated_like_an_optional_analysis_failure(
    malformation, capsys
):
    private_value = "qa-private-invalid-evaluation-must-not-appear"
    payload = unavailable_evaluation("QA_MALFORMED_RESULT")
    if malformation == "none":
        payload = None
    elif malformation == "list":
        payload = [private_value]
    elif malformation == "missing_digest":
        payload.pop("content_digest")
    elif malformation == "incorrect_digest":
        payload["content_digest"] = private_value
    else:
        if malformation == "wrong_version":
            payload["version"] = private_value
        elif malformation == "wrong_status":
            payload["status"] = private_value
        elif malformation == "oversized":
            payload["extra"] = private_value * 25000
        else:
            payload[malformation] = True
        payload.pop("content_digest")
        payload["content_digest"] = digest(canonical(payload))

    class MalformedEvaluator:
        def build(self, **_):
            return payload

    messages = []
    worker, store, _ = job(notifier=lambda message: messages.append(message) or 123)
    worker.flow_evaluator = MalformedEvaluator()

    result = worker.run_day(WINDOW)

    assert store.saved_reports[0]["flow_evaluation"] == unavailable_evaluation(
        "FLOW_EVALUATION_UNAVAILABLE"
    )
    assert result["accounts"] == [{"status": "CREATED", "delivery": "SENT"}]
    assert store.saved_reports[0]["account_evidence"]["symbols"]
    captured = capsys.readouterr()
    exposed = (
        canonical(
            {"result": result, "reports": store.saved_reports, "messages": messages}
        )
        + captured.out
        + captured.err
    )
    assert private_value not in exposed


def test_real_evaluation_core_uses_synthetic_public_evidence_and_stable_mature_inputs():
    clock = [NOW]
    public_calls, history_calls, messages = [], [], []

    class PublicMarket:
        def collect(self, symbol, *, start_ms, end_ms):
            public_calls.append(symbol)
            payload = bundle()
            shift = start_ms - payload["start_ms"]
            payload.update(symbol=symbol, start_ms=start_ms, end_ms=end_ms)
            payload["mapping"].update(spot_symbol=symbol, base_asset=symbol[:-4])
            for name in ("spot", "open_interest", "funding"):
                payload[name]["fetched_at_ms"] = clock[0]
            for row in payload["spot"]["rows"]:
                row[0] += shift
                row[6] += shift
            for name, timestamp in (
                ("open_interest", "timestamp"),
                ("funding", "fundingTime"),
            ):
                for row in payload[name]["rows"]:
                    row["symbol"] = symbol
                    row[timestamp] += shift
            return payload

    class MemoryHistoryEvaluation(FlowHistoryEvaluation):
        def history(self, day):
            history_calls.append(day)
            return []

    def forbidden_database():
        raise AssertionError("synthetic evaluation must not open a database")

    worker, store, market = job(
        notifier=lambda message: messages.append(message) or 123
    )
    worker.clock = lambda: clock[0]
    flow_store = FakeFlowStore(lambda: clock[0])
    worker.flow_reviewer = DailyMoverFlowReview(
        flow_store, PublicMarket, clock_ms=lambda: clock[0]
    )
    worker.flow_evaluator = MemoryHistoryEvaluation(forbidden_database)

    result = worker.run_day(WINDOW)

    report = store.saved_reports[0]
    evaluation = report["flow_evaluation"]
    assert result["accounts"] == [{"status": "CREATED", "delivery": "SENT"}]
    assert evaluation["status"] == "INSUFFICIENT_SAMPLE"
    assert evaluation["history"]["input_days"] == 1
    assert evaluation["history"]["usable_days"] == 1
    assert evaluation["history"]["day_symbol_clusters"] == 2
    assert evaluation["history_read"]["current_day_included"] is True
    summary = evaluation["splits"][evaluation["days"][0]["split"]]
    assert summary["counts"]["planned_slots"] == 6
    assert summary["counts"]["buy"] == summary["counts"]["sell"] == 3
    assert summary["price_baseline"]["outcome_observed"] == 6
    assert summary["price_baseline"]["continuations"] == 6
    for name in ("spot", "open_interest", "funding"):
        assert summary["comparisons"][name]["available_baseline"]["slots"] == 6
        assert summary["comparisons"][name]["unknown_slots"] == 0
    assert (
        summary["comparisons"]["spot"]["groups"]["SAME_DIRECTION_PERSISTENT"]["slots"]
        == 3
    )
    assert evaluation["protocol"]["historical_receipt_proven"] is False
    assert all(
        evaluation[key] is False
        for key in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )
    assert report == seal_report(report)

    clock[0] += 60_000
    repeated = worker.run_day(WINDOW)

    assert repeated["accounts"][0]["status"] == "EXISTING"
    assert history_calls == [WINDOW["date"], WINDOW["date"]]
    assert sorted(public_calls) == ["AAAUSDT", "BBBUSDT"]
    assert market.requests == 5 and len(store.saved_reports) == len(messages) == 1
