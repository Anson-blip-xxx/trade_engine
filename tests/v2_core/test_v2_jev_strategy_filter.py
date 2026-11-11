"""Model filtering cannot turn a risk rejection or a retry into another order."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_v2_directional_admission import formal
from test_v2_directional_replay import scenario

from tests.support.v2_admission import database as database_fixture
from v2_core.jev_entry_gate import GateAssessment
from v2_core.strategy import StrategyDecision

database = database_fixture


def install(worker, fn):
    worker.entry_filter = SimpleNamespace(assess=fn)


def assessment(status, original):
    return GateAssessment(
        status,
        "QA_JEV_" + status,
        original
        if status == "PASS"
        else None
        if status == "PENDING"
        else StrategyDecision("IGNORED", "QA_JEV_" + status),
        json.dumps(
            {
                "status": status,
                "execution_authorized": False,
                "valid_until_ms": 5 if status == "PASS" else None,
            }
        ),
    )


def test_pending_has_no_decision_intent_receipt_or_order(database):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    install(worker, lambda original, **_: assessment("PENDING", original))
    for _ in range(2):
        assert worker.consume(signal, context=context)["status"] == "DEFERRED"
    with database() as c:
        for table in (
            "v2_strategy_decisions",
            "v2_trade_intents",
            "v2_signal_receipts",
            "v2_orders",
        ):
            assert c.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    install(worker, lambda original, **_: assessment("PASS", original))
    assert worker.consume(signal, context=context)["status"] == "PREPARED"
    saved = worker.decision(signal)
    assert saved["quantity"] == "1.250000000000000000"
    assert (
        saved["snapshot"]["features"]["evaluation"]["jev_entry_gate"]["status"]
        == "PASS"
    )
    install(
        worker,
        lambda *_args, **_kwargs: pytest.fail("committed entry must not re-ask JEV"),
    )
    worker.consume(signal, context={})
    assert worker.decision(signal) == saved
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["SKIP", "VETO"])
def test_terminal_opinion_only_skips_one_signal(database, status):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    install(worker, lambda original, **_: assessment(status, original))
    result = worker.consume(signal, context=context)
    assert (result["status"], result["reason"]) == ("IGNORED", "QA_JEV_" + status)
    assert worker.decision(signal)["action"] == "IGNORED"
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_risk_rejection_does_not_ask_model(database):
    _, runtime, signal, context = scenario(database)
    context["directional"]["sizing"]["available_margin"] = "0"
    worker = formal(runtime, "S6")
    install(worker, lambda *_args, **_kwargs: pytest.fail("risk already rejected"))
    assert worker.consume(signal, context=context)["status"] == "IGNORED"


def test_quarantine_rejection_finishes_without_market_model_or_order(database):
    from test_v2_directional_replay import SCOPE

    from services.v2_opening_quarantine import OpeningQuarantineContext, quarantine_key
    from v2_core.state import BusinessState

    _, runtime, signal, _ = scenario(database)
    worker = formal(runtime, "S6")
    store = BusinessState(database)
    store.change(
        quarantine_key(SCOPE, "BTCUSDT"),
        expected_version=0,
        request_key="qa-quarantine",
        payload={"halted": True},
        reason="QA",
    )
    with database() as c:
        snapshot = c.execute(
            "SELECT snapshot FROM v2_inbound_signals WHERE signal_id=%s", (signal,)
        ).fetchone()[0]
    provider = OpeningQuarantineContext(
        database,
        lambda *_: pytest.fail("quarantine must not fetch market"),
        scope=SCOPE,
    )
    install(
        worker, lambda *_args, **_kwargs: pytest.fail("quarantine must not ask JEV")
    )
    result = worker.consume(signal, context=provider(snapshot))
    assert (result["status"], result["reason"]) == (
        "IGNORED",
        "SYMBOL_OPENING_QUARANTINED",
    )
    with database() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM v2_signal_receipts WHERE outcome='IGNORED'"
            ).fetchone()[0]
            == 1
        )
        assert c.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


@pytest.mark.parametrize(
    "broken", ["exception", "mutated_quantity", "veto_open", "pending_open", "invalid"]
)
def test_bad_filter_cannot_authorize_or_block_account(database, broken):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")

    def check(original, **_):
        if broken == "exception":
            raise ConnectionError("sensitive text must not enter evidence")
        if broken == "invalid":
            return {"status": "PASS"}
        if broken == "mutated_quantity":
            return assessment(
                "PASS",
                StrategyDecision(
                    "OPEN", original.rationale, side="BUY", quantity="999"
                ),
            )
        return GateAssessment(
            "VETO" if broken == "veto_open" else "PENDING", "invalid", original, "{}"
        )

    install(worker, check)
    result = worker.consume(signal, context=context)
    assert (result["status"], result["reason"]) == ("IGNORED", "JEV_GATE_UNAVAILABLE")
    assert "sensitive" not in json.dumps(worker.decision(signal))
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0


def test_model_deadline_is_frozen_and_expired_prepared_order_never_posts(database):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    install(worker, lambda original, **_: assessment("PASS", original))
    result = worker.consume(signal, context=context)
    assert result["status"] == "PREPARED"
    assert worker.decision(signal)["snapshot"]["expires_at_ms"] == 5
    # Original market/account context is still valid until 9 and the original
    # worker lifetime until 8. Only the frozen JEV limit has now expired.
    runtime.clock_ms = lambda: 5
    runtime.risk_check = lambda _: pytest.fail("stale JEV must fail before risk port")
    runtime.execution.submit = lambda _: pytest.fail("expired JEV must not POST")
    assert runtime.execution.dispatch(result["order_id"]) == "DENIED"
    assert runtime.data.trace(result["intent_id"])["orders"][0]["status"] == "CANCELLED"


def test_disabled_gate_does_not_shorten_original_decision_lifetime(database):
    from v2_core.jev_entry_gate import evaluate_entry_gate

    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")
    install(
        worker,
        lambda original, **_: evaluate_entry_gate(
            original,
            symbol=None,
            public_features=None,
            advisory=None,
            now_ms=None,
            started_at_ms=None,
            deadline_ms=None,
            signal_expires_at_ms=None,
        ),
    )
    assert worker.consume(signal, context=context)["status"] == "PREPARED"
    assert worker.decision(signal)["snapshot"]["expires_at_ms"] == 8


@pytest.mark.parametrize("bound", [None, True, "5", 3, 2**53])
def test_enabled_pass_with_missing_invalid_or_expired_bound_is_terminal(
    database, bound
):
    _, runtime, signal, context = scenario(database)
    worker = formal(runtime, "S6")

    def check(original, **_):
        value = assessment("PASS", original)
        proof = json.loads(value.evidence_json)
        proof["valid_until_ms"] = bound
        return replace(value, evidence_json=json.dumps(proof))

    install(worker, check)
    result = worker.consume(signal, context=context)
    assert (result["status"], result["reason"]) == ("IGNORED", "JEV_GATE_UNAVAILABLE")
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
