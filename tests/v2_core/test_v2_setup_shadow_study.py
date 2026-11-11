"""Independent bounded setup sampling: immutable inputs, no trading writes."""

import json
from copy import deepcopy
from dataclasses import asdict
from itertools import chain, repeat
from uuid import uuid4

import pytest
from test_v2_daily_mover_replay import (
    END,
    NOW,
    OBSERVED,
    SCOPE,
    START,
    Connection,
    QueryError,
    _insert,
    assert_digest,
    fixture,
)

import services.v2_setup_shadow_study as study
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import DecisionEvidence, canonical, digest
from v2_core.producer import market_envelope
from v2_core.strategy import StrategyScope

database = database_fixture


class StudyConnection(Connection):
    def __init__(self, rows=(), metadata=(), *, error_at="replay", **kwargs):
        super().__init__(rows, **kwargs)
        self.metadata = deepcopy(list(metadata))
        self.error_at = error_at

    def execute(self, sql, params=None):
        if sql == study._SELECT_SQL:
            self.calls.append((sql, params))
            self.last, self.params = sql, params
            if self.error and self.error_at == "selection":
                raise QueryError(self.error)
            return self
        return super().execute(sql, params)

    def fetchall(self):
        if self.last == study._SELECT_SQL:
            return deepcopy(self.metadata)
        return super().fetchall()


def rehash(row):
    proof = DecisionEvidence(row[2], canonical(row[10]), canonical(row[11]))
    row[0], row[1] = proof.evidence_ref, proof.config_digest
    return row


def reseal_prior(prior):
    """Recompute integrity hashes without claiming metadata is trustworthy."""
    prior["input_digest"] = digest(canonical(prior["selection"]))
    payload = {key: value for key, value in prior.items() if key != "content_digest"}
    prior["content_digest"] = digest(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return prior


def opportunity_hash(prior, row):
    return digest(
        canonical(
            {
                "scope_key": prior["scope_key"],
                "symbol": row["symbol"],
                "side": row["side"],
                "bar_close_ms": row["bar_close_ms"],
            }
        )
    )


def clone_record(base, *, symbol="BTCUSDT", source="s3", delay=0):
    row = deepcopy(base)
    row[3] = str(uuid4())
    row[5] = source
    row[8] += delay
    row[10]["source"] = source
    row[11].update(signal_id=row[3], symbol=symbol, source=source)
    row[12]["symbol"] = symbol
    context = row[11]["features"]["context"]
    context["symbol"] = symbol
    market = context["directional"]["market"]
    market["trend_structure"]["symbol"] = symbol
    context["sources"]["s3"] = market_envelope(
        "s3", "SANDBOX", symbol, market["input_evidence"]["closed_at"], market
    )
    return rehash(row)


def metadata(row, **changes):
    context = row[11]["features"]["context"]
    values = {
        "hour": row[8] // study.HOUR * study.HOUR,
        "consumer": row[4],
        "ref": row[0],
        "decided": row[8],
        "source": row[5],
        "environment": row[6],
        "symbol": row[12]["symbol"],
        "event": row[12]["signal"],
        "observed": str(row[12]["observed_at"]),
        "bar": str(
            context["directional"]["market"]["trend_structure"]["last_closed_at_ms"]
        ),
        **changes,
    }
    return tuple(
        values[key]
        for key in (
            "hour",
            "consumer",
            "ref",
            "decided",
            "source",
            "environment",
            "symbol",
            "event",
            "observed",
            "bar",
        )
    )


def synthetic_metadata(index, *, hour=OBSERVED, side="SELL", bar=None):
    bar = hour if bar is None else bar
    return (
        hour,
        StrategyScope(
            **asdict(SCOPE), producer="s8" if side == "SELL" else "s6"
        ).consumer,
        f"{index + 1:064x}",
        hour + index + 1,
        "s3",
        "SANDBOX",
        f"COIN{index}USDT",
        "TREND_DOWN" if side == "SELL" else "TREND_UP",
        str(hour),
        str(bar),
    )


def load(report, rows=(), *, metadata_rows=None, prior=None, as_of_ms=NOW, **kwargs):
    conn = StudyConnection(
        rows,
        [metadata(row) for row in rows] if metadata_rows is None else metadata_rows,
        **kwargs,
    )
    return study.load_setup_study(
        conn, SCOPE, report=report, as_of_ms=as_of_ms, prior=prior
    ), conn


def test_legacy_policy_study_reuses_prior_selection_and_exposes_adapter(monkeypatch):
    report, row = fixture()
    for key in (
        "trend.breakout_body_guard_enabled",
        "trend.entry_quality_shadow_enabled",
    ):
        row[10]["policy"].pop(key)
    rehash(row)
    # Simulate the sealed pre-upgrade result without changing its selection.
    with monkeypatch.context() as patch:
        patch.setattr(
            study,
            "_project",
            lambda *args: {"status": "UNKNOWN", "reason": "INCOMPLETE_FROZEN_POLICY"},
        )
        prior, _ = load(report, [row])
    original = deepcopy(prior)
    assert prior["summary"]["unknown_count"] == 1
    result, conn = load(report, [row], prior=prior)
    assert result["summary"]["baseline_matched_count"] == 1
    assert result["summary"]["unknown_count"] == 0
    assert result["selection"] == prior["selection"]
    assert result["input_digest"] == prior["input_digest"]
    assert (
        result["samples"][0]["policy_compatibility"]["mode"]
        == "EXPLICIT_LEGACY_SCHEMA_ADAPTER"
    )
    assert not any(sql == study._SELECT_SQL for sql, _ in conn.calls)
    assert prior == original
    assert_digest(result)


def test_study_selects_account_decisions_independently_of_mover_rankings():
    report, row = fixture()
    report.pop("rows")
    report.pop("gate_shadow")
    report.pop("account_evidence")
    original = deepcopy((report, row))
    result, conn = load(report, [row])
    assert result["status"] == "AVAILABLE"
    assert result["summary"]["selected_count"] == 1
    assert result["summary"]["baseline_matched_count"] == 1
    assert result["summary"]["alternatives"]["BREAKOUT_VOLUME075"] == {
        "market_candidate_count": 1,
        "new_market_candidate_count": 1,
    }
    assert result["summary"]["alternatives"]["PULLBACK_VOLUME075"] == {
        "market_candidate_count": 0,
        "new_market_candidate_count": 0,
    }
    assert result["samples"][0]["status"] == "BASELINE_MATCHED"
    assert result["coverage"]["population_complete"] is False
    assert result["plan"]["selection_uses_rankings_or_future_outcomes"] is False
    selected_query = next(
        params for sql, params in conn.calls if sql == study._SELECT_SQL
    )
    assert selected_query[:2] == (START, END)
    assert selected_query[-1] == NOW
    assert all("ranking" not in sql.lower() for sql, _ in conn.calls)
    assert (report, row) == original
    assert_digest(result)
    assert len(json.dumps(result).encode()) < 512 * 1024
    for private in (
        SCOPE.account_id,
        row[3],
        row[2],
        "PRIVATE_RATIONALE",
        '"context"',
        '"config"',
    ):
        assert private not in json.dumps(result)
    assert all(
        result[key] is False
        for key in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )


def test_hourly_scan_is_32_rows_plus_sentinel_and_four_opportunities():
    report, _ = fixture()
    rows = [synthetic_metadata(i) for i in range(33)]
    result, conn = load(report, metadata_rows=rows)
    selected = result["selection"]["rows"]
    assert [row["evidence_ref"] for row in selected] == [row[2] for row in rows[:4]]
    hour = next(
        item
        for item in result["selection"]["hours"]
        if item["hour_start_ms"] == OBSERVED
    )
    assert hour == {
        "hour_start_ms": OBSERVED,
        "observed_metadata_count": 33,
        "scan_truncated": True,
        "selected_count": 4,
        "duplicates_removed": 0,
    }
    assert result["selection"]["excluded_reasons"] == {"HOURLY_SELECTION_LIMIT": 28}
    assert result["coverage"]["population_complete"] is False
    assert len(result["selection"]["hours"]) == 24
    assert next(params for sql, params in conn.calls if sql == study._REPLAY_SQL)[
        1
    ] == [row[2] for row in rows[:4]]
    # If only the sentinel is valid, it still cannot become a selected sample.
    malformed = [(*row[:4], "quant", *row[5:]) for row in rows[:32]] + rows[32:]
    sentinel_only, sentinel_conn = load(report, metadata_rows=malformed)
    assert sentinel_only["selection"]["rows"] == []
    assert sentinel_only["selection"]["excluded_reasons"] == {
        "MISSING_OR_INVALID_METADATA": 32
    }
    assert not any(sql == study._REPLAY_SQL for sql, _ in sentinel_conn.calls)


def test_all_day_selection_is_capped_96_and_frozen_inputs_batched_20():
    report, _ = fixture()
    rows = [
        synthetic_metadata(hour_index * 100 + i, hour=START + hour_index * study.HOUR)
        for hour_index in range(24)
        for i in range(33)
    ]
    result, conn = load(report, metadata_rows=rows)
    assert len(result["selection"]["rows"]) == 96
    assert (
        result["summary"]["selected_count"] == result["summary"]["unknown_count"] == 96
    )
    batches = [params[1] for sql, params in conn.calls if sql == study._REPLAY_SQL]
    assert list(map(len, batches)) == [20, 20, 20, 20, 16]
    assert all(
        hour["scan_truncated"] and hour["selected_count"] == 4
        for hour in result["selection"]["hours"]
    )
    assert result["coverage"]["population_complete"] is False


def test_same_symbol_side_closed_bar_deduplicates_across_source_and_hour():
    report, base = fixture()
    initial = clone_record(base)
    other_source = clone_record(base, source="tv_bridge", delay=1)
    next_hour = clone_record(base, delay=study.HOUR)
    different_side_report, different_side = fixture(side="LONG")
    assert different_side_report["scope_key"] == report["scope_key"]
    different_side[8] += 2
    different_bar = metadata(
        next_hour,
        ref="f" * 64,
        bar=str(OBSERVED + study.BAR),
        observed=str(OBSERVED + study.BAR),
        decided=OBSERVED + study.HOUR + 12,
    )
    records = [initial, other_source, different_side, next_hour]
    metas = [
        metadata(initial),
        metadata(other_source),
        metadata(different_side),
        metadata(next_hour),
        different_bar,
    ]
    result, _ = load(report, records, metadata_rows=metas)
    assert len(result["selection"]["rows"]) == 3
    assert [row["evidence_ref"] for row in result["selection"]["rows"]] == [
        initial[0],
        different_side[0],
        "f" * 64,
    ]
    assert sum(hour["duplicates_removed"] for hour in result["selection"]["hours"]) == 2
    assert result["summary"]["baseline_matched_count"] == 2


def test_late_decision_uses_decision_day_even_when_observation_is_previous_day():
    report, row = fixture()
    report["window"] = {"start_ms": END, "end_ms": END + study.DAY}
    row[8] = END + 100
    result, _ = load(report, [row], as_of_ms=END + study.DAY + 1000)
    assert result["summary"]["baseline_matched_count"] == 1
    sample = result["samples"][0]
    assert sample["observed_ms"] == OBSERVED < result["window_start_ms"]
    assert sample["decided_ms"] == END + 100
    assert sample["bar_close_ms"] == OBSERVED


@pytest.mark.parametrize(
    "defect", ["hash", "bar", "incomplete_policy", "unbound_market"]
)
def test_frozen_binding_failures_remain_unknown_without_candidate_promotion(defect):
    report, row = fixture()
    selected = metadata(row)
    if defect == "hash":
        row[1] = "0" * 64
    elif defect == "bar":
        selected = metadata(row, bar=str(OBSERVED - study.BAR))
    elif defect == "incomplete_policy":
        row[10]["policy"].pop("trend.max_atr_pct")
        rehash(row)
        selected = metadata(row)
    else:
        row[11]["features"]["context"]["sources"]["s3"]["symbol"] = "ETHUSDT"
        rehash(row)
        selected = metadata(row)
    result, _ = load(report, [row], metadata_rows=[selected])
    assert result["summary"]["unknown_count"] == 1
    assert result["summary"]["baseline_matched_count"] == 0
    assert result["summary"]["alternatives"] == {}
    assert result["samples"][0]["status"] == "UNKNOWN"
    if defect == "bar":
        assert result["samples"][0]["reason"] == "FROZEN_BAR_BINDING_MISMATCH"
    if defect == "hash":
        assert result["samples"][0]["reason"] == "FROZEN_DIGEST_MISMATCH"
    if defect == "incomplete_policy":
        assert result["samples"][0]["reason"] == "INCOMPLETE_FROZEN_POLICY"
    if defect == "unbound_market":
        assert result["samples"][0]["reason"] == "TREND_STRUCTURE_UNCONFIRMED"


def test_selection_is_fixed_before_evaluation_and_invalid_rows_are_not_replaced(
    monkeypatch,
):
    report, base = fixture()
    records = [clone_record(base, symbol=f"COIN{i}USDT", delay=i) for i in range(5)]
    selected = [metadata(row) for row in records]
    records[0][1] = "0" * 64
    conn = StudyConnection(records, selected)
    calls = []
    real = study.evaluate_setup_shadow

    def observed(*args, **kwargs):
        calls.append(kwargs["evidence_ref"])
        assert sum(sql == study._SELECT_SQL for sql, _ in conn.calls) == 1
        queried = next(
            params[1] for sql, params in conn.calls if sql == study._REPLAY_SQL
        )
        assert queried == [record[0] for record in records[:4]]
        return real(*args, **kwargs)

    monkeypatch.setattr(study, "evaluate_setup_shadow", observed)
    result = study.load_setup_study(conn, SCOPE, report=report, as_of_ms=NOW)
    assert result["summary"]["selected_count"] == 4
    assert result["summary"]["unknown_count"] == 1
    assert result["summary"]["baseline_matched_count"] == 3
    assert calls == [row[0] for row in records[1:4]]
    assert records[4][0] not in json.dumps(result)


def test_recorded_baseline_mismatch_cannot_become_a_counted_candidate():
    report, row = fixture(recorded_reason="TREND_NO_FRESH_SETUP")
    result, _ = load(report, [row])
    assert result["summary"]["baseline_mismatch_count"] == 1
    assert result["summary"]["baseline_matched_count"] == 0
    assert result["summary"]["alternatives"] == {}
    sample = result["samples"][0]
    assert sample["status"] == "BASELINE_MISMATCH"
    assert sample["baseline_market_reason"] == "TREND_VOLUME_UNCONFIRMED"
    assert sample["alternatives"] == {}
    assert result["cohorts"] == []


def test_prior_sealed_selection_resists_later_appended_earlier_decisions():
    report, row = fixture()
    first, _ = load(report, [row])
    new = clone_record(row, symbol="ETHUSDT")
    new[8] -= 1
    second, conn = load(
        report,
        [new, row],
        metadata_rows=[metadata(new), metadata(row)],
        prior=first,
        as_of_ms=NOW + 1000,
    )
    assert second["selection"] == first["selection"]
    assert second["input_digest"] == first["input_digest"]
    assert second["selection"]["selected_at_ms"] == NOW
    assert second["summary"]["selected_count"] == 1
    assert second["samples"][0]["evidence_ref"] == row[0]
    assert not any(sql == study._SELECT_SQL for sql, _ in conn.calls)
    assert next(
        params[1] for sql, params in conn.calls if sql == study._REPLAY_SQL
    ) == [row[0]]
    assert_digest(second)


def test_tampered_existing_selection_fails_closed_instead_of_resampling():
    report, row = fixture()
    first, _ = load(report, [row])
    first["selection"]["rows"][0]["symbol"] = "PRIVATE_TAMPER"
    result, conn = load(report, [row], prior=first)
    assert conn.calls == []
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "INVALID_PRIOR_SELECTION"
    assert result["selection_freeze_failed"] is True
    assert result["summary"]["baseline_matched_count"] == 0
    assert result["selection"] is None
    assert "PRIVATE_TAMPER" not in json.dumps(result)


@pytest.mark.parametrize(
    "defect",
    [
        "selection_extra",
        "row_extra",
        "hour_extra",
        "exclusion_extra",
        "plan",
        "execution_permission",
        "parameter_permission",
        "promotion_permission",
        "opportunity_hash",
        "duplicate_opportunity",
        "five_per_hour",
        "hour_count",
        "hour_selected_mismatch",
        "hour_bool_count",
        "hour_not_bool_truncated",
        "excluded_bool_count",
        "negative_exclusions",
        "future_selection",
        "side_unhashable",
        "source_unhashable",
        "event_unhashable",
    ],
)
def test_resealed_prior_requires_whitelist_safe_metadata_and_sampling_invariants(
    defect,
):
    report, record = fixture()
    original, _ = load(report, [record])
    prior = deepcopy(original)
    selection = prior["selection"]
    selected = selection["rows"][0]
    hour_index = (selected["decided_ms"] - START) // study.HOUR
    hour = selection["hours"][hour_index]
    if defect == "selection_extra":
        selection["private_context"] = "PRIVATE_PRIOR_DATA"
    elif defect == "row_extra":
        selected["account_id"] = "PRIVATE_PRIOR_DATA"
    elif defect == "hour_extra":
        hour["context"] = "PRIVATE_PRIOR_DATA"
    elif defect == "exclusion_extra":
        selection["excluded_reasons"]["PRIVATE_PRIOR_DATA"] = 1
    elif defect == "plan":
        prior["plan"]["baseline_min_volume_ratio"] = "0.5"
    elif defect in {
        "execution_permission",
        "parameter_permission",
        "promotion_permission",
    }:
        key = {
            "execution_permission": "execution_authorized",
            "parameter_permission": "automatic_parameter_updates",
            "promotion_permission": "promotion_authorized",
        }[defect]
        prior[key] = True
    elif defect == "opportunity_hash":
        selected["opportunity_ref"] = "f" * 64
    elif defect == "duplicate_opportunity":
        duplicate = deepcopy(selected)
        duplicate["source"] = "tv_bridge"
        duplicate["evidence_ref"] = "f" * 64
        duplicate["decided_ms"] += study.HOUR
        selection["rows"].append(duplicate)
        selection["hours"][hour_index + 1].update(
            observed_metadata_count=1, selected_count=1
        )
    elif defect == "five_per_hour":
        for index in range(4):
            extra = deepcopy(selected)
            extra.update(symbol=f"COIN{index}USDT", evidence_ref=f"{index + 1:064x}")
            extra["opportunity_ref"] = opportunity_hash(prior, extra)
            selection["rows"].append(extra)
        hour.update(selected_count=5, observed_metadata_count=5)
    elif defect == "hour_count":
        selection["hours"].pop()
    elif defect == "hour_selected_mismatch":
        hour["selected_count"] = 0
    elif defect == "hour_bool_count":
        hour["observed_metadata_count"] = True
    elif defect == "hour_not_bool_truncated":
        hour["scan_truncated"] = 0
    elif defect == "excluded_bool_count":
        selection["excluded_reasons"]["HOURLY_SELECTION_LIMIT"] = True
    elif defect == "negative_exclusions":
        selection["excluded_reasons"]["HOURLY_SELECTION_LIMIT"] = -1
    elif defect == "future_selection":
        selection["selected_at_ms"] = NOW + 1
    else:
        selected[defect.removesuffix("_unhashable")] = []
    reseal_prior(prior)
    result, conn = load(report, [record], prior=prior)
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "INVALID_PRIOR_SELECTION"
    assert result["selection_freeze_failed"] is True
    assert result["selection"] is None
    assert result["samples"] == []
    assert conn.calls == []
    assert "PRIVATE_PRIOR_DATA" not in json.dumps(result)
    assert all(
        result[key] is False
        for key in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )
    assert_digest(result)


def test_failed_selection_freeze_marker_persists_without_automatic_resampling():
    report, row = fixture()
    prior, _ = load(report, [row])
    prior["selection"]["rows"][0]["symbol"] = "PRIVATE_TAMPER"
    failed, first_conn = load(report, [row], prior=prior)
    assert failed["selection"] is None
    assert failed["selection_freeze_failed"] is True
    assert first_conn.calls == []
    again, second_conn = load(report, [row], prior=failed, as_of_ms=NOW + 1000)
    assert again["reason"] == "INVALID_PRIOR_SELECTION"
    assert again["selection_freeze_failed"] is True
    assert again["selection"] is None
    assert second_conn.calls == []
    assert "PRIVATE_TAMPER" not in json.dumps(again)


def test_initial_read_failure_without_a_frozen_selection_can_retry():
    report, row = fixture()
    failed, _ = load(report, [row], error_at="selection", error="57014")
    assert failed["selection"] is None
    assert not failed.get("selection_freeze_failed")
    recovered, conn = load(report, [row], prior=failed, as_of_ms=NOW + 1000)
    assert recovered["summary"]["baseline_matched_count"] == 1
    assert recovered["selection"]["selected_at_ms"] == NOW + 1000
    assert sum(sql == study._SELECT_SQL for sql, _ in conn.calls) == 1


def test_successful_empty_selection_remains_frozen_after_late_records_arrive():
    report, row = fixture()
    empty, _ = load(report)
    assert empty["selection"]["rows"] == []
    later, conn = load(report, [row], prior=empty, as_of_ms=NOW + 1000)
    assert later["selection"] == empty["selection"]
    assert later["input_digest"] == empty["input_digest"]
    assert later["summary"]["selected_count"] == 0
    assert later["coverage"]["population_complete"] is False
    assert conn.calls == []


@pytest.mark.parametrize(
    "readonly,isolation,reason",
    [
        ("off", "repeatable read", "READ_ONLY_REQUIRED"),
        ("on", "read committed", "STABLE_SNAPSHOT_REQUIRED"),
    ],
)
def test_readonly_and_stable_snapshot_are_required_before_sampling(
    readonly, isolation, reason
):
    report, row = fixture()
    result, conn = load(report, [row], readonly=readonly, isolation=isolation)
    assert result["reason"] == reason
    assert result["coverage"]["query_complete"] is False
    assert result["coverage"]["retryable"] is False
    assert not any(
        sql in {study._SELECT_SQL, study._REPLAY_SQL} for sql, _ in conn.calls
    )


@pytest.mark.parametrize("error_at", ["selection", "replay"])
@pytest.mark.parametrize(
    "code,reason",
    [
        ("57014", "QUERY_TIMEOUT"),
        ("42501", "READ_PERMISSION_MISSING"),
        ("42P01", "READ_SCHEMA_UNAVAILABLE"),
        ("55P03", "QUERY_LOCK_TIMEOUT"),
        ("other", "READ_FAILED"),
    ],
)
def test_query_errors_are_bounded_retryable_and_never_leak_database_details(
    error_at, code, reason
):
    report, row = fixture()
    result, conn = load(report, [row], error_at=error_at, error=code)
    assert result["coverage"]["query_complete"] is False
    assert result["coverage"]["retryable"] is True
    assert conn.rollbacks == 1
    if error_at == "selection":
        assert result["reason"] == reason
    else:
        assert result["samples"][0]["reason"] == reason
    assert "PRIVATE_DATABASE" not in json.dumps(result)
    assert_digest(result)


@pytest.mark.parametrize("expired_after_selection", [False, True])
def test_global_read_budget_stops_new_queries_without_losing_selected_refs(
    monkeypatch, expired_after_selection
):
    report, row = fixture()
    clock = chain([0, 0, 31] if expired_after_selection else [0, 31], repeat(31))
    monkeypatch.setattr(study.time, "monotonic", lambda: next(clock))
    result, conn = load(report, [row])
    assert result["coverage"]["retryable"] is True
    assert result["coverage"]["query_complete"] is False
    assert not any(sql == study._REPLAY_SQL for sql, _ in conn.calls)
    if expired_after_selection:
        assert result["selection"]["rows"][0]["evidence_ref"] == row[0]
        assert result["samples"][0]["reason"] == "READ_BUDGET_EXHAUSTED"
    else:
        assert result["reason"] == "READ_BUDGET_EXHAUSTED"
        assert conn.calls == []


def test_no_selected_samples_never_claims_complete_population():
    report, _ = fixture()
    result, conn = load(report)
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "NO_COMPARABLE_OPPORTUNITIES"
    assert result["summary"]["selected_count"] == 0
    assert result["coverage"]["query_complete"] is True
    assert result["coverage"]["population_complete"] is False
    assert result["selection"]["rows"] == []
    assert not any(sql == study._REPLAY_SQL for sql, _ in conn.calls)


def test_invalid_scope_binding_never_queries():
    report, row = fixture()
    report["scope_key"] = "0" * 64
    result, conn = load(report, [row])
    assert result["reason"] == "INVALID_STUDY_BINDING"
    assert conn.calls == []


def test_pg_actual_metadata_schema_and_scoped_frozen_join(database):
    report, row = fixture()
    report.pop("rows")
    report.pop("gate_shadow")
    _insert(database, row)
    other = clone_record(row, symbol="ETHUSDT")
    other[4] = '["BINANCE","another-account","SANDBOX","FUTURES","s8"]'
    _insert(database, other)
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        result = study.load_setup_study(conn, SCOPE, report=report, as_of_ms=NOW)
        assert result["summary"]["baseline_matched_count"] == 1
        assert result["samples"][0]["evidence_ref"] == row[0]
        assert result["samples"][0]["bar_close_ms"] == OBSERVED
        assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions"
        ).fetchone() == (2,)


def test_pg_decision_day_query_includes_previous_day_observation(database):
    report, row = fixture()
    report["window"] = {"start_ms": END, "end_ms": END + study.DAY}
    row[8] = END + 100
    _insert(database, row)
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        result = study.load_setup_study(
            conn, SCOPE, report=report, as_of_ms=END + study.DAY + 1000
        )
        assert result["summary"]["baseline_matched_count"] == 1
        assert result["samples"][0]["observed_ms"] == OBSERVED
        assert result["samples"][0]["decided_ms"] == END + 100


def test_pg_hourly_lateral_scan_has_sentinel_and_four_opportunity_cap(database):
    report, base = fixture()
    rows = [clone_record(base, symbol=f"COIN{i}USDT", delay=i) for i in range(34)]
    for row in rows:
        _insert(database, row)
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        result = study.load_setup_study(conn, SCOPE, report=report, as_of_ms=NOW)
        assert result["summary"]["selected_count"] == 4
        assert result["summary"]["baseline_matched_count"] == 4
        hour = next(
            hour
            for hour in result["selection"]["hours"]
            if hour["hour_start_ms"] == OBSERVED
        )
        assert hour["observed_metadata_count"] == 33
        assert hour["scan_truncated"] is True
        assert result["selection"]["excluded_reasons"] == {"HOURLY_SELECTION_LIMIT": 28}
        assert [sample["evidence_ref"] for sample in result["samples"]] == [
            row[0] for row in rows[:4]
        ]


def test_pg_missing_schema_failure_preserves_outer_readonly_transaction(database):
    report, _ = fixture()
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL search_path TO pg_catalog")
        result = study.load_setup_study(conn, SCOPE, report=report, as_of_ms=NOW)
        assert result["reason"] == "READ_SCHEMA_UNAVAILABLE"
        assert result["coverage"]["retryable"] is True
        assert conn.execute("SELECT 1").fetchone() == (1,)
