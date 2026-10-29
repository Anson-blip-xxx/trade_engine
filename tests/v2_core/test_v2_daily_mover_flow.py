"""Real pure-core + checkpoint orchestration, network-free end-to-end tests."""

from copy import deepcopy

import pytest
from test_v2_daily_mover_job import WINDOW as JOB_WINDOW
from test_v2_daily_mover_job import job
from test_v2_mover_flow import DAY, HOUR, START, bundle

from services.v2_daily_mover_flow import DailyMoverFlowReview, unavailable_review
from services.v2_daily_mover_market import MoverMarketError
from services.v2_daily_mover_report import format_digest
from v2_core.daily_movers import previous_day_window
from v2_core.evidence import canonical, digest


class Store:
    def __init__(self, clock):
        self.clock, self.rows, self.saves, self.prunes = clock, {}, [], []

    def read(self, day, symbol):
        return deepcopy(self.rows.get((day, symbol)))

    def save(self, day, symbol, payload, *, complete):
        previous = self.rows.get((day, symbol))
        if previous and (previous["complete"] or previous["attempts"] >= 3):
            return deepcopy(previous)
        result = {
            "payload": deepcopy(payload),
            "complete": complete,
            "attempts": 1 + (previous or {}).get("attempts", 0),
            "updated_at_ms": self.clock(),
            "input_digest": digest(canonical(payload)),
        }
        self.rows[(day, symbol)] = result
        self.saves.append((day, symbol))
        return deepcopy(result)

    def prune_raw(self, day, *, limit):
        self.prunes.append((day, limit))
        return 0


class Market:
    def __init__(self):
        self.calls, self.error, self.mutate = [], None, lambda payload: None

    def collect(self, symbol, *, start_ms, end_ms):
        self.calls.append(symbol)
        if self.error:
            raise self.error
        payload = bundle()
        assert payload["start_ms"] == start_ms and payload["end_ms"] == end_ms
        payload["symbol"] = symbol
        payload["mapping"].update(spot_symbol=symbol, base_asset=symbol[:-4])
        for part in ("open_interest", "funding"):
            for row in payload[part]["rows"]:
                row["symbol"] = symbol
        self.mutate(payload)
        return payload


def harness():
    clock = [START + DAY + HOUR]
    store, market = Store(lambda: clock[0]), Market()
    factories = []

    def factory():
        factories.append(True)
        return market

    reviewer = DailyMoverFlowReview(store, factory, clock_ms=lambda: clock[0])
    window = previous_day_window(START + DAY)
    precursor = {
        "status": "DESCRIPTIVE_RESEARCH_ONLY",
        "fixed_cohort": ["TESTUSDT"],
        "top_mover_diagnostics": ["OTHERUSDT"],
    }
    return (
        reviewer,
        store,
        market,
        clock,
        factories,
        {"window": window, "precursor": precursor},
    )


def test_real_core_flow_all_components_and_idempotent_checkpoint():
    reviewer, store, market, _, factories, args = harness()
    assert not factories
    first = reviewer.build(**args)
    assert first["component_status_counts"] == {
        name: {"OBSERVED": 12} for name in ("spot", "open_interest", "funding")
    }
    assert len(first["cases"]) == 12 and not first["execution_authorized"]
    assert len(store.saves) == len(market.calls) == 2
    assert len(factories) == 1
    assert reviewer.build(**args) == first
    assert len(store.saves) == len(market.calls) == 2
    assert store.prunes == [(args["window"]["date"], 500)] * 2
    assert all(
        case["components"]["spot"]["receipt"]["historical_receipt_proven"] is False
        for case in first["cases"]
    )


def test_incomplete_samples_retry_hourly_maximum_three_not_every_call():
    reviewer, store, market, clock, _, args = harness()
    market.mutate = lambda p: p["open_interest"].update(rows=[])
    reviewer.build(**args)
    reviewer.build(**args)
    assert len(market.calls) == 2
    for _ in range(5):
        clock[0] += HOUR
        reviewer.build(**args)
    assert len(market.calls) == 6
    assert all(r["attempts"] == 3 and not r["complete"] for r in store.rows.values())


@pytest.mark.parametrize(
    "reason", ["NO_EXACT_SPOT_MAPPING", "EXACT_SPOT_ASSET_MISMATCH"]
)
def test_no_exact_spot_is_unknown_and_terminal_without_discarding_derivatives(reason):
    reviewer, store, market, clock, _, args = harness()

    def absent(p):
        p["mapping"] = {"status": "UNKNOWN", "reason": reason}
        p["spot"].update(status="UNKNOWN", reason=reason, rows=[])

    market.mutate = absent
    report = reviewer.build(**args)
    assert report["component_status_counts"]["spot"] == {"UNKNOWN": 12}
    assert report["component_status_counts"]["open_interest"] == {"OBSERVED": 12}
    assert all(row["complete"] for row in store.rows.values())
    clock[0] += 2 * HOUR
    reviewer.build(**args)
    assert len(market.calls) == 2


@pytest.mark.parametrize(
    "error",
    [
        "MOVER_RATE_LIMITED",
        "MOVER_SHARED_QUOTA_WAIT",
        "MOVER_JOB_DEADLINE",
        "MOVER_FLOW_REQUEST_LIMIT",
        "private_error_secret",
    ],
)
def test_global_halt_keeps_all_unfetched_reference_cases_explicit(error):
    reviewer, store, market, _, _, args = harness()
    market.error = MoverMarketError(error)
    report = reviewer.build(**args)
    assert len(market.calls) == 1 and not store.saves
    assert len(report["cases"]) == 12
    assert all(
        count == {"UNKNOWN": 12} for count in report["component_status_counts"].values()
    )
    assert "private_error_secret" not in canonical(report)


def test_optional_storage_failure_stops_network_but_not_primary_job(monkeypatch):
    reviewer, store, market, _, factories, args = harness()

    def fail(*_):
        raise RuntimeError("private_db_secret")

    monkeypatch.setattr(store, "read", fail)
    report = reviewer.build(**args)
    assert len(report["cases"]) == 12
    assert not factories and not market.calls
    assert "private_db_secret" not in canonical(report)


def test_failed_refresh_preserves_previous_usable_components():
    reviewer, _, market, clock, _, args = harness()
    market.mutate = lambda p: p["open_interest"].update(rows=[])
    before = reviewer.build(**args)
    clock[0] += HOUR
    market.error = MoverMarketError("MOVER_RATE_LIMITED")
    after = reviewer.build(**args)
    assert before == after


def test_returned_unknown_preserves_old_valid_raw_and_original_receipt():
    reviewer, store, market, clock, _, args = harness()
    market.mutate = lambda p: p["open_interest"].update(rows=[])
    before = reviewer.build(**args)
    clock[0] += HOUR

    def failed_spot(p):
        p["spot"].update(
            status="UNKNOWN",
            reason="MOVER_PUBLIC_HTTP_ERROR",
            rows=[],
            fetched_at_ms=clock[0],
        )

    market.mutate = failed_spot
    after = reviewer.build(**args)
    assert after["component_status_counts"]["spot"] == {"OBSERVED": 12}
    assert after["component_status_counts"]["open_interest"] == {"OBSERVED": 12}
    for old, new in zip(before["cases"], after["cases"], strict=True):
        assert old["components"]["spot"] == new["components"]["spot"]
    assert all(
        r["payload"]["retry_evidence"]["retained_components"] == ["spot"]
        for r in store.rows.values()
    )
    assert all(r["complete"] for r in store.rows.values())


def test_returned_transport_unknown_halts_new_fetches_across_builds_but_saves_partial():
    reviewer, store, market, _, _, args = harness()
    market.mutate = lambda p: p["spot"].update(
        status="UNKNOWN", reason="MOVER_PUBLIC_TRANSPORT_FAILURE", rows=[]
    )
    report = reviewer.build(**args)
    assert len(market.calls) == len(store.saves) == 1
    assert report["component_status_counts"]["open_interest"] == {
        "OBSERVED": 6,
        "UNKNOWN": 6,
    }
    assert report["component_status_counts"]["spot"] == {"UNKNOWN": 12}
    assert reviewer.build(**args) == report
    assert len(market.calls) == 1


def test_retry_cannot_trade_away_one_known_clock_for_another_with_equal_coverage():
    reviewer, _, market, clock, _, args = harness()
    market.mutate = lambda p: p["spot"]["rows"].pop(15)
    before = reviewer.build(**args)
    clock[0] += HOUR
    market.mutate = lambda p: p["spot"]["rows"].pop(1)
    after = reviewer.build(**args)
    assert [c["components"]["spot"] for c in before["cases"]] == [
        c["components"]["spot"] for c in after["cases"]
    ]


def test_scope_mismatch_cannot_be_reused_as_same_symbol(monkeypatch):
    reviewer, store, _, _, _, args = harness()
    reviewer.build(**args)
    for value in store.rows.values():
        value["payload"]["symbol"] = "UNRELATEDUSDT"
    report = reviewer.build(**args)
    assert set(report["unknown_symbols"].values()) == {"FLOW_CHECKPOINT_SCOPE_MISMATCH"}
    assert all(c["status"] == "UNKNOWN" for c in report["cases"])


def test_retention_failure_is_explicit_and_does_not_remove_observations(monkeypatch):
    reviewer, store, _, _, _, args = harness()

    def fail(*_, **__):
        raise RuntimeError("private_db_secret")

    monkeypatch.setattr(store, "prune_raw", fail)
    result = reviewer.build(**args)
    assert result["checkpoint_retention"] == "UNKNOWN"
    assert result["component_status_counts"]["spot"] == {"OBSERVED": 12}


def test_selection_cannot_be_silently_truncated_or_outcome_filtered():
    reviewer, _, market, _, _, args = harness()
    args["precursor"]["fixed_cohort"] = [f"COIN{i}USDT" for i in range(40)]
    report = reviewer.build(**args)
    assert report["reason"] == "FLOW_SELECTION_EXCEEDS_BUDGET" and not market.calls


@pytest.mark.parametrize(
    "field,value", [("date", "2020-01-01"), ("start_ms", True), ("end_ms", 1)]
)
def test_invalid_day_is_rejected_before_storage_or_market(field, value):
    reviewer, store, market, _, _, args = harness()
    args["window"][field] = value
    with pytest.raises(ValueError):
        reviewer.build(**args)
    assert not store.rows and not market.calls


def test_job_adds_flow_report_and_refreshes_only_when_flow_digest_changes():
    worker, store, market = job()
    data = [unavailable_review("TEST_SNAPSHOT_1")]

    class Reviewer:
        def build(self, **_):
            return data[0]

    worker.flow_reviewer = Reviewer()
    worker.run_day(JOB_WINDOW)
    worker.run_day(JOB_WINDOW)
    assert len(store.saved_reports) == 1
    data[0] = unavailable_review("TEST_SNAPSHOT_2")
    worker.run_day(JOB_WINDOW)
    assert len(store.saved_reports) == 2 and market.requests == 5
    assert store.saved_reports[-1]["flow_review"] == data[0]


def test_job_flow_failure_does_not_block_existing_account_evidence():
    worker, store, _ = job()

    class Reviewer:
        def build(self, **_):
            raise RuntimeError("private_error_secret")

    worker.flow_reviewer = Reviewer()
    worker.run_day(JOB_WINDOW)
    assert (
        store.saved_reports[0]["flow_review"]["reason"] == "FLOW_RESEARCH_UNAVAILABLE"
    )
    assert store.saved_reports[0]["account_evidence"]["symbols"]
    assert "private_error_secret" not in canonical(store.saved_reports[0])


def test_daily_digest_shows_flow_coverage_not_prediction_or_independent_sample_count():
    reviewer, _, _, _, _, args = harness()
    flow = reviewer.build(**args)
    worker, store, _ = job()
    worker.run_day(JOB_WINDOW)
    report = store.saved_reports[0]
    report["flow_review"] = flow
    message = format_digest(report)
    assert (
        "现货 12/12" in message
        and "持仓量 12/12" in message
        and "资金费 12/12" in message
    )
    assert "非独立样本" in message and "历史补取" in message
    assert len(message) <= 3500
    flow["component_status_counts"]["spot"]["OBSERVED"] = "private_corrupt_count"
    assert "private_corrupt_count" not in format_digest(report)
