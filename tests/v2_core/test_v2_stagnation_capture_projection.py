"""Prospective proof is scoped to one original decision, never backfilled."""

import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from services import v2_stagnation_research_evidence as evidence
from tests.support.v2_admission import database as database_fixture
from tests.v2_core.test_v2_stagnation_research_evidence import SCOPE, Connection, frozen
from tests.v2_core.test_v2_stagnation_shadow import scenario
from v2_core.evidence import canonical, digest
from v2_core.state import BusinessState, StateKey

database = database_fixture


def test_actual_exit_capture_survives_pg_view_and_research_reader(database):
    from test_v2_directional_exit_market import Market, provider
    from test_v2_directional_exit_stage import SCOPE as exit_scope
    from test_v2_directional_exit_stage import setup_exit

    root = Path(__file__).resolve().parents[2]
    with database() as conn:
        for migration in (
            "20261110_stagnation_research_view.sql",
            "20261110_stagnation_prospective_projection.sql",
        ):
            conn.execute((root / "db/migrations" / migration).read_text())
    # The lifecycle fixture opens at virtual millisecond 3. Keep its elapsed
    # holding clock bounded rather than mixing that epoch with wall time.
    now = 72 * 3_600_000 + 123_000
    _, result, venue, service = setup_exit(database, allow_writes=True, now=now)
    service.observe = provider(Market(server=now), clock=now)
    episode = result["intent_id"]
    answer = service.run_once()
    assert answer["exits"][episode]["status"] == "DISPATCHED", answer
    # The reader accepts a caller-supplied closed-trade context. This fixture
    # tests the projection, not a completed exchange settlement or its PnL.
    as_of = time.time_ns() // 1_000_000 + 1000
    trade = {
        "id": episode,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "opened_ms": 3,
        "closed_ms": now + 1000,
        "exit_reason": "LOW_YIELD_STAGNATION",
    }
    with database() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        loaded = evidence.load_frozen_exit(conn, exit_scope, trade, as_of_ms=as_of)
    assert loaded["status"] == "AVAILABLE", loaded
    assert loaded["prospective_capture"]["digest_verified"]
    shadow = evidence.compare_frozen_exit(loaded)["comparison"]["reports"][0]["report"]
    assert shadow["baseline_reproduced_count"] == 1
    assert shadow["status"] != "UNAVAILABLE", shadow
    assert shadow["execution_authorized"] is False
    assert len(venue.order_writes) == 1


def capture_fixture():
    trade, legacy = frozen()
    raw = scenario()["snapshots"][0]
    capture = {
        "version": "prospective-exit-research-snapshot-v1",
        "execution_authorized": False,
        "promotion_authorized": False,
        "binding": {
            "episode": trade["id"],
            "symbol": trade["symbol"],
            "environment": SCOPE.environment,
            "account_scope_digest": digest(canonical(asdict(SCOPE))),
            "opened_at_ms": trade["opened_ms"],
            "at_ms": raw["at_ms"],
        },
        "status": "AVAILABLE",
        "components": {
            name: {"status": "AVAILABLE", "reason": None}
            for name in (
                "observation",
                "hourly_history",
                "native_terms",
                "native_quantity",
            )
        },
        "snapshot": raw,
    }
    return trade, [*deepcopy(legacy), capture, digest(canonical(capture))]


def load(trade, saved):
    return evidence.load_frozen_exit(
        Connection(saved), SCOPE, trade, as_of_ms=trade["closed_ms"]
    )


def test_valid_prospective_capture_preserves_receipt_and_original_baseline():
    trade, saved = capture_fixture()
    result = load(trade, saved)
    assert result["status"] == "AVAILABLE"
    assert result["prospective_capture"]["digest_verified"]
    assert result["payload"]["snapshots"][0] == saved[9]["snapshot"]
    compared = evidence.compare_frozen_exit(result)
    shadow = compared["comparison"]["reports"][0]["report"]
    assert shadow["baseline_reproduced_count"] == 1
    assert shadow["execution_authorized"] is False
    assert "qa-stagnation" not in str(compared)
    assert trade["id"] not in str(compared)


@pytest.mark.parametrize(
    "defect",
    [
        "digest",
        "account",
        "episode",
        "time",
        "facts",
        "policy",
        "observation",
        "future",
        "extra",
    ],
)
def test_capture_cannot_replace_original_context_even_with_new_digest(defect):
    trade, saved = capture_fixture()
    capture = saved[9]
    raw = capture["snapshot"]
    if defect == "digest":
        saved[10] = "0" * 64
    elif defect == "account":
        capture["binding"]["account_scope_digest"] = "0" * 64
    elif defect == "episode":
        capture["binding"]["episode"] = "another-episode"
    elif defect == "time":
        raw["at_ms"] += 1
    elif defect == "facts":
        raw["facts"]["mark_price"] = "999"
    elif defect == "policy":
        raw["baseline_policy"] = {}
    elif defect == "observation":
        raw["observation"]["observed_at_ms"] -= 1000
    elif defect == "future":
        raw["observation"]["available_at_ms"] = raw["at_ms"] + 1
    else:
        raw["guessed_quantity"] = "1"
    if defect != "digest":
        saved[10] = digest(canonical(capture))
    result = load(trade, saved)
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "FROZEN_EXIT_EVIDENCE_INVALID"


def test_partial_capture_keeps_received_market_evidence_without_quantity_guess():
    trade, saved = capture_fixture()
    capture = saved[9]
    capture["status"] = "PARTIAL"
    capture["snapshot"]["native_protection"] = None
    capture["components"]["native_quantity"]["status"] = "UNKNOWN"
    saved[10] = digest(canonical(capture))
    result = load(trade, saved)
    assert result["status"] == "AVAILABLE"
    assert (
        result["prospective_capture"]["component_statuses"]["native_quantity"]
        == "UNKNOWN"
    )
    compared = evidence.compare_frozen_exit(result)
    shadow = compared["comparison"]["reports"][0]["report"]
    assert shadow["baseline_reproduced_count"] == 1
    assert shadow["continuation_eligibility"] == "UNKNOWN"
    assert shadow["reason"] == "FROZEN_NATIVE_PROTECTION_MISSING"


def test_projection_legacy_row_does_not_gain_current_receipt():
    trade, saved = frozen()
    result = load(trade, [*saved, None, None])
    assert result["status"] == "AVAILABLE"
    assert result["prospective_capture"] is None
    assert result["payload"]["snapshots"][0]["observation"]["available_at_ms"] is None


def test_optional_capture_failure_keeps_independently_valid_parent_baseline():
    trade, saved = capture_fixture()
    saved[9] = {
        "version": "prospective-exit-research-snapshot-v1",
        "status": "UNKNOWN",
        "reason": "RESEARCH_CAPTURE_UNAVAILABLE",
        "snapshot": None,
        "binding": None,
        "execution_authorized": False,
        "promotion_authorized": False,
    }
    saved[10] = digest(canonical(saved[9]))
    result = load(trade, saved)
    assert result["status"] == "AVAILABLE"
    assert result["prospective_capture"]["reason"] == "CAPTURE_BASELINE_UNAVAILABLE"
    assert result["payload"]["snapshots"][0]["observation"]["available_at_ms"] is None
    shadow = evidence.compare_frozen_exit(result)["comparison"]["reports"][0]["report"]
    assert shadow["baseline_reproduced_count"] == 1
    assert shadow["continuation_eligibility"] == "UNKNOWN"


def test_pg_projection_appends_capture_without_changing_original_evidence(database):
    root = Path(__file__).resolve().parents[2]
    with database() as conn:
        for migration in (
            "20261110_stagnation_research_view.sql",
            "20261110_stagnation_prospective_projection.sql",
        ):
            conn.execute((root / "db/migrations" / migration).read_text())
    trade, saved = capture_fixture()
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
    payload.update(research_snapshot=saved[9], research_snapshot_digest=saved[10])
    key = StateKey(**asdict(SCOPE), namespace="directional-exit-v1", key=trade["id"])
    BusinessState(database).change(
        key,
        expected_version=0,
        request_key="qa-prospective-capture",
        payload=payload,
        reason="QA",
    )
    with database() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        result = evidence.load_frozen_exit(
            conn, SCOPE, trade, as_of_ms=time.time_ns() // 1000000 + 1000
        )
        assert result["status"] == "AVAILABLE"
        assert result["prospective_capture"]["digest_verified"]
        assert result["payload"]["snapshots"][0]["facts"] == payload["facts"]
        assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
