"""Read-only research composition against disposable QA PG, never trading DB."""

import time

from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import baseline, service
from test_v2_intent_admission import database as database_fixture

from services.v2_profitability_audit import AuditPolicy, load_audit_inputs

database = database_fixture
closed = closed_fixture


def test_full_read_snapshot_preserves_orders_and_ledger(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    assert service(database, income).run_once()["status"] == "CLEAR"
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        counts = "SELECT (SELECT count(*) FROM v2_orders),(SELECT count(*) FROM v2_state_history),(SELECT count(*) FROM v2_fills)"
        before = conn.execute(counts).fetchone()
        now = time.time_ns() // 1000000
        report = load_audit_inputs(
            conn,
            SCOPE,
            now_ms=now,
            holdout_start_ms=now - 3600000,
            policy=AuditPolicy(max_trades=1, trend_cases=1),
        )
        assert conn.execute(counts).fetchone() == before
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
    assert report["paths"]["selected_count"] == 1
    assert report["paths"]["cases"][0]["trade"]["settlement_state"] == "SETTLED"
    assert report["cohorts"]["execution_authorized"] is False
    # The legacy fixture uses exchange fills at epoch 3/10 ms but inserts its
    # intent at today's DB time. It is settled accounting, not a causally valid
    # train/holdout label; the new review must explicitly exclude that mismatch.
    assert report["cohorts"]["periods"]["HOLDOUT"]["settled"] == 0
    assert report["cohorts"]["excluded"] == {"INVALID_LABEL_TIMING": 1}
