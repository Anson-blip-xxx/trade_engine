import copy
import json
import socket
from types import SimpleNamespace

import pytest

from services import v2_review_progress as review
from v2_core.account_risk import AccountScope

SCOPE = AccountScope("BINANCE", "qa-only-account", "SANDBOX", "FUTURES")
KEY = "a" * 32


def row(identity="one", *, delta="-2", changed=True, **updates):
    base = {
        "status": "CLOSED",
        "net_realized": "1",
        "paired_net_delta": "0",
        "path_digest": "a" * 32,
    }
    alternative = {
        "status": "CLOSED",
        "net_realized": str(review.Decimal(1) + review.Decimal(delta)),
        "paired_net_delta": delta,
        "path_digest": ("b" if changed else "a") * 32,
    }
    result = {
        "intent_id": identity,
        "study_id": "qa-study",
        "scope_key": KEY,
        "environment": "SANDBOX",
        "producer": "s6",
        "source": "s3",
        "strategy_version": "qa-strategy-v1",
        "config_digest": "c" * 64,
        "symbol": "BTCUSDT",
        "cohort": "POST_REGISTRATION_REPLAY",
        "research_version": "qa-research-v1",
        "study_digest": "d" * 64,
        "policy_digest": "e" * 64,
        "exit_policy_digest": "f" * 64,
        "parameters_digest": "1" * 64,
        "cost_digest": "2" * 64,
        "created_ms": 100,
        "maintenance": False,
        "status": "DONE",
        "research_trigger_count": 1,
        "actual_holding_trigger_count": 0,
        "reason": "ANALYZED",
        "variants": {
            "baseline": base,
            "partial": alternative,
            "tighten": copy.deepcopy(base),
        },
    }
    result.update(updates)
    return result


def report(rows, **updates):
    kwargs = {"scope": SCOPE, "scope_key": KEY, "start_ms": 1, "as_of_ms": 1000}
    kwargs.update(updates)
    return review.build_progress(rows, **kwargs)


def variant(result, name="partial"):
    return next(group for group in result["groups"] if group["variant"] == name)


def test_negative_actual_pairs_do_not_promote_and_noop_is_not_effective():
    result = report([row(), row("two", delta="0", changed=False)])
    partial = variant(result)
    assert partial["paired_samples"] == 2
    assert partial["effective_pairs"] == partial["no_op_pairs"] == 1
    assert partial["paired_net_delta_quote_units"] == "-2"
    assert partial["quote_currency"] == "UNKNOWN"
    assert partial["conclusion"] == "DO_NOT_PROMOTE_NEGATIVE"
    assert variant(result, "tighten")["effective_pairs"] == 0
    assert (
        not result["automatic_parameter_updates"] and not result["promotion_authorized"]
    )
    assert not result["execution_authorized"]
    encoded = json.dumps(result)
    assert (
        SCOPE.account_id not in encoded
        and '"intent_id"' not in encoded
        and KEY not in encoded
    )


def test_small_positive_not_improvement_proof():
    result = variant(report([row(delta="1")]))
    assert result["conclusion"] == "INSUFFICIENT_ACTIONABLE_EVIDENCE"
    assert result["positive_pairs"] == 1


def test_positive_with_reporting_floor_still_only_observe():
    result = variant(report([row(delta="1")], minimum_effective_pairs=1))
    assert result["conclusion"] == "OBSERVE_ONLY"
    assert "independent future" in result["next_action"]


def test_changed_path_zero_is_not_noop():
    result = variant(report([row(delta="0")]))
    assert result["effective_pairs"] == result["zero_delta_pairs"] == 1
    assert result["no_op_pairs"] == 0


@pytest.mark.parametrize(
    "field",
    [
        "study_id",
        "cohort",
        "producer",
        "source",
        "strategy_version",
        "config_digest",
        "research_version",
        "study_digest",
        "policy_digest",
        "exit_policy_digest",
        "parameters_digest",
        "cost_digest",
        "symbol",
    ],
)
def test_incompatible_definitions_never_pool(field):
    other = "3" * 64 if field.endswith("digest") else "other"
    if field == "symbol":
        other = "ETHUSDC"
    result = report([row(), row("two", delta="4", **{field: other})])
    assert len(result["groups"]) == 4
    assert sorted(
        group["paired_net_delta_quote_units"]
        for group in result["groups"]
        if group["variant"] == "partial"
    ) == ["-2", "4"]
    assert all(group["quote_currency"] == "UNKNOWN" for group in result["groups"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("net_realized", None),
        ("net_realized", "NaN"),
        ("paired_net_delta", None),
        ("path_digest", None),
        ("status", "OPEN"),
    ],
)
def test_unknown_or_invalid_pair_not_zero(field, value):
    sample = row()
    sample["variants"]["partial"][field] = value
    result = variant(report([sample]))
    assert (
        result["paired_samples"] == 0 and result["paired_net_delta_quote_units"] is None
    )
    assert sum(result["coverage_gaps"].values()) == 1
    assert result["conclusion"] == "INSUFFICIENT_ACTIONABLE_EVIDENCE"


def test_inconsistent_delta_and_false_noop_rejected():
    wrong = row()
    wrong["variants"]["partial"]["paired_net_delta"] = "99"
    assert variant(report([wrong]))["coverage_gaps"] == {"PAIR_DELTA_INCONSISTENT": 1}
    assert variant(report([row(changed=False)]))["coverage_gaps"] == {
        "IDENTICAL_PATH_DIFFERENT_NET": 1
    }


def test_pending_horizon_is_expected_and_uncovered_is_explicit():
    pending = row(
        "pending", status="PENDING", reason="ACTUAL_HORIZON_NOT_CLOSED", variants={}
    )
    uncovered = row(
        "uncovered", status="UNCOVERED", reason="ARCHIVE_MISSING", variants={}
    )
    result = report([row(delta="1"), pending, uncovered], minimum_effective_pairs=1)
    group = variant(result)
    assert result["waiting_horizon"] == group["waiting_horizon"] == 1
    assert group["pending_other"] == 0
    assert group["job_counts"] == {"DONE": 1, "PENDING": 1, "UNCOVERED": 1}
    assert group["paired_samples"] == 1
    assert group["conclusion"] == "INSUFFICIENT_ACTIONABLE_EVIDENCE"


def test_trigger_denominators_unknown_and_mismatch():
    missing = row(
        "missing", research_trigger_count=None, actual_holding_trigger_count=None
    )
    group = variant(report([row(), missing]))
    assert group["trigger_coverage"]["research_trigger_count"] == {
        "known_samples": 1,
        "unknown_samples": 1,
        "triggered_samples": 1,
    }
    assert (
        group["trigger_coverage"]["actual_holding_trigger_count"]["triggered_samples"]
        == 0
    )
    assert group["trigger_mismatch"]["both_known_samples"] == 1
    assert (
        group["trigger_mismatch"][
            "research_trigger_without_actual_holding_trigger_samples"
        ]
        == 1
    )


def test_missing_cost_definition_never_validates_pairs():
    group = variant(report([row(cost_digest=None)]))
    assert group["coverage_gaps"] == {"RESEARCH_DEFINITION_UNKNOWN": 1}
    assert group["paired_net_delta_quote_units"] is None


def test_truncation_blocks_any_progress_claim():
    assert (
        variant(report([row(delta="1")], truncated=True, minimum_effective_pairs=1))[
            "conclusion"
        ]
        == "INSUFFICIENT_ACTIONABLE_EVIDENCE"
    )


def test_scope_environment_and_identity_safety():
    for value in (row(scope_key="b" * 32), row(environment="LIVE")):
        with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
            report([value])
    with pytest.raises(ValueError, match="SANDBOX"):
        report([], scope=AccountScope("BINANCE", "qa", "LIVE", "FUTURES"))
    assert report([row(maintenance=True)])["groups"] == []
    assert report([row(created_ms=1001)])["groups"] == []
    with pytest.raises(ValueError, match="SYMBOL_INVALID"):
        report([row(symbol="private-value-not-a-symbol")])


@pytest.mark.parametrize("field,value", [("exchange", "OTHER"), ("product", "SPOT")])
def test_scope_revalidated_even_for_forged_dataclass(field, value):
    scope = copy.deepcopy(SCOPE)
    object.__setattr__(scope, field, value)
    with pytest.raises(ValueError, match="SANDBOX_SCOPE_REQUIRED"):
        report([], scope=scope)


def test_deterministic_groups_duplicate_idempotency_and_conflict():
    rows = [row(), row("two", strategy_version="other", delta="4")]
    assert report(rows) == report(list(reversed(rows)))
    assert report([row(), row()])["excluded_records"] == {"duplicate_identical": 1}
    with pytest.raises(ValueError, match="DUPLICATE_CONFLICT"):
        report([row(), row(delta="4")])


class Connection:
    def __init__(self, *, readonly="on", isolation="repeatable read", rows=()):
        self.readonly, self.rows, self.calls = readonly, rows, []
        self.isolation = isolation

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql == "SHOW transaction_read_only":
            return SimpleNamespace(fetchone=lambda: (self.readonly,))
        if sql == "SHOW transaction_isolation":
            return SimpleNamespace(fetchone=lambda: (self.isolation,))
        if "transaction_timestamp()" in sql:
            return SimpleNamespace(fetchone=lambda: (1000, KEY))
        return SimpleNamespace(fetchall=lambda: self.rows)


def test_query_is_scoped_bounded_and_readonly_without_financial_output():
    conn = Connection()
    result = review.load_progress(conn, SCOPE, days=1, limit=5)
    assert result["groups"] == []
    sql, params = conn.calls[-1]
    assert "i.scope_key=%s" in sql and "i.environment='SANDBOX'" in sql
    assert "NOT i.maintenance" in sql and "LIMIT %s" in sql and params[-1] == 6
    assert "trade_v2_research.actual_exit_intents" in sql
    assert "'fills',execution_path.fills" in sql
    assert "CASE WHEN execution_path.valid" in sql
    assert all(statement.startswith(("SELECT", "SHOW")) for statement, _ in conn.calls)
    assert conn.calls[2][1] == tuple(review.asdict(SCOPE).values())
    assert SCOPE.account_id not in json.dumps(result)
    with pytest.raises(ValueError, match="READ_ONLY"):
        review.load_progress(Connection(readonly="off"), SCOPE)
    with pytest.raises(ValueError, match="STABLE_SNAPSHOT"):
        review.load_progress(Connection(isolation="read committed"), SCOPE)
    assert (
        review.load_progress(Connection(isolation="serializable"), SCOPE)["groups"]
        == []
    )


def test_execution_fingerprint_sql_excludes_labels_and_requires_every_fill_field():
    sql = review._EXECUTION_PATH_SQL
    assert "'reason'" not in sql and "'gross_pnl'" not in sql
    assert "WITH ORDINALITY" in sql and "ORDER BY f.ordinality" in sql
    assert "count(*)>0" in sql and ") IS TRUE),false)" in sql
    for field in ("bar_at_ms", "quantity", "price", "fee"):
        assert f"jsonb_typeof(f.value->'{field}')" in sql
    for field in ("remaining_quantity", "closed_bar_at_ms"):
        assert f"jsonb_typeof(v.value->'{field}')" in sql


def test_adapter_projection_definition_fingerprints_and_pending():
    policy = {"fee_rate": "0.0005", "slippage_fraction": "0.0005"}
    sample = row()
    values = (
        "qa-study",
        "one",
        KEY,
        "SANDBOX",
        "s6",
        "s3",
        "qa-v1",
        "c" * 64,
        "BTCUSDT",
        100,
        False,
        "DONE",
        {"start_ms": 50, "version": "research-v1"},
        "POST_REGISTRATION_REPLAY",
        "research-v1",
        policy,
        {"min_profit_r": "0.5"},
        {"period": 14},
        "ANALYZED",
        1,
        0,
        sample["variants"],
    )
    group = variant(review.load_progress(Connection(rows=[values]), SCOPE))
    assert group["paired_net_delta_quote_units"] == "-2"
    assert group["definition"]["cost_digest"] == review._digest(policy)
    pending = list(values)
    pending[1], pending[11], pending[13:18], pending[18], pending[19:] = (
        "two",
        "PENDING",
        [None] * 5,
        "ACTUAL_HORIZON_NOT_CLOSED",
        [None] * 3,
    )
    result = review.load_progress(Connection(rows=[tuple(pending)]), SCOPE)
    assert result["waiting_horizon"] == 1
    assert variant(result)["definition"]["cohort"] == "POST_REGISTRATION_REPLAY"
    assert variant(result)["paired_net_delta_quote_units"] is None


def test_cli_redacts_database_failures(monkeypatch, capsys):
    import psycopg

    monkeypatch.setenv("V2_REVIEW_READ_DSN", "never-print-secret")

    def fail(*args, **kwargs):
        raise RuntimeError("never-print-secret qa-only-account")

    monkeypatch.setattr(psycopg, "connect", fail)
    assert review.main(["--account-id", "qa-only-account"]) == 1
    output = capsys.readouterr().out
    assert "REVIEW_PROGRESS_UNAVAILABLE" in output
    assert "secret" not in output and "qa-only-account" not in output


def test_external_tcp_disabled_by_suite_boundary():
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(RuntimeError, match="external network disabled"),
    ):
        sock.connect(("127.0.0.1", 1))
