"""Real isolated PG contracts, never run against the configured service DB."""

import time
from dataclasses import replace

import pytest
from test_v2_directional_cash import closed as closed_fixture
from test_v2_directional_replay import SCOPE
from test_v2_directional_settlement import baseline, service
from test_v2_intent_admission import database as database_fixture

from services.v2_trade_path_review import load_path_inputs

database = database_fixture
closed = closed_fixture


def test_scoped_readonly_settled_fill_snapshot(database, closed):
    runtime, episode, income = closed
    baseline(database, runtime, episode)
    assert service(database, income).run_once()["status"] == "CLEAR"
    with database() as c:
        c.execute("SET TRANSACTION READ ONLY")
        counts = "SELECT (SELECT count(*) FROM v2_orders),(SELECT count(*) FROM v2_state_history)"
        before = c.execute(counts).fetchone()
        now = time.time_ns() // 1000000
        snapshot = load_path_inputs(c, SCOPE, now_ms=now)
        other = load_path_inputs(c, replace(SCOPE, account_id="other"), now_ms=now)
        assert c.execute(counts).fetchone() == before
    assert snapshot["selected_count"] == 1
    assert snapshot["cases"][0]["trade"]["id"] == episode
    assert {f["leg"] for f in snapshot["cases"][0]["fills"]} == {"OPEN", "CLOSE"}
    assert other["cases"] == []


def test_unsettled_not_selected(database, closed):
    with database() as c:
        c.execute("SET TRANSACTION READ ONLY")
        assert (
            load_path_inputs(c, SCOPE, now_ms=time.time_ns() // 1000000)["cases"] == []
        )


def test_write_transaction_rejected(database):
    with database() as c, pytest.raises(ValueError, match="READ_ONLY"):
        load_path_inputs(c, SCOPE, now_ms=100000)
