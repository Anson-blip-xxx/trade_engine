"""Frozen-input replay is scoped, causal, read-only and never a trade decision."""

import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

import pytest

import services.v2_daily_mover_replay as replay
from services.v2_trend_entry import evaluate_trend_market
from tests.support.v2_admission import database as database_fixture
from tests.v2_core.test_v2_mover_gate_shadow import frozen_sample
from v2_core.account_risk import AccountScope
from v2_core.evidence import DecisionEvidence, canonical, digest
from v2_core.strategy import StrategyScope

database = database_fixture
SCOPE = AccountScope("BINANCE", "replay-private-account", "SANDBOX", "FUTURES")
START, END, OBSERVED = 57_600_000, 144_000_000, 86_400_000
NOW = END + 1000


def fixture(*, side="SHORT", defect=None, recorded_reason=None):
    frozen = frozen_sample(side=side, regime="range")
    frozen["config"]["account_scope"] = asdict(SCOPE)
    frozen["context"]["account_scope"] = asdict(SCOPE)
    if defect:
        defect(frozen)
    reason = (
        recorded_reason
        or evaluate_trend_market(frozen["signal"], frozen["context"], frozen["config"])[
            0
        ].reason
    )
    identity = str(uuid4())
    snapshot = {
        "signal_id": identity,
        "observed_at": OBSERVED,
        "decided_at": OBSERVED + 2,
        "source": "s3",
        "symbol": "BTCUSDT",
        "rationale": "PRIVATE_RATIONALE",
        "features": {
            "context": frozen["context"],
            "signal": frozen["signal"]["features"],
            "evaluation": {"market_plan": {"reason": reason}},
        },
    }
    proof = DecisionEvidence(
        "private-version-v123", canonical(frozen["config"]), canonical(snapshot)
    )
    producer = "s6" if side == "LONG" else "s8"
    row = [
        proof.evidence_ref,
        proof.config_digest,
        proof.strategy_version,
        identity,
        StrategyScope(**asdict(SCOPE), producer=producer).consumer,
        "s3",
        "SANDBOX",
        OBSERVED + 10,
        OBSERVED + 11,
        OBSERVED + 1,
        json.loads(proof.config_json),
        json.loads(proof.snapshot_json),
        frozen["signal"],
    ]
    selected = {
        "evidence_ref": proof.evidence_ref,
        "source": "s3",
        "event": frozen["signal"]["signal"],
        "observed_ms": OBSERVED,
        "decided_ms": OBSERVED + 11,
        "market_sources": {
            "market_environment": "SANDBOX",
            "market_source": "BINANCE_FUTURES",
        },
    }
    report = {
        "account_environment": "SANDBOX",
        "scope_key": digest(canonical(asdict(SCOPE))),
        "window": {"start_ms": START, "end_ms": END},
        "as_of_ms": NOW,
        "account_evidence": {
            "account_scope": asdict(SCOPE),
            "window_start_ms": START,
            "window_end_ms": END,
            "as_of_ms": NOW,
        },
        "gate_shadow": {"samples": [{"evidence_ref": proof.evidence_ref}]},
        "rows": [
            {
                "symbol": "BTCUSDT",
                "direction": "BUY" if side == "LONG" else "SELL",
                "evidence": {"rejection_details": [{"reason_samples": [selected]}]},
            }
        ],
    }
    return report, row


class QueryError(Exception):
    def __init__(self, code):
        super().__init__("PRIVATE_DATABASE_PASSWORD_AND_APIKEY")
        self.sqlstate = code


class Connection:
    def __init__(
        self, rows=(), *, readonly="on", isolation="repeatable read", error=None
    ):
        self.rows = deepcopy(list(rows))
        self.readonly, self.isolation, self.error = readonly, isolation, error
        self.calls, self.rollbacks = [], 0
        self.last, self.params = None, None

    @contextmanager
    def transaction(self):
        try:
            yield
        except Exception:
            self.rollbacks += 1
            raise

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.last, self.params = sql, params
        if sql == replay._REPLAY_SQL and self.error:
            raise QueryError(self.error)
        return self

    def fetchone(self):
        return (
            self.readonly
            if self.last == "SHOW transaction_read_only"
            else self.isolation,
        )

    def fetchall(self):
        return [deepcopy(row) for row in self.rows if row[0] in self.params[1]]


def load(report, rows, **settings):
    return replay.load_frozen_replay_review(
        Connection(rows, **settings), SCOPE, report, as_of_ms=NOW
    )


def assert_digest(result):
    payload = {key: value for key, value in result.items() if key != "content_digest"}
    assert (
        hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        == result["content_digest"]
    )


def rehash(report, row):
    """Create a different valid immutable record, not a forged old digest."""
    old = row[0]
    proof = DecisionEvidence(row[2], canonical(row[10]), canonical(row[11]))
    row[0], row[1] = proof.evidence_ref, proof.config_digest
    for sample in report["gate_shadow"]["samples"]:
        if sample["evidence_ref"] == old:
            sample["evidence_ref"] = row[0]
    for outer in report["rows"]:
        for group in outer["evidence"]["rejection_details"]:
            for sample in group["reason_samples"]:
                if sample["evidence_ref"] == old:
                    sample["evidence_ref"] = row[0]


def test_actual_evaluator_comparison_bound_to_stored_reason_without_raw_export():
    report, row = fixture()
    original = deepcopy((report, row))
    result = load(report, [row])
    assert result["status"] == "AVAILABLE"
    assert result["summary"] == {
        "requested_count": 1,
        "replayed_count": 1,
        "baseline_matched_count": 1,
        "baseline_mismatch_count": 0,
        "unknown_count": 0,
        "volume_gate_changed_count": 1,
        "new_market_candidate_count": 1,
        "other_gate_still_failed_count": 0,
    }
    assert result["basis"] == "CURRENT_EVALUATOR_ON_FROZEN_INPUTS_BASELINE_CHECKED"
    assert result["historical_code_identity"] == "NOT_PROVEN"
    assert result["historical_program_replay"] is False
    assert result["coverage"] == {
        "query_complete": True,
        "all_inputs_available": True,
        "retryable": False,
    }
    assert result["retryable"] is False
    assert result["samples"][0]["baseline_market_reason"] == "TREND_VOLUME_UNCONFIRMED"
    assert result["samples"][0]["candidate_market_reason"] == "CANDIDATE"
    for flag in (
        "execution_authorized",
        "automatic_parameter_updates",
        "promotion_authorized",
    ):
        assert result[flag] is False
    encoded = json.dumps(result)
    for private in (
        SCOPE.account_id,
        row[3],
        row[2],
        "PRIVATE_RATIONALE",
        '"context"',
        '"config"',
        '"signal"',
        '"policy"',
    ):
        assert private not in encoded
    assert (report, row) == original
    assert_digest(result)


def test_recorded_baseline_mismatch_is_not_counted_as_new_candidate():
    report, row = fixture(recorded_reason="TREND_NO_FRESH_SETUP")
    result = load(report, [row])
    assert result["status"] == "PARTIAL"
    assert result["summary"]["baseline_mismatch_count"] == 1
    assert result["summary"]["replayed_count"] == 1
    assert result["summary"]["baseline_matched_count"] == 0
    assert result["summary"]["new_market_candidate_count"] == 0
    assert result["summary"]["volume_gate_changed_count"] == 0
    assert result["samples"][0]["new_market_candidate"] is False


def test_other_gate_remains_rejected_even_when_volume_changes():
    report, row = fixture(
        side="LONG",
        defect=lambda sample: sample["context"]["directional"].update(
            regime="weak_bear"
        ),
    )
    result = load(report, [row])
    assert result["status"] == "AVAILABLE"
    assert result["summary"]["volume_gate_changed_count"] == 1
    assert result["summary"]["new_market_candidate_count"] == 0
    assert result["samples"][0]["candidate_market_reason"] == "regime_conflict"


@pytest.mark.parametrize(
    "index,value",
    [
        (1, "0" * 64),
        (2, "changed-version"),
        (3, str(uuid4())),
        (4, '["BINANCE","another-account","SANDBOX","FUTURES","s8"]'),
        (5, "tv_bridge"),
        (6, "LIVE"),
        (7, NOW + 1),
        (8, NOW + 1),
        (9, NOW + 1),
        (7, OBSERVED - 1),
        (8, OBSERVED - 1),
        (9, OBSERVED - 1),
        (10, None),
        (11, None),
        (12, None),
    ],
)
def test_tampered_join_scope_hash_and_future_records_never_replay(index, value):
    report, row = fixture()
    row[index] = value
    result = load(report, [row])
    assert result["summary"]["replayed_count"] == 0
    assert result["summary"]["unknown_count"] == 1
    assert result["summary"]["new_market_candidate_count"] == 0


@pytest.mark.parametrize(
    "target,key,value",
    [
        (10, "strategy", "S6"),
        (
            10,
            "account_scope",
            asdict(AccountScope("BINANCE", "other", "SANDBOX", "FUTURES")),
        ),
        (10, "source", "quant"),
        (11, "symbol", "ETHUSDT"),
        (12, "symbol", "ETHUSDT"),
        (12, "signal", "TREND_UP"),
        (12, "observed_at", OBSERVED - 1),
        (11, "decided_at", NOW + 1),
        (11, "observed_at", START - 1),
        (10, "password", "NEVER_EXPORT_THIS"),
    ],
)
def test_raw_evidence_tampering_does_not_get_default_repaired(target, key, value):
    report, row = fixture()
    row[target][key] = value
    result = load(report, [row])
    assert result["summary"]["replayed_count"] == 0
    assert "NEVER_EXPORT_THIS" not in json.dumps(result)


@pytest.mark.parametrize(
    "defect",
    [
        lambda r: r.update(scope_key="0" * 64),
        lambda r: r.update(account_environment="LIVE"),
        lambda r: r.update(account_environment=[]),
        lambda r: r.update(as_of_ms=NOW - 1),
        lambda r: r["account_evidence"].update(as_of_ms=NOW + 1),
        lambda r: r["account_evidence"].update(as_of_ms=END - 1),
        lambda r: r["account_evidence"].update(account_scope={}),
        lambda r: r["window"].update(start_ms=START + 1),
        lambda r: r["gate_shadow"].update(samples=[{}] * 101),
        lambda r: r.update(rows=[{}] * 21),
    ],
)
def test_invalid_report_binding_never_queries(defect):
    report, row = fixture()
    defect(report)
    conn = Connection([row])
    result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
    assert result["reason"] == "REPLAY_INPUT_INVALID"
    assert conn.calls == []


@pytest.mark.parametrize(
    "readonly,isolation,reason",
    [
        ("off", "repeatable read", "REPLAY_READ_ONLY_REQUIRED"),
        ("on", "read committed", "REPLAY_STABLE_SNAPSHOT_REQUIRED"),
    ],
)
def test_caller_must_supply_stable_readonly_transaction(readonly, isolation, reason):
    report, row = fixture()
    result = load(report, [row], readonly=readonly, isolation=isolation)
    assert result["reason"] == reason
    assert result["summary"]["unknown_count"] == 1


@pytest.mark.parametrize(
    "code,reason",
    [
        ("42501", "REPLAY_PERMISSION_MISSING"),
        ("42P01", "REPLAY_SCHEMA_UNAVAILABLE"),
        ("57014", "REPLAY_QUERY_TIMEOUT"),
        ("other", "REPLAY_READ_UNAVAILABLE"),
    ],
)
def test_query_failures_are_safe_retryable_and_rollback_savepoint(code, reason):
    report, row = fixture()
    conn = Connection([row], error=code)
    result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
    assert result["reason"] == reason
    assert result["retryable"] is True and conn.rollbacks == 1
    assert result["coverage"]["query_complete"] is False
    assert result["summary"]["unknown_count"] == 1
    assert "PRIVATE_DATABASE_PASSWORD" not in json.dumps(result)


@pytest.mark.parametrize(
    "kind",
    ["selected_duplicate", "join_duplicate", "missing", "unbound", "conflicting"],
)
def test_missing_or_conflicting_evidence_is_unknown_not_extra_independent_samples(kind):
    report, row = fixture()
    rows = [row]
    if kind == "selected_duplicate":
        report["gate_shadow"]["samples"] *= 2
    elif kind == "join_duplicate":
        rows *= 2
    elif kind == "missing":
        rows = []
    elif kind == "unbound":
        report["rows"] = []
    else:
        group = report["rows"][0]["evidence"]["rejection_details"][0]
        alternate = deepcopy(group["reason_samples"][0])
        alternate["source"] = "tv_bridge"
        group["reason_samples"].append(alternate)
    result = load(report, rows)
    assert result["summary"]["baseline_matched_count"] == 0
    assert result["summary"]["unknown_count"] == len(report["gate_shadow"]["samples"])
    assert result["retryable"] is False


def test_incomplete_frozen_policy_is_never_filled_from_current_defaults():
    report, row = fixture()
    del row[10]["policy"]["entry.min_score"]
    proof = DecisionEvidence(row[2], canonical(row[10]), canonical(row[11]))
    old = row[0]
    row[0], row[1] = proof.evidence_ref, proof.config_digest
    report["gate_shadow"]["samples"][0]["evidence_ref"] = row[0]
    report["rows"][0]["evidence"]["rejection_details"][0]["reason_samples"][0][
        "evidence_ref"
    ] = row[0]
    assert row[0] != old
    result = load(report, [row])
    assert result["unknown_reasons"] == {"INCOMPLETE_FROZEN_POLICY": 1}


def test_registered_legacy_schema_preserves_immutable_evidence_and_reports_adapter():
    report, row = fixture()
    for key in (
        "trend.breakout_body_guard_enabled",
        "trend.entry_quality_shadow_enabled",
    ):
        row[10]["policy"].pop(key)
    rehash(report, row)
    original = deepcopy((report, row))
    result = load(report, [row])
    assert result["summary"]["baseline_matched_count"] == 1
    assert result["summary"]["unknown_count"] == 0
    sample = result["samples"][0]
    assert sample["evidence_ref"] == row[0]
    assert sample["config_digest"] == row[1]
    assert sample["policy_compatibility"]["source_config_digest"] == row[1]
    assert sample["policy_compatibility"]["mode"] == "EXPLICIT_LEGACY_SCHEMA_ADAPTER"
    assert result["historical_program_replay"] is False
    assert result["historical_code_identity"] == "NOT_PROVEN"
    assert (report, row) == original
    assert_digest(result)


def test_missing_context_is_unknown_without_retrying_immutable_history():
    report, row = fixture()
    del row[11]["features"]["context"]
    proof = DecisionEvidence(row[2], canonical(row[10]), canonical(row[11]))
    row[0] = proof.evidence_ref
    report["gate_shadow"]["samples"][0]["evidence_ref"] = row[0]
    report["rows"][0]["evidence"]["rejection_details"][0]["reason_samples"][0][
        "evidence_ref"
    ] = row[0]
    result = load(report, [row])
    assert result["unknown_reasons"] == {"FROZEN_CONTEXT_MISSING": 1}
    assert result["coverage"]["query_complete"] is True
    assert result["retryable"] is False


def test_earlier_evidence_cutoff_not_replaced_with_later_report_clock():
    report, row = fixture()
    report["account_evidence"]["as_of_ms"] = END
    conn = Connection([row])
    result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
    params = next(params for sql, params in conn.calls if sql == replay._REPLAY_SQL)
    assert params[3:] == (END, END, END)
    assert result["evidence_as_of_ms"] == END


def test_read_budget_prevents_new_queries(monkeypatch):
    report, row = fixture()
    clock = iter([0, 16])
    monkeypatch.setattr(replay.time, "monotonic", lambda: next(clock))
    conn = Connection([row])
    result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
    assert result["reason"] == "REPLAY_READ_BUDGET_EXCEEDED"
    assert result["retryable"] is True
    assert not any(sql == replay._REPLAY_SQL for sql, _ in conn.calls)


def test_remaining_budget_caps_each_sql_timeout(monkeypatch):
    report, row = fixture()
    clock = iter([0, 14.5, 14.6])
    monkeypatch.setattr(replay.time, "monotonic", lambda: next(clock))
    conn = Connection([row])
    result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
    assert result["summary"]["baseline_matched_count"] == 1
    assert ("SET LOCAL statement_timeout='500ms'", None) in conn.calls


@pytest.mark.parametrize(
    "defect,reason",
    [
        (
            lambda r: r[11]["features"]["context"].update(account_scope={}),
            "FROZEN_CONTEXT_BINDING_MISMATCH",
        ),
        (
            lambda r: r[11]["features"]["context"].update(environment="LIVE"),
            "FROZEN_CONTEXT_BINDING_MISMATCH",
        ),
        (
            lambda r: r[11]["features"]["context"].update(assembled_at=NOW + 1),
            "FROZEN_CONTEXT_BINDING_MISMATCH",
        ),
        (
            lambda r: r[11]["features"]["context"].update(assembled_at=OBSERVED - 1),
            "FROZEN_CONTEXT_BINDING_MISMATCH",
        ),
        (
            lambda r: r[11]["features"]["signal"].update(strength=99),
            "FROZEN_CONTEXT_BINDING_MISMATCH",
        ),
        (
            lambda r: r[11]["features"]["evaluation"]["market_plan"].update(
                reason="PRIVATE_UNRECOGNIZED_REASON"
            ),
            "FROZEN_BASELINE_REASON_UNAVAILABLE",
        ),
        (
            lambda r: r[10]["policy"].update({"trend.min_volume_ratio": "0.5"}),
            "FROZEN_INPUTS_NOT_REPLAYABLE",
        ),
    ],
)
def test_valid_hash_does_not_override_context_scope_baseline_or_study_plan(
    defect, reason
):
    report, row = fixture()
    defect(row)
    rehash(report, row)
    result = load(report, [row])
    assert result["unknown_reasons"] == {reason: 1}
    assert "PRIVATE_UNRECOGNIZED_REASON" not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"market_environment": "LIVE"},
        {"market_environment": "SANDBOX", "market_host": "fapi.binance.com"},
        {"market_environment": "SANDBOX", "market_source": "BINANCE_USDM_LIVE"},
        {},
    ],
)
def test_report_projection_cannot_claim_cross_environment_market_evidence(changes):
    report, row = fixture()
    report["rows"][0]["evidence"]["rejection_details"][0]["reason_samples"][0][
        "market_sources"
    ] = changes
    result = load(report, [row])
    assert result["summary"]["baseline_matched_count"] == 0
    assert result["unknown_reasons"] == {"FROZEN_INPUTS_NOT_REPLAYABLE": 1}


def test_oversized_snapshot_is_not_evaluated_even_if_fixture_bypasses_sql_cap(
    monkeypatch,
):
    report, row = fixture()
    row[11]["features"]["padding"] = "x" * 524288
    rehash(report, row)
    monkeypatch.setattr(
        replay,
        "evaluate_mover_gate_shadow",
        lambda *args, **kwargs: pytest.fail("large blob must not reach evaluator"),
    )
    result = load(report, [row])
    assert result["unknown_reasons"] == {"FROZEN_INPUTS_MISSING_OR_TOO_LARGE": 1}


def test_missing_original_version_is_unknown_not_current_version_fallback():
    report, row = fixture()
    row[2] = ""
    result = load(report, [row])
    assert result["summary"]["unknown_count"] == 1
    assert result["summary"]["replayed_count"] == 0


def test_only_selected_refs_are_queried_not_all_rejection_examples():
    report, row = fixture()
    report["rows"][0]["evidence"]["rejection_details"][0]["reason_samples"].append(
        {"evidence_ref": "0" * 64}
    )
    conn = Connection([row])
    result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
    params = next(params for sql, params in conn.calls if sql == replay._REPLAY_SQL)
    assert params[1] == [row[0]]
    assert result["summary"]["requested_count"] == 1


def test_unavailable_envelope_handles_non_dict_and_non_scalar_environment():
    for report in (
        None,
        [],
        {"account_environment": []},
        {"window": []},
        {"account_evidence": []},
    ):
        result = replay.unavailable_review(report, as_of_ms=NOW, reason=[])
        assert result["status"] == "UNKNOWN"
        assert result["reason"] == "REPLAY_READ_UNAVAILABLE"
        assert result["coverage"]["retryable"] == result["retryable"] is True
        assert_digest(result)


def test_samples_batched_twenty_at_a_time_and_only_public_rows_retained(monkeypatch):
    report, _ = fixture()
    report["gate_shadow"]["samples"] = []
    group = report["rows"][0]["evidence"]["rejection_details"][0]
    example = group["reason_samples"][0]
    group["reason_samples"] = []
    for index in range(41):
        ref = hashlib.sha256(str(index).encode()).hexdigest()
        report["gate_shadow"]["samples"].append({"evidence_ref": ref})
        group["reason_samples"].append({**example, "evidence_ref": ref})
    conn = Connection([])
    result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
    batches = [params[1] for sql, params in conn.calls if sql == replay._REPLAY_SQL]
    assert list(map(len, batches)) == [20, 20, 1]
    assert result["summary"]["unknown_count"] == 41
    assert result["retryable"] is False


def _insert(connect, row):
    with connect() as conn:
        conn.execute(
            "INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot,received_at) VALUES(%s,%s,%s,%s,%s::jsonb,to_timestamp(%s/1000.0))",
            (row[3], row[5], row[6], str(uuid4()), canonical(row[12]), row[9]),
        )
        conn.execute(
            "INSERT INTO v2_decision_evidence(evidence_ref,config_digest,strategy_version,config,snapshot,created_at) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,to_timestamp(%s/1000.0))",
            (row[0], row[1], row[2], canonical(row[10]), canonical(row[11]), row[7]),
        )
        conn.execute(
            "INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,evidence_ref,action,created_at) VALUES(%s,%s,%s,%s,'IGNORED',to_timestamp(%s/1000.0))",
            (str(uuid4()), row[4], row[3], row[0], row[8]),
        )


def test_pg_exact_scoped_join_and_causal_timestamp(database):
    report, row = fixture()
    _insert(database, row)
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
        assert result["summary"]["baseline_matched_count"] == 1
        assert result["summary"]["new_market_candidate_count"] == 1
        assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "field,value",
    [
        (4, '["BINANCE","other","SANDBOX","FUTURES","s8"]'),
        (7, NOW + 1),
        (8, NOW + 1),
        (9, NOW + 1),
        (6, "LIVE"),
    ],
)
def test_pg_excludes_other_account_environment_and_future_created_records(
    database, field, value
):
    report, row = fixture()
    row[field] = value
    _insert(database, row)
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
        assert result["summary"]["replayed_count"] == 0
        assert result["unknown_reasons"] == {"FROZEN_RECORD_MISSING_OR_CONFLICTING": 1}


def test_pg_missing_schema_rolls_back_only_nested_savepoint(database):
    report, _ = fixture()
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL search_path TO pg_catalog")
        result = replay.load_frozen_replay_review(conn, SCOPE, report, as_of_ms=NOW)
        assert result["reason"] == "REPLAY_SCHEMA_UNAVAILABLE"
        assert conn.execute("SELECT 1").fetchone() == (1,)
