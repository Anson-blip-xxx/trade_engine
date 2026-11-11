"""Historical source gaps cannot create trend continuation authority."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from services import v2_stagnation_research_evidence as evidence
from services.v2_trend_campaign_research import CampaignPolicy, evaluate
from tests.support.v2_admission import database as database_fixture
from tests.v2_core.test_v2_stagnation_shadow import scenario
from tests.v2_core.test_v2_trend_campaign_research import Archive, case
from v2_core.account_risk import AccountScope
from v2_core.state import BusinessState, StateKey

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-stagnation", "SANDBOX", "FUTURES")


def frozen():
    raw = scenario()
    row = raw["snapshots"][0]
    trade = {
        "id": str(uuid4()),
        "symbol": "BTCUSDT",
        "side": "BUY",
        "opened_ms": raw["opened_at_ms"],
        "closed_ms": row["at_ms"] + 1000,
        "exit_reason": "LOW_YIELD_STAGNATION",
    }
    observed = {
        **row["observation"],
        "symbol": "BTCUSDT",
        "source": "binance-public-exit-v2",
    }
    observed.pop("available_at_ms")
    stored = (
        asdict(SCOPE),
        trade["id"],
        row["facts"],
        row["baseline"],
        row["baseline_policy"],
        observed,
        None,
        None,
        row["at_ms"] + 1,
    )
    return trade, stored


class Connection:
    def __init__(self, saved=None, *, error=False):
        self.saved, self.error, self.calls, self.rollback = saved, error, [], False

    @contextmanager
    def transaction(self):
        try:
            yield
        except Exception:
            self.rollback = True
            raise

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if self.error:
            raise RuntimeError("SECRET_DO_NOT_EXPORT")
        return self

    def fetchone(self):
        return self.saved


def test_frozen_baseline_can_match_without_manufacturing_receipt_or_protection():
    trade, stored = frozen()
    conn = Connection(stored)
    report = evidence.load_frozen_exit(conn, SCOPE, trade, as_of_ms=trade["closed_ms"])
    compared = evidence.compare_frozen_exit(report)
    shadow = compared["comparison"]["reports"][0]["report"]
    assert shadow["baseline_reproduced_count"] == 1
    assert shadow["continuation_eligibility"] == "UNKNOWN"
    assert shadow["execution_authorized"] is False
    assert shadow["reason"] == "OBSERVATION_AVAILABILITY_NOT_PROVEN"
    assert "payload" not in compared
    assert "qa-stagnation" not in str(compared)
    assert len(conn.calls) == 1


@pytest.mark.parametrize("change", ["scope", "side", "symbol", "future", "opened"])
def test_scope_and_decision_time_must_match(change):
    trade, stored = frozen()
    saved = list(deepcopy(stored))
    if change == "scope":
        saved[0]["account_id"] = "another"
    if change == "side":
        saved[2]["side"] = "SELL"
    if change == "symbol":
        saved[5]["symbol"] = "ETHUSDT"
    if change == "future":
        saved[8] = trade["closed_ms"] + 1
    if change == "opened":
        saved[2]["held_ms"] = 10**12
    assert (
        evidence.load_frozen_exit(
            Connection(saved), SCOPE, trade, as_of_ms=trade["closed_ms"]
        )["status"]
        == "UNKNOWN"
    )


def test_query_failure_is_scoped_and_does_not_export_exception():
    trade, _ = frozen()
    conn = Connection(error=True)
    result = evidence.load_frozen_exit(conn, SCOPE, trade, as_of_ms=trade["closed_ms"])
    assert result["reason"] == "FROZEN_EXIT_QUERY_UNAVAILABLE"
    assert conn.rollback
    assert "SECRET" not in str(result)
    conn = Connection()
    trade["exit_reason"] = "PEAK_DRAWDOWN"
    assert (
        evidence.load_frozen_exit(conn, SCOPE, trade, as_of_ms=trade["closed_ms"])[
            "status"
        ]
        == "NOT_APPLICABLE"
    )
    assert not conn.calls


def test_changed_frozen_inputs_cannot_be_replayed_as_original():
    trade, stored = frozen()
    result = evidence.load_frozen_exit(
        Connection(stored), SCOPE, trade, as_of_ms=trade["closed_ms"]
    )
    result["payload"]["snapshots"][0]["facts"]["mark_price"] = "101"
    assert (
        evidence.compare_frozen_exit(result)["reason"]
        == "STAGNATION_COMPARISON_UNAVAILABLE"
    )


def test_existing_campaign_preserves_actual_baseline_when_stagnation_arm_unavailable():
    candidate = case()
    candidate["stagnation_evidence"] = {
        "status": "UNKNOWN",
        "reason": "FROZEN_EXIT_NOT_RETAINED",
        "execution_authorized": False,
    }
    output = evaluate(
        candidate, SCOPE, Archive().read, now_ms=107 * 3600000, policy=CampaignPolicy()
    )
    assert output["status"] == "ANALYZED"
    assert output["actual_baseline"]["net_pnl_usdt"] == "22"
    assert output["stagnation_exit_shadow"]["reason"] == "FROZEN_EXIT_NOT_RETAINED"


def test_pg_frozen_query_uses_scoped_immutable_history_inside_readonly(database):
    import time

    trade, saved = frozen()
    with database() as conn:
        conn.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20261110_stagnation_research_view.sql"
            ).read_text()
        )
    key = StateKey(**asdict(SCOPE), namespace="directional-exit-v1", key=trade["id"])
    payload = dict(
        zip(
            (
                "account_scope",
                "episode",
                "facts",
                "decision",
                "policy",
                "observation",
                "native_protection",
                "hourly_history",
            ),
            saved[:8],
            strict=True,
        )
    )
    BusinessState(database).change(
        key,
        expected_version=0,
        request_key="qa-frozen-exit",
        payload=payload,
        reason="QA",
    )
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        result = evidence.load_frozen_exit(
            conn, SCOPE, trade, as_of_ms=time.time_ns() // 1000000 + 1000
        )
        assert result["status"] == "AVAILABLE"
        assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)


def test_pg_projection_does_not_expose_other_namespaces_or_live(database):
    with database() as conn:
        conn.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20261110_stagnation_research_view.sql"
            ).read_text()
        )
    for environment, namespace in (
        ("SANDBOX", "account-coverage-v1"),
        ("LIVE", "directional-exit-v1"),
    ):
        scope = {**asdict(SCOPE), "environment": environment}
        key = StateKey(**scope, namespace=namespace, key="qa-other-state")
        BusinessState(database).change(
            key,
            expected_version=0,
            request_key="qa-other",
            payload={"secret_like": "not-exposed"},
            reason="QA",
        )
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_stagnation_frozen_exit_evidence"
        ).fetchone() == (0,)
