"""Whole-account closing-day selection, settlement proof and bounded failures."""

import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

import services.v2_daily_account_review as review
import tests.v2_core.test_v2_daily_mover_evidence as evidence_helpers
from services.v2_daily_mover_execution_review import build_execution_review
from tests.support.v2_admission import database as database_fixture
from tests.v2_core.test_v2_daily_mover_evidence import add_trade
from tests.v2_core.test_v2_daily_mover_execution_review import projection
from v2_core.account_risk import AccountScope

database = database_fixture
SCOPE = AccountScope("BINANCE", "private-account-not-exported", "SANDBOX", "FUTURES")
START = 20000 * review.DAY_MS - 28_800_000
END = START + review.DAY_MS
NOW = END + 3_600_000


def digest(value, *, ensure_ascii=False):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=ensure_ascii,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def row(identity=None, **changes):
    values = {
        "identity": str(identity or UUID(int=1)),
        "symbol": "BTCUSDT",
        "side": "BUY",
        "decided": START - 2000,
        "opening": "2",
        "closing": "2",
        "opening_notional": "200",
        "closing_notional": "220",
        "fees": "0.2",
        "opened": START - 1000,
        "closed": START + 10000,
        "fills_valid": True,
        "all_known": True,
        "terminal": True,
        "orders_known": True,
        "maintenance": False,
    }
    values.update(changes)
    return tuple(values.values())


def label(identity=None, **changes):
    return {
        "state": "SETTLED",
        "intent_id": str(identity or UUID(int=1)),
        "closed_ms": START + 10000,
        "settlement_revision": 1,
        "settled_ms": START + 11000,
        "net_pnl": "19.8",
        **changes,
    }


def jev_row(identity=None, **changes):
    values = {
        "identity": str(identity or UUID(int=1)),
        "present": True,
        "source": "s3",
        "schema": "jev-entry-gate-v1",
        "status": "PASS",
        "reason": "JEV_SECOND_FILTER_PASSED",
        "fallback_cause": None,
        "enabled": True,
        "unavailable_action": "BASELINE",
        "trend": "0.8",
        "chase": "0.2",
        "minimum": "0.6",
        "maximum": "0.7",
    }
    values.update(changes)
    return tuple(values.values())


class QueryError(Exception):
    def __init__(self, code):
        super().__init__("DO_NOT_EXPORT_CONNECTION_OR_SECRET")
        self.sqlstate = code


class Connection:
    def __init__(
        self,
        details=None,
        *,
        candidates=None,
        readonly="on",
        isolation="repeatable read",
        error=None,
        jev=None,
        costs=None,
    ):
        self.details = [row()] if details is None else details
        self.candidates = (
            [(r[0],) for r in self.details] if candidates is None else candidates
        )
        self.readonly, self.error = readonly, error
        self.isolation = isolation
        self.jev = [] if jev is None else jev
        self.costs = [] if costs is None else costs
        self.calls, self.transactions, self.rollbacks, self.query = [], 0, 0, None

    @contextmanager
    def transaction(self):
        self.transactions += 1
        try:
            yield
        except Exception:
            self.rollbacks += 1
            raise

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.query = sql
        if self.error and sql == self.error[0]:
            raise QueryError(self.error[1])
        return self

    def fetchone(self):
        return (
            self.isolation
            if self.query == "SHOW transaction_isolation"
            else self.readonly,
        )

    def fetchall(self):
        if self.query == review._JEV_SQL:
            return self.jev
        if self.query == review._COST_SQL:
            return self.costs
        return self.candidates if self.query == review._CANDIDATES_SQL else self.details


@pytest.fixture
def labels(monkeypatch):
    values = {str(UUID(int=1)): label()}
    calls = []

    def load(conn, scope, identities, *, now):
        calls.append((scope, identities, now))
        return {identity: values.get(identity) for identity in identities}

    monkeypatch.setattr(review, "outcomes", load)
    return values, calls


def load(conn=None, **changes):
    return review.load_account_day_review(
        conn or Connection(),
        **{
            "scope": SCOPE,
            "start_ms": START,
            "end_ms": END,
            "as_of_ms": NOW,
            **changes,
        },
    )


def ranking_report(*details, labels=None):
    entries = []
    for raw in details:
        proof = (labels or {}).get(raw[0], label(raw[0]))
        entries.append(
            {
                "id": raw[0],
                "symbol": raw[1],
                "side": raw[2],
                "decided_ms": raw[3],
                "opening_quantity": raw[4],
                "closing_quantity": raw[5],
                "opening_notional": raw[6],
                "closing_notional": raw[7],
                "fees_usdt": raw[8],
                "opened_ms": raw[9],
                "closed_ms": raw[10],
                "fills_valid": raw[11],
                "settled_ms": proof["settled_ms"],
                "settlement_state": "SETTLED",
                "status": "FILLED_SETTLED",
                "net_pnl_available": True,
                "net_pnl": proof["net_pnl"],
                "gross_pnl_usdt": str(
                    (Decimal(raw[7]) - Decimal(raw[6])) * (1 if raw[2] == "BUY" else -1)
                ),
                "settlement_revision": proof["settlement_revision"],
                "source": "s3",
                "exit_reason": "PEAK_DRAWDOWN",
            }
        )
    symbols = sorted({item["symbol"] for item in entries})
    report = {
        "scope_key": digest(asdict(SCOPE), ensure_ascii=True),
        "account_environment": "SANDBOX",
        "as_of_ms": NOW,
        "window": {"start_ms": START, "end_ms": END, "timezone": "Asia/Shanghai"},
        "rows": [
            {
                "symbol": symbol,
                "direction": "BUY",
                "evidence": {
                    "trades": [
                        projection(item) for item in entries if item["symbol"] == symbol
                    ]
                },
            }
            for symbol in symbols
        ],
        "account_evidence": {
            "account_scope": asdict(SCOPE),
            "window_start_ms": START,
            "window_end_ms": END,
            "as_of_ms": NOW,
            "symbols": {
                symbol: {
                    "trades": [item for item in entries if item["symbol"] == symbol]
                }
                for symbol in symbols
            },
        },
    }
    report["execution_review"] = build_execution_review(report)
    return report


def test_full_day_summary_carry_in_scope_digest_readonly_and_no_identity(labels):
    conn = Connection()
    result = load(conn)
    assert result["status"] == "AVAILABLE"
    assert result["totals"]["settled_count"] == 1
    assert result["totals"]["net_usdt"] == "19.8"
    assert result["totals"]["wins"] == 1
    assert result["totals"]["profit_factor"] is None
    assert result["totals"]["recorded_fees_usdt"] == "0.2"
    assert result["totals"]["funding_usdt"] is None
    assert result["by_side"]["SELL"]["settled_count"] == 0
    assert result["coverage"]["query_complete"] is True
    assert result["coverage"]["full_closed_settlement_population_proven"] is True
    assert result["coverage"]["carry_in_settled_count"] == 1
    assert result["scope_key"] == digest(asdict(SCOPE), ensure_ascii=True)
    assert result["trades"][0]["trade_ref"] == digest(
        {"scope_key": result["scope_key"], "episode": str(UUID(int=1))}
    )
    assert result["content_digest"] == digest(
        {key: value for key, value in result.items() if key != "content_digest"}
    )
    text = json.dumps(result)
    assert SCOPE.account_id not in text and str(UUID(int=1)) not in text
    assert all(
        result[key] is False
        for key in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )
    assert labels[1] == [(SCOPE, [str(UUID(int=1))], NOW)]
    assert conn.transactions == 5 and conn.rollbacks == 0
    assert all("INSERT " not in sql and "UPDATE " not in sql for sql, _ in conn.calls)
    assert "v2_directional_outcomes" not in review._CANDIDATES_SQL + review._DETAILS_SQL


def test_query_candidate_is_close_based_not_entry_or_ranking_scoped():
    sql = review._CANDIDATES_SQL
    assert "o.leg='CLOSE'" in sql and "f.occurred_at_ms>=%s" in sql
    assert "i.created_at>=" not in sql and "payload->>'symbol'" not in sql
    assert "LIMIT 501" in sql and "SELECT DISTINCT" in sql


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": asdict(SCOPE)},
        {"start_ms": True},
        {"start_ms": START + 1},
        {"end_ms": END + review.DAY_MS},
        {"as_of_ms": END - 1},
        {"as_of_ms": float(NOW)},
        {"as_of_ms": 2**53},
    ],
)
def test_invalid_input_does_not_query(changes):
    conn = Connection()
    result = load(conn, **changes)
    assert result["reason"] == "ACCOUNT_REVIEW_INPUT_INVALID"
    assert result["totals"] is None and conn.calls == []


def test_readonly_is_required_before_any_select():
    conn = Connection(readonly="off")
    result = load(conn)
    assert result["reason"] == "READ_ONLY_REQUIRED"
    assert result["coverage"]["retryable"] is False
    assert conn.calls == [("SHOW transaction_read_only", None)]


@pytest.mark.parametrize("isolation", ["read committed", "read uncommitted", None])
def test_mixed_snapshot_is_not_allowed_or_retried(isolation):
    conn = Connection(isolation=isolation)
    result = load(conn)
    assert result["reason"] == "STABLE_SNAPSHOT_REQUIRED"
    assert result["coverage"]["retryable"] is False
    assert all(sql.startswith("SHOW ") for sql, _ in conn.calls)


def test_serializable_snapshot_is_also_supported(labels):
    assert load(Connection(isolation="serializable"))["status"] == "AVAILABLE"


@pytest.mark.parametrize("sql", [review._CANDIDATES_SQL, review._DETAILS_SQL])
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("42501", "READ_PERMISSION_MISSING"),
        ("57014", "QUERY_TIMEOUT"),
        ("55P03", "QUERY_LOCK_TIMEOUT"),
        ("42P01", "READ_SCHEMA_UNAVAILABLE"),
        ("XXX", "READ_FAILED"),
    ],
)
def test_query_failure_is_rollback_safe_retryable_and_not_zero(sql, state, reason):
    conn = Connection(error=(sql, state))
    result = load(conn)
    assert result["reason"] == reason
    assert result["totals"] is None and result["coverage"]["query_complete"] is False
    assert result["coverage"]["retryable"] is True and conn.rollbacks == 1
    assert "DO_NOT_EXPORT" not in json.dumps(result)


def test_no_close_candidates_is_verified_empty_not_unavailable(labels):
    conn = Connection([])
    result = load(conn)
    assert result["status"] == "AVAILABLE" and result["totals"]["net_usdt"] == "0"
    assert result["coverage"]["candidate_count"] == 0 and labels[1] == []
    assert conn.transactions == 1


@pytest.mark.parametrize("count", [1, 100, 101, 499, 500])
def test_bounded_outcome_batches_and_complete_day(count, labels):
    rows = [row(UUID(int=i + 1)) for i in range(count)]
    labels[0].update({raw[0]: label(raw[0]) for raw in rows})
    result = load(Connection(rows))
    assert (
        result["status"] == "AVAILABLE" and result["totals"]["settled_count"] == count
    )
    assert all(len(call[1]) <= 100 for call in labels[1])
    assert len(labels[1]) == (count + 99) // 100


def test_501_sentinel_never_reports_partial_prefix_as_whole_account(labels):
    conn = Connection([row(UUID(int=i + 1)) for i in range(501)])
    result = load(conn)
    assert result["reason"] == "ACCOUNT_CANDIDATE_LIMIT"
    assert result["totals"] is None and result["trades"] == []
    assert result["coverage"]["truncated"] is True
    assert result["coverage"]["query_complete"] is False
    assert labels[1] == [] and conn.transactions == 1


@pytest.mark.parametrize(
    "candidates",
    [
        [("bad-secret",)],
        [(str(UUID(int=1)),)] * 2,
        [(str(UUID(int=1)), "unexpected")],
        [()],
    ],
)
def test_bad_candidate_identity_never_escapes(candidates, labels):
    result = load(Connection(candidates=candidates))
    assert result["reason"] == "ACCOUNT_CANDIDATE_DATA_INVALID"
    assert result["totals"] is None and "bad-secret" not in json.dumps(result)
    assert labels[1] == []


def test_missing_detail_rows_is_not_a_complete_day(labels):
    result = load(Connection([], candidates=[(str(UUID(int=1)),)]))
    assert result["reason"] == "ACCOUNT_DETAIL_COVERAGE_INVALID"
    assert result["totals"] is None


@pytest.mark.parametrize(
    ("changes", "reason", "complete"),
    [
        ({"maintenance": True}, "MAINTENANCE_EXCLUDED", True),
        ({"all_known": False}, "LATER_FILL_OR_ORDER_STATE", False),
        ({"orders_known": False}, "LATER_FILL_OR_ORDER_STATE", False),
        ({"fills_valid": False}, "INCONSISTENT_FILL_PATH", False),
        ({"opening": "0"}, "INCONSISTENT_FILLS", False),
        ({"closing": "3"}, "INCONSISTENT_FILLS", False),
        ({"closing": "1"}, "OPEN_OR_PARTIAL_EXIT", True),
        ({"decided": START + 10}, "INVALID_FILL_TIMING", False),
        ({"closed": NOW + 1}, "INVALID_FILL_TIMING", False),
        ({"closed": END}, "NOT_CLOSED_IN_REVIEW_DAY", True),
        ({"closed": START - 1}, "NOT_CLOSED_IN_REVIEW_DAY", True),
        ({"terminal": False}, "AWAITING_TERMINAL_ORDERS", False),
        ({"symbol": "SECRET_VALUE"}, "TRADE_DIRECTION_OR_SYMBOL_INVALID", False),
        ({"side": "BAD"}, "TRADE_DIRECTION_OR_SYMBOL_INVALID", False),
        ({"opening_notional": "0"}, "INVALID_ACCOUNTING_NUMBER", False),
        ({"fees": "NaN"}, "INVALID_ACCOUNTING_NUMBER", False),
        ({"closing_notional": "Infinity"}, "INVALID_ACCOUNTING_NUMBER", False),
        ({"opening": True}, "INVALID_ACCOUNTING_NUMBER", False),
    ],
)
def test_exclusions_and_uncertainties_not_synthesized_into_zero(
    changes, reason, complete, labels
):
    result = load(Connection([row(**changes)]))
    assert result["coverage"]["excluded_reasons"] == {reason: 1}
    assert result["coverage"]["full_closed_settlement_population_proven"] is complete
    assert result["coverage"]["query_complete"] is True
    assert result["coverage"]["retryable"] is (
        reason in review._RETRYABLE_SETTLEMENT_STATES
    )
    assert result["totals"] == (review._summary([]) if complete else None)
    assert "SECRET_VALUE" not in json.dumps(result)


@pytest.mark.parametrize(
    "state", sorted(review._LABEL_STATES) + ["DO_NOT_EXPORT_SECRET", None]
)
def test_settlement_outcome_must_be_explicitly_settled(state, labels):
    labels[0][str(UUID(int=1))]["state"] = state
    result = load()
    assert result["trades"] == []
    assert result["coverage"]["excluded_reasons"] == {
        "SETTLEMENT_FILL_MISMATCH"
        if state == "OPEN_OR_PARTIAL_EXIT"
        else state
        if state in review._LABEL_STATES
        else "LABEL_UNAVAILABLE": 1
    }
    assert "DO_NOT_EXPORT_SECRET" not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"settled_ms": NOW + 1},
        {"settled_ms": START},
        {"settled_ms": True},
        {"closed_ms": START + 9999},
        {"intent_id": str(UUID(int=2))},
        {"settlement_revision": None},
        {"settlement_revision": True},
        {"settlement_revision": -1},
    ],
)
def test_settlement_exact_episode_timing_revision_binding(changes, labels):
    labels[0][str(UUID(int=1))].update(changes)
    result = load()
    assert result["coverage"]["excluded_reasons"] == {"SETTLEMENT_BINDING_INVALID": 1}
    assert result["totals"] is None


def test_missing_usdt_fees_is_unknown_not_zero(labels):
    result = load(Connection([row(fees=None)]))
    assert result["status"] == "AVAILABLE"
    assert result["totals"]["recorded_fees_usdt"] is None
    assert result["totals"]["recorded_fees_known_count"] == 0
    assert result["totals"]["net_usdt"] == "19.8"


def test_one_missing_settlement_is_explicit_verified_subset(labels):
    labels[0][str(UUID(int=2))] = label(
        UUID(int=2), state="SETTLEMENT_REVISION_CHANGED"
    )
    result = load(Connection([row(), row(UUID(int=2))]))
    assert result["status"] == "PARTIAL"
    assert result["totals"]["settled_count"] == 1
    assert result["coverage"]["full_closed_settlement_population_proven"] is False
    assert result["coverage"]["retryable"] is True
    assert result["ranking_vs_other"]["status"] == "UNKNOWN"


def test_label_batch_failure_does_not_abort_other_batch_and_cannot_claim_full_day(
    labels, monkeypatch
):
    rows = [row(UUID(int=i + 1)) for i in range(101)]

    def outcomes(conn, scope, ids, *, now):
        if len(ids) == 100:
            raise QueryError("57014")
        return {ids[0]: label(ids[0])}

    monkeypatch.setattr(review, "outcomes", outcomes)
    conn = Connection(rows)
    result = load(conn)
    assert result["status"] == "PARTIAL" and result["totals"]["settled_count"] == 1
    assert result["coverage"]["query_complete"] is False
    assert result["coverage"]["retryable"] is True
    assert result["coverage"]["excluded_reasons"] == {"QUERY_TIMEOUT": 100}
    assert conn.rollbacks == 1


def test_total_budget_stops_queries_and_deadline_clamps_last_timeout(
    labels, monkeypatch
):
    ticks = iter([0, 0, 19, 20])
    monkeypatch.setattr(review.time, "monotonic", lambda: next(ticks))
    conn = Connection()
    result = load(conn)
    assert result["coverage"]["excluded_reasons"] == {"READ_BUDGET_EXHAUSTED": 1}
    assert result["totals"] is None and labels[1] == []
    assert ("SET LOCAL statement_timeout='1000ms'", None) in conn.calls
    assert conn.transactions == 2


def test_wins_losses_factor_and_ranked_episode_complement_not_symbols(labels):
    second = row(UUID(int=2), side="SELL", closing_notional="205")
    labels[0][second[0]] = label(second[0], net_pnl="-5.2")
    report = ranking_report(row())
    before = deepcopy(report)
    result = load(Connection([row(), second]), report=report)
    assert result["totals"]["net_usdt"] == "14.6"
    assert result["totals"]["wins"] == 1 and result["totals"]["losses"] == 1
    assert Decimal(result["totals"]["win_rate_pct"]) == 50
    assert abs(
        Decimal(result["totals"]["profit_factor"]) - Decimal("19.8") / Decimal("5.2")
    ) < Decimal("0.0000000001")
    split = result["ranking_vs_other"]
    assert split["status"] == "AVAILABLE"
    assert split["ranking"]["net_usdt"] == "19.8"
    assert split["other"]["net_usdt"] == "-5.2"
    assert split["other"]["settled_count"] == 1  # same symbol, different episode!
    assert result["by_side"]["SELL"]["net_usdt"] == "-5.2"
    assert report == before


@pytest.mark.parametrize("key", ["scope_key", "account_environment", "as_of_ms"])
def test_wrong_report_binding_does_not_lose_account_totals(key, labels):
    report = ranking_report(row())
    report[key] = "BAD"
    result = load(report=report)
    assert result["status"] == "AVAILABLE"
    assert result["ranking_vs_other"]["reason"] == "RANKING_SCOPE_OR_CUTOFF_MISMATCH"


def test_tampered_ranking_even_with_resealed_hash_is_recomputed(labels):
    report = ranking_report(row())
    report["execution_review"]["totals"]["net_usdt"] = "1000"
    report["execution_review"]["content_digest"] = digest(
        {
            key: value
            for key, value in report["execution_review"].items()
            if key != "content_digest"
        }
    )
    result = load(report=report)
    assert result["ranking_vs_other"]["reason"] == "RANKING_PROJECTION_UNVERIFIED"
    assert result["totals"]["net_usdt"] == "19.8"


def test_missing_ranking_episode_in_whole_account_cannot_make_negative_complement(
    labels,
):
    report = ranking_report(row(UUID(int=2)))
    result = load(report=report)
    assert result["ranking_vs_other"]["reason"] == "RANKING_SETTLEMENT_MISMATCH"
    assert result["ranking_vs_other"]["other"] is None


def test_ranking_changed_net_revision_is_not_silently_subtracted(labels):
    report = ranking_report(row())
    labels[0][str(UUID(int=1))]["net_pnl"] = "18"
    result = load(report=report)
    assert result["totals"]["net_usdt"] == "18"
    assert result["ranking_vs_other"]["reason"] == "RANKING_SETTLEMENT_MISMATCH"


def test_unicode_symbol_and_input_order_have_stable_safe_digest(labels):
    first, second = row(symbol="牛来USDT"), row(UUID(int=2), side="SELL")
    labels[0][second[0]] = label(second[0], net_pnl="0")
    result = load(Connection([first, second]))
    reordered = load(Connection([second, first]))
    assert result["content_digest"] == reordered["content_digest"]
    assert result["totals"]["breakeven"] == 1
    assert "牛来USDT" in json.dumps(result, ensure_ascii=False)


def test_jev_all_budget_fallback_is_not_model_approval_and_sources_are_primary(labels):
    rows = [row(UUID(int=i + 1)) for i in range(51)]
    labels[0].update({raw[0]: label(raw[0], net_pnl="-0.2") for raw in rows})
    evidence = [
        jev_row(
            raw[0],
            status="FALLBACK",
            reason="JEV_BASELINE_FALLBACK",
            fallback_cause="TOKEN_BUDGET_PACED" if i < 49 else "TOKEN_BUDGET_EXHAUSTED",
            source="s3" if i < 50 else "tv_bridge",
            trend=None,
            chase=None,
        )
        for i, raw in enumerate(rows)
    ]
    conn = Connection(rows, jev=evidence)
    result = load(conn)
    jev = result["jev_participation"]
    assert jev["version"] == review.JEV_VERSION
    assert jev["status"] == "AVAILABLE" and jev["reason"] is None
    assert jev["denominator_settled_count"] == 51
    assert jev["denominator_net_usdt"] == result["totals"]["net_usdt"] == "-10.2"
    assert jev["by_participation"]["FALLBACK"] == {
        "settled_count": 51,
        "net_usdt": "-10.2",
    }
    assert jev["model_evaluated"] == {"settled_count": 0, "net_usdt": "0"}
    assert jev["fallback_causes"]["TOKEN_BUDGET_PACED"]["settled_count"] == 49
    assert jev["fallback_causes"]["TOKEN_BUDGET_EXHAUSTED"]["settled_count"] == 2
    assert jev["by_source"]["s3"]["settled_count"] == 50
    assert jev["by_source"]["tv_bridge"]["settled_count"] == 1
    assert jev["coverage"]["evidence_row_count"] == 51
    assert jev["coverage"]["unknown_count"] == 0
    assert all(
        jev[key] == result[key]
        for key in (
            "scope_key",
            "account_environment",
            "as_of_ms",
            "window_start_ms",
            "window_end_ms",
        )
    )
    assert all(
        jev[key] is False
        for key in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    )
    assert "REJECTED_UNFILLED_CANDIDATES_OUTSIDE_THIS_DENOMINATOR" in jev["limitations"]
    assert (
        "GROUP_NET_COMPARISON_DOES_NOT_PROVE_MODEL_INCREMENTAL_PROFIT"
        in jev["limitations"]
    )
    params = next(params for sql, params in conn.calls if sql == review._JEV_SQL)
    assert params == (NOW, sorted(raw[0] for raw in rows), *asdict(SCOPE).values(), NOW)
    assert result["content_digest"] == digest(
        {key: value for key, value in result.items() if key != "content_digest"}
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "MODEL_APPROVED"),
        ({"trend": "0.6", "chase": "0.7"}, "MODEL_APPROVED"),
        (
            {"status": "VETO", "reason": "JEV_TREND_UNSUPPORTED", "trend": "0.5"},
            "MODEL_REJECTED",
        ),
        (
            {"status": "VETO", "reason": "JEV_CHASE_RISK_HIGH", "chase": "0.8"},
            "MODEL_REJECTED",
        ),
        (
            {
                "reason": "JEV_GATE_DISABLED",
                "enabled": False,
                "trend": None,
                "chase": None,
            },
            "DISABLED",
        ),
        (
            {"status": "VETO", "reason": "PREEXISTING_REJECTION", "enabled": None},
            "NOT_MODEL_EVALUATED",
        ),
        ({"status": "SKIP", "reason": "JEV_RESULT_INVALID"}, "NOT_MODEL_EVALUATED"),
        (
            {
                "schema": "jev-fixed-entry-check-v1",
                "status": "SKIP",
                "reason": "JEV_REVIEW_UNAVAILABLE",
                "enabled": None,
            },
            "NOT_MODEL_EVALUATED",
        ),
        ({"status": "PENDING", "reason": "JEV_RESULT_PENDING"}, "NOT_MODEL_EVALUATED"),
        ({"present": False}, "UNKNOWN"),
        ({"schema": None}, "UNKNOWN"),
        ({"schema": "future-untrusted-schema"}, "UNKNOWN"),
        ({"schema": "jev-fixed-entry-check-v1"}, "UNKNOWN"),
        ({"status": "APPROVED"}, "UNKNOWN"),
        ({"reason": None}, "UNKNOWN"),
        ({"reason": "JEV_GATE_DISABLED"}, "UNKNOWN"),
        ({"enabled": "true"}, "UNKNOWN"),
        ({"enabled": 1}, "UNKNOWN"),
        ({"trend": None}, "UNKNOWN"),
        ({"trend": "NaN"}, "UNKNOWN"),
        ({"trend": "0_._8"}, "UNKNOWN"),
        ({"trend": "8e-1"}, "UNKNOWN"),
        ({"trend": " 0.8"}, "UNKNOWN"),
        ({"chase": "1.1"}, "UNKNOWN"),
        ({"minimum": "bad-provider-secret"}, "UNKNOWN"),
        ({"trend": "0.5"}, "UNKNOWN"),
        (
            {
                "status": "VETO",
                "reason": "JEV_CHASE_RISK_HIGH",
                "trend": "0.5",
                "chase": "0.8",
            },
            "UNKNOWN",
        ),
        (
            {
                "status": "FALLBACK",
                "reason": "JEV_BASELINE_FALLBACK",
                "unavailable_action": "SKIP",
            },
            "UNKNOWN",
        ),
    ],
)
def test_jev_model_participation_requires_frozen_valid_schema_reason_and_scores(
    changes, expected, labels
):
    result = load(Connection(jev=[jev_row(**changes)]))
    jev = result["jev_participation"]
    assert result["status"] == "AVAILABLE" and result["totals"]["net_usdt"] == "19.8"
    assert jev["by_participation"][expected] == {"settled_count": 1, "net_usdt": "19.8"}
    assert jev["model_evaluated"]["settled_count"] == int(
        expected in {"MODEL_APPROVED", "MODEL_REJECTED"}
    )
    assert jev["status"] == ("UNKNOWN" if expected == "UNKNOWN" else "AVAILABLE")
    assert "bad-provider-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "cause", [None, "MODEL_RESPONSE_INVALID", "SECRET_PROVIDER_PROMPT"]
)
def test_jev_unrecognized_or_disallowed_baseline_cause_is_unknown(cause, labels):
    result = load(
        Connection(
            jev=[
                jev_row(
                    status="FALLBACK",
                    reason="JEV_BASELINE_FALLBACK",
                    fallback_cause=cause,
                )
            ]
        )
    )
    jev = result["jev_participation"]
    assert jev["status"] == "UNKNOWN"
    assert jev["by_participation"]["UNKNOWN"]["settled_count"] == 1
    assert jev["fallback_causes"] == {}
    assert "SECRET_PROVIDER_PROMPT" not in json.dumps(result)


def test_jev_only_verified_settled_episodes_enter_query_and_denominator(labels):
    rows = [row(), row(UUID(int=2)), row(UUID(int=3), maintenance=True)]
    labels[0][rows[1][0]] = label(rows[1][0], state="SETTLEMENT_REVISION_CHANGED")
    conn = Connection(rows, jev=[jev_row()])
    result = load(conn)
    assert result["status"] == "PARTIAL"
    jev = result["jev_participation"]
    assert jev["status"] == "AVAILABLE" and jev["denominator_settled_count"] == 1
    assert jev["denominator_net_usdt"] == result["totals"]["net_usdt"]
    params = next(params for sql, params in conn.calls if sql == review._JEV_SQL)
    assert params[1] == [rows[0][0]]


def test_jev_missing_evidence_is_unknown_and_all_groups_reconcile(labels):
    rows = [row(), row(UUID(int=2))]
    labels[0][rows[1][0]] = label(rows[1][0], net_pnl="-2.1")
    result = load(Connection(rows, jev=[jev_row()]))
    jev = result["jev_participation"]
    assert jev["status"] == "PARTIAL"
    assert jev["by_participation"]["UNKNOWN"] == {
        "settled_count": 1,
        "net_usdt": "-2.1",
    }
    assert (
        sum(group["settled_count"] for group in jev["by_participation"].values()) == 2
    )
    assert sum(
        Decimal(group["net_usdt"]) for group in jev["by_participation"].values()
    ) == Decimal("17.7")
    assert jev["coverage"]["query_complete"] is True


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("42501", "READ_PERMISSION_MISSING"),
        ("57014", "QUERY_TIMEOUT"),
        ("42P01", "READ_SCHEMA_UNAVAILABLE"),
    ],
)
def test_jev_read_failure_preserves_account_facts_and_never_claims_zero_participation(
    state, reason, labels
):
    conn = Connection(error=(review._JEV_SQL, state))
    result = load(conn)
    jev = result["jev_participation"]
    assert result["status"] == "AVAILABLE" and result["totals"]["net_usdt"] == "19.8"
    assert result["coverage"]["full_closed_settlement_population_proven"] is True
    assert jev["status"] == "UNKNOWN" and jev["reason"] == reason
    assert jev["by_participation"]["UNKNOWN"]["settled_count"] == 1
    assert jev["coverage"]["query_complete"] is False
    assert jev["coverage"]["retryable"] is True and conn.rollbacks == 1
    assert "DO_NOT_EXPORT" not in json.dumps(result)


def test_jev_read_uses_remaining_original_account_budget(labels, monkeypatch):
    ticks = iter([0, 0, 0, 0, 20, 20])
    monkeypatch.setattr(review.time, "monotonic", lambda: next(ticks))
    conn = Connection(jev=[jev_row()])
    result = load(conn)
    assert result["totals"]["net_usdt"] == "19.8"
    assert result["jev_participation"]["reason"] == "READ_BUDGET_EXHAUSTED"
    assert all(sql != review._JEV_SQL for sql, _ in conn.calls)
    assert conn.transactions == 3


def cost_plan(**changes):
    return {
        "evidence_present": True,
        "planned_entry_price": "100",
        "initial_stop_price": "95",
        "intent_quantity": "2",
        "sizing_quantity": "2",
        "result_quantity": "2",
        "sizing_reason": "SIZED",
        "result_action": "OPEN",
        "result_side": "BUY",
        "cost_buffer_fraction": "0.002",
        "budgeted_stop_risk_usdt": "11",
        "close_order_count": 1,
        "native_close_count": 0,
        "native_trigger_price": None,
        "native_stop_type_verified": False,
        **changes,
    }


def test_cost_projection_uses_verified_episode_population_and_no_private_fields(labels):
    conn = Connection(
        costs=[(str(UUID(int=1)), cost_plan(prompt="NEVER_EXPORT_SECRET"))]
    )
    result = load(conn)
    costs = result["execution_cost_evidence"]
    assert costs["status"] == "AVAILABLE"
    assert costs["denominator_settled_count"] == costs["plan_comparable_count"] == 1
    assert costs["native_stop_decomposed_count"] == 0
    assert costs["cases"][0]["plan"]["gross_stop_loss_requested_quantity_usdt"] == "10"
    assert costs["cases"][0]["actual"]["exit_fill_vwap"] == "110"
    assert costs["cases"][0]["actual"]["settled_net_usdt"] == "19.8"
    assert (
        costs["cases"][0]["stop_decomposition"]["reason"]
        == "NOT_PROVEN_NATIVE_STOP_EXIT"
    )
    assert "NEVER_EXPORT" not in json.dumps(result)
    assert SCOPE.account_id not in json.dumps(result)
    params = next(params for sql, params in conn.calls if sql == review._COST_SQL)
    assert params == (NOW, NOW, [str(UUID(int=1))], *asdict(SCOPE).values(), NOW)


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("42501", "READ_PERMISSION_MISSING"),
        ("57014", "QUERY_TIMEOUT"),
        ("55P03", "QUERY_LOCK_TIMEOUT"),
        ("42P01", "READ_SCHEMA_UNAVAILABLE"),
    ],
)
def test_cost_query_failure_cannot_discard_settlement_or_jev(labels, state, reason):
    result = load(Connection(jev=[jev_row()], error=(review._COST_SQL, state)))
    assert result["status"] == "AVAILABLE" and result["totals"]["net_usdt"] == "19.8"
    assert result["jev_participation"]["status"] == "AVAILABLE"
    costs = result["execution_cost_evidence"]
    assert costs["status"] == "UNKNOWN" and costs["reason"] == reason
    assert costs["coverage"]["query_complete"] is False
    assert costs["coverage"]["unknown_count"] == 1
    assert costs["coverage"]["retryable"] is True
    assert costs["cases"][0]["actual"]["settled_net_usdt"] == "19.8"
    assert "DO_NOT_EXPORT" not in json.dumps(result)


@pytest.mark.parametrize(
    "records",
    [
        [(str(UUID(int=2)), cost_plan())],
        [(str(UUID(int=1)), [])],
        [(str(UUID(int=1)), cost_plan()), (str(UUID(int=1)), cost_plan())],
    ],
)
def test_cost_foreign_or_duplicate_or_malformed_projection_not_used(labels, records):
    result = load(Connection(costs=records))
    assert result["totals"]["net_usdt"] == "19.8"
    assert result["execution_cost_evidence"]["reason"] == "COST_EVIDENCE_DATA_INVALID"
    assert result["execution_cost_evidence"]["plan_comparable_count"] == 0


def test_cost_projection_uses_original_total_deadline(labels, monkeypatch):
    ticks = iter([0, 0, 0, 0, 0, 20])
    monkeypatch.setattr(review.time, "monotonic", lambda: next(ticks))
    conn = Connection(jev=[jev_row()], costs=[(str(UUID(int=1)), cost_plan())])
    result = load(conn)
    assert result["execution_cost_evidence"]["reason"] == "READ_BUDGET_EXHAUSTED"
    assert result["jev_participation"]["status"] == "AVAILABLE"
    assert result["totals"]["net_usdt"] == "19.8"
    assert all(sql != review._COST_SQL for sql, _ in conn.calls)


def test_cost_query_is_bound_small_exact_and_not_raw_config_export():
    sql = review._COST_SQL
    assert "LIMIT 500" in sql and "i.intent_id=ANY(%s::uuid[])" in sql
    assert "o.episode_id=i.intent_id" in sql and "oe.order_id=o.order_id" in sql
    assert "e.config," not in sql and "SELECT e.snapshot" not in sql
    assert "e.evidence_ref,e.config_digest,e.strategy_version" in sql
    assert "e.signal_id IS NOT DISTINCT FROM i.signal_id" in sql
    assert "e.created_at<=i.created_at" in sql
    assert "oe.created_at<=to_timestamp(%s/1000.0)" in sql
    assert (
        "'planned_entry_price',e.snapshot#>>'{features,context,directional,price}'"
        in sql
    )
    # Quant's signal bar close can differ from its actual sizing reference.
    assert "quant,frame,current,close" not in sql


def test_pg_cost_missing_plan_preserves_account_and_queries_only_scoped_ids(database):
    with database() as conn:
        add_trade(conn, scope=SCOPE)
        add_trade(conn, scope=replace(SCOPE, account_id="foreign-account"))
    result = read_pg(database)
    assert result["status"] == "AVAILABLE" and result["totals"]["settled_count"] == 1
    costs = result["execution_cost_evidence"]
    assert costs["coverage"]["query_complete"] is True
    assert costs["coverage"]["evidence_row_count"] == 1
    assert costs["coverage"]["queried_episode_count"] == 1
    assert costs["cases"][0]["actual"]["settled_net_usdt"] == "19.8"
    assert costs["cases"][0]["reason"] == "FROZEN_PLAN_INVALID"


def test_pg_cost_query_failure_is_savepoint_isolated(database, monkeypatch):
    with database() as conn:
        add_trade(conn, scope=SCOPE)
    monkeypatch.setattr(
        review,
        "_COST_SQL",
        "SELECT * FROM no_execution_cost_table WHERE "
        + " AND ".join(["%s::text IS NOT NULL"] * 8),
    )
    result = read_pg(database)
    assert result["status"] == "AVAILABLE" and result["totals"]["net_usdt"] == "19.8"
    assert result["execution_cost_evidence"]["reason"] == "READ_SCHEMA_UNAVAILABLE"
    assert result["execution_cost_evidence"]["coverage"]["unknown_count"] == 1


@pytest.mark.parametrize(
    ("entry_source", "current_price"),
    [("tv_bridge", "100"), ("quant", "101"), ("quant", None)],
)
@pytest.mark.parametrize(
    ("parent_changes", "known_after_cutoff", "expected"),
    [
        ({}, False, "AVAILABLE"),
        (
            {"triggerPrice": "94"},
            False,
            "NATIVE_STOP_TRIGGER_DIFFERS_FROM_INITIAL_PLAN",
        ),
        ({"actualOrderId": "999"}, False, "NATIVE_STOP_TRIGGER_UNVERIFIED"),
        ({"algoId": 999}, False, "NATIVE_STOP_TRIGGER_UNVERIFIED"),
        ({"orderType": "TAKE_PROFIT_MARKET"}, False, "NATIVE_STOP_TRIGGER_UNVERIFIED"),
        ({}, True, "NATIVE_STOP_TRIGGER_UNVERIFIED"),
    ],
)
def test_pg_cost_native_child_trigger_binding_is_required(
    database,
    monkeypatch,
    parent_changes,
    known_after_cutoff,
    expected,
    entry_source,
    current_price,
):
    original_proof = evidence_helpers.DecisionEvidence

    def proof(strategy, config, snapshot):
        payload = json.loads(snapshot)
        payload["source"] = entry_source
        payload["result"].update(side="BUY", quantity="2")
        payload["features"]["context"] = {
            "directional": {"price": current_price},
            "quant": {"frame": {"current": {"close": "100"}}},
        }
        payload["features"]["evaluation"] = {
            "sizing": {"reason": "SIZED", "quantity": "2", "stop_price": "95"},
            "sizing_explanation": {
                "cost_buffer_fraction": "0.002",
                "budgeted_stop_risk": "11",
            },
        }
        return original_proof(strategy, config, json.dumps(payload))

    monkeypatch.setattr(evidence_helpers, "DecisionEvidence", proof)
    binding = {
        "origin": "BINANCE_ALGO_CHILD",
        "parent_algo_id": 123,
        "parent_client_algo_id": "qa-parent",
        "exchange_order_id": "456",
    }
    closes = []

    class FixtureConnection:
        # Adapt only insertion-time fixture facts; never mutate immutable
        # evidence, intent identities or existing order request evidence.
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=None):
            params = list(params) if params is not None else None
            if "INSERT INTO v2_trade_intents" in sql:
                params[6] = Jsonb({**params[6].obj, "quantity": "2"})
            elif "INSERT INTO v2_orders" in sql and params[2] == "CLOSE":
                closes.append(params[0])
                params[7] = Jsonb(binding)
            elif "INSERT INTO v2_fills" in sql:
                if params[1] in closes:
                    params[3] = "94.8"
                elif entry_source == "quant":
                    params[3] = "101"
            elif "INSERT INTO v2_settlements" in sql:
                params[1] = Jsonb(
                    {
                        **params[1].obj,
                        "net_pnl": "-12.6" if entry_source == "quant" else "-10.6",
                    }
                )
            return self.conn.execute(sql, params)

    with database() as conn:
        add_trade(FixtureConnection(conn), scope=SCOPE)
        parent = {
            "triggerPrice": "95",
            "orderType": "STOP_MARKET",
            "algoId": 123,
            "clientAlgoId": "qa-parent",
            "actualOrderId": "456",
            **parent_changes,
        }
        conn.execute(
            """INSERT INTO v2_order_events(event_id,order_id,version,status,evidence,created_at)
            VALUES (%s,%s,2,'FILLED',%s,to_timestamp(%s/1000.0))""",
            (
                uuid4(),
                closes[0],
                Jsonb({"parent_observation": parent, "native_child_binding": binding}),
                NOW + 1 if known_after_cutoff else START + 10000,
            ),
        )
    result = read_pg(database)
    assert result["status"] == "AVAILABLE"
    assert result["totals"]["net_usdt"] == (
        "-12.6" if entry_source == "quant" else "-10.6"
    )
    costs = result["execution_cost_evidence"]
    if current_price is None:
        assert costs["status"] == "UNKNOWN" and costs["plan_comparable_count"] == 0
        assert costs["cases"][0]["reason"] == "FROZEN_PLAN_INVALID"
        assert costs["cases"][0]["plan"] is None
        return
    assert costs["status"] == "AVAILABLE" and costs["plan_comparable_count"] == 1
    case = costs["cases"][0]
    assert case["plan"]["entry_reference_price"] == current_price
    assert Decimal(case["price_comparison"]["entry_adverse_deviation_usdt"]) == 0
    decomposition = costs["cases"][0]["stop_decomposition"]
    if expected == "AVAILABLE":
        assert costs["native_stop_decomposed_count"] == 1
        assert decomposition["identity_verified"] is True
        expected_loss = Decimal("12" if entry_source == "quant" else "10")
        assert Decimal(decomposition["planned_gross_loss_usdt"]) == expected_loss
        assert Decimal(decomposition["stop_fill_adverse_deviation_usdt"]) == Decimal(
            "0.4"
        )
        assert Decimal(
            decomposition["actual_gross_loss_usdt"]
        ) == expected_loss + Decimal("0.4")
    else:
        assert costs["native_stop_decomposed_count"] == 0
        assert decomposition == {"status": "NOT_COMPARABLE", "reason": expected}


def test_jev_projection_is_small_bound_and_does_not_export_arbitrary_codes(labels):
    result = load(
        Connection(
            jev=[
                jev_row(
                    source="SECRET_ACCOUNT_ID",
                    status="FALLBACK",
                    reason="JEV_BASELINE_FALLBACK",
                    fallback_cause="TOKEN_BUDGET_PACED",
                )
            ]
        )
    )
    jev = result["jev_participation"]
    assert jev["by_source"]["UNKNOWN"]["settled_count"] == 1
    assert jev["fallback_causes"]["TOKEN_BUDGET_PACED"]["settled_count"] == 1
    assert "SECRET_" not in json.dumps(result)
    sql = review._JEV_SQL
    assert "LIMIT 500" in sql and "i.intent_id=ANY(%s::uuid[])" in sql
    assert "e.snapshot," not in sql and "e.config," not in sql
    assert "e.created_at<=to_timestamp(%s/1000.0)" in sql
    assert "i.created_at<=to_timestamp(%s/1000.0)" in sql
    assert "e.evidence_ref,e.config_digest,e.strategy_version" in sql
    assert "e.signal_id IS NOT DISTINCT FROM i.signal_id" in sql


@pytest.mark.parametrize(
    "evidence",
    [
        [jev_row(), jev_row()],
        [jev_row(UUID(int=2))],
        [(str(UUID(int=1)),)],
    ],
)
def test_jev_duplicate_foreign_or_malformed_rows_cannot_inflate_groups(
    evidence, labels
):
    result = load(Connection(jev=evidence))
    assert result["totals"]["settled_count"] == 1
    assert result["jev_participation"]["reason"] == "JEV_ENTRY_EVIDENCE_DATA_INVALID"
    assert (
        result["jev_participation"]["by_participation"]["UNKNOWN"]["settled_count"] == 1
    )


def read_pg(database, **changes):
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        before = conn.execute("SELECT count(*) FROM v2_orders").fetchone()
        result = load(conn, **changes)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == before
        assert conn.execute("SELECT 1").fetchone() == (1,)
        return result


def test_pg_carry_in_without_entry_lookback_and_cross_scope_exclusion(database):
    with database() as conn:
        add_trade(conn, scope=SCOPE, opened=START - 90 * review.DAY_MS)
        add_trade(conn, scope=replace(SCOPE, account_id="foreign-account"))
        add_trade(conn, scope=replace(SCOPE, environment="LIVE"))
    result = read_pg(database)
    assert result["status"] == "AVAILABLE"
    assert result["totals"]["settled_count"] == 1
    assert result["totals"]["net_usdt"] == "19.8"
    assert result["coverage"]["carry_in_settled_count"] == 1


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        (
            {
                "schema": "jev-entry-gate-v1",
                "status": "PASS",
                "reason": "JEV_SECOND_FILTER_PASSED",
                "policy": {
                    "enabled": True,
                    "min_trend_support": "0.6",
                    "max_chase_risk": "0.7",
                },
                "hypotheses": {"trend_supported": "0.8", "chase_risk": "0.2"},
            },
            "MODEL_APPROVED",
        ),
        (
            {
                "schema": "jev-entry-gate-v1",
                "status": "FALLBACK",
                "reason": "JEV_BASELINE_FALLBACK",
                "fallback_cause": "TOKEN_BUDGET_PACED",
                "policy": {"enabled": True, "unavailable_action": "BASELINE"},
            },
            "FALLBACK",
        ),
    ],
)
def test_pg_jev_reads_exact_frozen_entry_projection(
    database, monkeypatch, gate, expected
):
    original = evidence_helpers.add_decision

    def with_gate(*args, **kwargs):
        kwargs["fields"] = {"jev_entry_gate": gate, "prompt": "DO_NOT_EXPORT_PROMPT"}
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence_helpers, "add_decision", with_gate)
    with database() as conn:
        add_trade(conn, scope=SCOPE)
        add_trade(conn, scope=replace(SCOPE, account_id="foreign-account"))
    result = read_pg(database)
    jev = result["jev_participation"]
    assert jev["status"] == "AVAILABLE"
    assert jev["by_participation"][expected] == {"settled_count": 1, "net_usdt": "19.8"}
    assert jev["by_source"]["tv_bridge"]["settled_count"] == 1
    assert jev["coverage"]["queried_episode_count"] == 1
    assert "DO_NOT_EXPORT_PROMPT" not in json.dumps(result)


def test_pg_jev_query_failure_is_savepoint_isolated_from_account_totals(
    database, monkeypatch
):
    with database() as conn:
        add_trade(conn, scope=SCOPE)
    monkeypatch.setattr(
        review,
        "_JEV_SQL",
        "SELECT * FROM no_jev_review_table_exists WHERE "
        + " AND ".join(["%s::text IS NOT NULL"] * 7),
    )
    result = read_pg(database)
    assert result["status"] == "AVAILABLE" and result["totals"]["net_usdt"] == "19.8"
    jev = result["jev_participation"]
    assert jev["status"] == "UNKNOWN" and jev["reason"] == "READ_SCHEMA_UNAVAILABLE"
    assert jev["coverage"]["unknown_count"] == 1


def test_pg_nonterminal_partial_close_and_stale_revision_not_complete_profit(database):
    with database() as conn:
        add_trade(conn, scope=SCOPE, closing="1", settle=False)
        changed = add_trade(conn, scope=SCOPE)
        conn.execute(
            "UPDATE v2_episodes SET accounting_revision=2 WHERE episode_id=%s",
            (changed,),
        )
    result = read_pg(database)
    assert result["status"] == "UNKNOWN" and result["totals"] is None
    assert result["coverage"]["excluded_reasons"] == {
        "OPEN_OR_PARTIAL_EXIT": 1,
        "SETTLEMENT_REVISION_CHANGED": 1,
    }


def insert_extra_close(conn, episode, *, quantity, when, origin=None, terminal=True):
    order = str(uuid4())
    conn.execute(
        """INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,request_key,
        quantity,status,updated_at,request_evidence)
        VALUES (%s,%s,'CLOSE',%s,%s,'1',%s,to_timestamp(%s/1000.0),%s)""",
        (
            order,
            episode,
            order,
            order,
            "FILLED" if terminal else "UNKNOWN",
            when,
            Jsonb({"origin": origin} if origin else {}),
        ),
    )
    if quantity is not None:
        conn.execute(
            """INSERT INTO v2_fills(fill_key,order_id,quantity,price,fee,fee_currency,
            occurred_at_ms,payload,created_at) VALUES (%s,%s,%s,'110','0','USDT',%s,'{}',to_timestamp(%s/1000.0))""",
            (str(uuid4()), order, quantity, when, when),
        )


def test_pg_partial_exit_day_and_last_close_next_day_is_not_same_day_settlement(
    database,
):
    with database() as conn:
        identity = add_trade(conn, scope=SCOPE, closing="1", settle=False)
        insert_extra_close(conn, identity, quantity="1", when=END + 1000)
    result = read_pg(database)
    assert result["status"] == "AVAILABLE"
    assert result["totals"]["settled_count"] == 0
    assert result["coverage"]["excluded_reasons"] == {"NOT_CLOSED_IN_REVIEW_DAY": 1}


def test_pg_later_fill_and_order_state_are_not_backdated_to_prior_cutoff(database):
    with database() as conn:
        identity = add_trade(conn, scope=SCOPE)
        insert_extra_close(conn, identity, quantity="1", when=NOW + 1000)
    result = read_pg(database)
    assert result["totals"] is None
    assert result["coverage"]["excluded_reasons"] == {"LATER_FILL_OR_ORDER_STATE": 1}


@pytest.mark.parametrize(
    "origin", ["APPROVED_TESTNET_MAINTENANCE", "USER_APPROVED_MAINTENANCE"]
)
def test_pg_maintenance_orders_are_excluded_even_without_their_own_fill(
    database, origin
):
    with database() as conn:
        identity = add_trade(conn, scope=SCOPE)
        insert_extra_close(
            conn, identity, quantity=None, when=START + 10000, origin=origin
        )
    result = read_pg(database)
    assert result["status"] == "AVAILABLE"
    assert result["totals"]["settled_count"] == 0
    assert result["coverage"]["excluded_reasons"] == {"MAINTENANCE_EXCLUDED": 1}


def test_pg_current_nonterminal_order_prevents_settlement_reuse(database):
    with database() as conn:
        identity = add_trade(conn, scope=SCOPE)
        insert_extra_close(
            conn, identity, quantity=None, when=START + 10000, terminal=False
        )
    result = read_pg(database)
    assert result["status"] == "UNKNOWN" and result["totals"] is None
    assert result["coverage"]["excluded_reasons"] == {"AWAITING_TERMINAL_ORDERS": 1}


def test_pg_failed_select_is_savepoint_isolated(database, monkeypatch):
    monkeypatch.setattr(
        review,
        "_CANDIDATES_SQL",
        "SELECT * FROM no_account_review_table_exists WHERE "
        + " AND ".join(["%s::text IS NOT NULL"] * 8),
    )
    result = read_pg(database)
    assert result["reason"] == "READ_SCHEMA_UNAVAILABLE"
    assert result["coverage"]["retryable"] is True


def test_pg_readwrite_connection_refused_without_mutation(database):
    with database() as conn:
        result = load(conn)
        assert result["reason"] == "READ_ONLY_REQUIRED"
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


def test_pg_read_committed_readonly_is_not_a_complete_snapshot(database):
    with database() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        result = load(conn)
        assert result["reason"] == "STABLE_SNAPSHOT_REQUIRED"
        assert result["coverage"]["retryable"] is False


def test_pg_close_exactly_at_next_midnight_does_not_enter_prior_day(database):
    with database() as conn:
        identity = add_trade(conn, scope=SCOPE, closing="0", settle=False)
        insert_extra_close(conn, identity, quantity="2", when=END)
    result = read_pg(database)
    assert result["status"] == "AVAILABLE"
    assert result["coverage"]["candidate_count"] == 0
    assert result["totals"]["settled_count"] == 0


def test_pg_last_close_at_start_counts_and_older_entry_does_not_disappear(database):
    with database() as conn:
        identity = add_trade(
            conn, scope=SCOPE, opened=START - 60000, closing="0", settle=False
        )
        insert_extra_close(conn, identity, quantity="2", when=START)
        conn.execute(
            "UPDATE v2_episodes SET status='SETTLED' WHERE episode_id=%s", (identity,)
        )
        conn.execute(
            """INSERT INTO v2_settlements(episode_id,revision,currency,evidence,settled_at)
            VALUES (%s,1,'USDT',%s,to_timestamp(%s/1000.0))""",
            (
                identity,
                Jsonb(
                    {
                        "net_pnl": "19.9",
                        "exchange_flat": True,
                        "orders_terminal": True,
                        "fills_complete": True,
                        "cash_complete": True,
                        "ledger_revision": 1,
                    }
                ),
                START + 1000,
            ),
        )
    result = read_pg(database)
    assert result["status"] == "AVAILABLE"
    assert result["totals"]["settled_count"] == 1
    assert result["totals"]["net_usdt"] == "19.9"
    assert result["trades"][0]["closed_ms"] == START
