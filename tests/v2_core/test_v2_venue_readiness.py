import copy
import json
from dataclasses import asdict, replace

import pytest
from test_v2_account_coverage import Account
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import evidence, intent
from test_v2_protection_child import SCOPE

from v2_core.errors import SubmissionNotSent
from v2_core.runner import ExchangeObservation, ExecutionRunner
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey
from v2_core.venue_readiness import GuardedOpeningSubmit, TestnetVenueReadiness

database = database_fixture
REF = {
    "symbol": "BTCUSDT",
    "environment": "SANDBOX",
    "mark_price": "100",
    "observed_at_ms": 1000,
}


class Venue(Account):
    def __init__(self):
        super().__init__()
        self.rows["/fapi/v3/account"]["assets"] = [
            {
                "asset": "USDT",
                "availableBalance": "100",
                "walletBalance": "100",
                "marginBalance": "100",
            }
        ]
        self.rows["/fapi/v1/accountConfig"] = {
            "canTrade": True,
            "dualSidePosition": False,
            "multiAssetsMargin": False,
        }
        self.rows["/fapi/v1/symbolConfig"] = [
            {
                "symbol": "BTCUSDT",
                "leverage": 2,
                "marginType": "ISOLATED",
                "isAutoAddMargin": False,
                "maxNotionalValue": "1000",
            }
        ]

    def __call__(self, method, path, params):
        if path == "/fapi/v1/symbolConfig":
            assert method == "GET" and params == {"symbol": "BTCUSDT"}
            return copy.deepcopy(self.rows[path])
        return super().__call__(method, path, params)


def inspect(venue, **overrides):
    return TestnetVenueReadiness(venue, scope=SCOPE, clock_ms=lambda: 1000).inspect(
        **{
            "symbol": "BTCUSDT",
            "quantity": "1",
            "leverage": 2,
            "margin_type": "ISOLATED",
            "reference": REF,
            **overrides,
        }
    )


def test_verified_venue_is_not_itself_execution_permission():
    result = inspect(Venue())
    assert result["status"] == "READY_FOR_GUARDED_SUBMISSION"
    assert result["execution_authorized"] is False
    assert result["required_margin"] == "55.00"


@pytest.mark.parametrize(
    "failure",
    [
        "position",
        "order",
        "algo",
        "permission",
        "leverage",
        "mode",
        "auto_margin",
        "balance",
        "limit",
        "duplicate",
        "unavailable",
    ],
)
def test_unsafe_account_never_passes(failure):
    venue = Venue()
    cfg = venue.rows["/fapi/v1/symbolConfig"][0]
    if failure == "position":
        venue.rows["/fapi/v3/positionRisk"] = [
            {"symbol": "ZORAUSDT", "positionSide": "BOTH", "positionAmt": "1"}
        ]
    elif failure == "order":
        venue.rows["/fapi/v1/openOrders"] = [{"symbol": "BTCUSDT", "orderId": 1}]
    elif failure == "algo":
        venue.rows["/fapi/v1/openAlgoOrders"] = [{"symbol": "BTCUSDT", "algoId": 1}]
    elif failure == "permission":
        venue.rows["/fapi/v1/accountConfig"]["canTrade"] = "true"
    elif failure == "leverage":
        cfg["leverage"] = 3
    elif failure == "mode":
        cfg["marginType"] = "CROSSED"
    elif failure == "auto_margin":
        cfg["isAutoAddMargin"] = True
    elif failure == "balance":
        venue.rows["/fapi/v3/account"]["availableBalance"] = "54.99"
    elif failure == "limit":
        cfg["maxNotionalValue"] = "99"
    elif failure == "duplicate":
        venue.rows["/fapi/v1/symbolConfig"].append(dict(cfg))
    else:
        venue.fail = "/fapi/v1/accountConfig"
    result = inspect(venue)
    assert result["status"] == "BLOCKED" and result["blockers"]
    assert "sensitive" not in json.dumps(result)


@pytest.mark.parametrize("timestamp", [-1, 1001, -10000, True])
def test_invalid_mark_time_rejected_before_io(timestamp):
    venue = Venue()
    with pytest.raises(ValueError):
        inspect(venue, reference={**REF, "observed_at_ms": timestamp})
    assert not venue.calls


@pytest.fixture
def case(database):
    data, venue = TradingData(database), Venue()
    base = evidence()
    proof = replace(
        base,
        snapshot_json=json.dumps(
            {**json.loads(base.snapshot_json), "expires_at_ms": 11000}
        ),
    )
    original = replace(intent(), evidence_ref=proof.evidence_ref)
    data.accept(original, proof)
    order, _ = data.orders.prepare(original.intent_id)
    sent = []

    def submit(snapshot):
        key = StateKey(**asdict(SCOPE), namespace="opening-readiness-v1", key=order)
        proof = json.loads(BusinessState(database).read(key).payload_json)
        assert proof["status"] == "READY_FOR_GUARDED_SUBMISSION"
        sent.append(snapshot)
        return ExchangeObservation(
            snapshot["client_order_id"], "ACKNOWLEDGED", "10", evidence={"source": "qa"}
        )

    gate = GuardedOpeningSubmit(
        database,
        readiness=TestnetVenueReadiness(venue, scope=SCOPE, clock_ms=lambda: 1000),
        reference=lambda _: REF,
        plan=lambda _: {"leverage": 2, "margin_type": "ISOLATED"},
        submit=submit,
        enabled=True,
    )
    runner = ExecutionRunner(
        database,
        submit=gate,
        query=lambda _: None,
        risk_check=lambda _: True,
        scope=SCOPE,
    )
    return data, order, venue, gate, runner, sent


def test_registered_proof_before_send_and_recovery_never_resends(case):
    _, order, _, _, runner, sent = case
    assert runner.dispatch(order) == "ACKNOWLEDGED"
    assert runner.dispatch(order) == "UNKNOWN"
    assert len(sent) == 1


def test_symbol_settings_proof_is_required_and_persisted_before_send(case):
    data, order, _, gate, runner, sent = case
    observed = []

    def configure(snapshot, terms):
        observed.append((snapshot["order_id"], terms))
        return {"status": "VERIFIED", "state_id": "settings-proof", "changed": []}

    gate.configure = configure
    assert runner.dispatch(order) == "ACKNOWLEDGED"
    key = StateKey(**asdict(SCOPE), namespace="opening-readiness-v1", key=order)
    proof = json.loads(BusinessState(data._connect).read(key).payload_json)
    assert proof["symbol_settings"]["state_id"] == "settings-proof"
    assert observed == [(order, {"leverage": 2, "margin_type": "ISOLATED"})]
    assert len(sent) == 1


def test_unverified_symbol_settings_prevent_readiness_and_send(case):
    _, order, venue, gate, runner, sent = case
    gate.configure = lambda *_: {"status": "UNCONFIRMED"}
    assert runner.dispatch(order) == "REJECTED"
    assert venue.calls == [] and sent == []


def test_bad_actual_leverage_is_definitely_not_sent(case):
    _, order, venue, _, runner, sent = case
    venue.rows["/fapi/v1/symbolConfig"][0]["leverage"] = 20
    assert runner.dispatch(order) == "REJECTED"
    assert not sent


def test_post_timeout_remains_unknown_not_rejected(case):
    _, order, _, gate, runner, _ = case
    calls = []

    def timeout(snapshot):
        calls.append(snapshot)
        raise TimeoutError("secret")

    gate.submit = timeout
    assert runner.dispatch(order) == "UNKNOWN"
    assert runner.dispatch(order) == "UNKNOWN"
    assert len(calls) == 1


def test_default_disabled_never_reads_venue(case):
    _, order, venue, gate, runner, sent = case
    gate.enabled = False
    assert runner.dispatch(order) == "REJECTED"
    assert not venue.calls and not sent


def test_reduce_only_permission_is_independent_from_entry_permission(case):
    _, _, venue, gate, _, sent = case
    gate.enabled = False
    gate.reduce_only_enabled = True
    gate.submit = lambda order: sent.append(order) or "reduced"
    close = {
        **asdict(SCOPE),
        "leg": "CLOSE",
        "reduce_only": True,
    }
    assert gate(close) == "reduced"
    assert sent == [close]
    assert not venue.calls
    gate.reduce_only_enabled = False
    with pytest.raises(SubmissionNotSent, match="DISABLED"):
        gate(close)
    assert len(sent) == 1


def test_pg_registration_failure_prevents_submission(case, monkeypatch):
    _, order, _, _, runner, sent = case
    original = BusinessState.change

    def fail(self, key, **kwargs):
        result = original(self, key, **kwargs)
        if key.namespace == "opening-readiness-v1":
            raise RuntimeError("commit failure")
        return result

    monkeypatch.setattr(BusinessState, "change", fail)
    assert runner.dispatch(order) == "REJECTED"
    assert not sent


def test_account_lock_prevents_submission(database, case):
    _, order, _, _, runner, sent = case
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        )
        conn.commit()
        assert runner.dispatch(order) == "REJECTED"
    assert not sent


def test_expiry_after_pg_proof_prevents_submission(case):
    _, order, _, gate, runner, sent = case
    original = gate.readiness.inspect

    def expires(**kwargs):
        result = original(**kwargs)
        gate.readiness.clock = lambda: 11000
        return result

    gate.readiness.inspect = expires
    assert runner.dispatch(order) == "REJECTED"
    assert not sent


def test_other_collateral_cannot_cover_insufficient_usdt():
    venue = Venue()
    venue.rows["/fapi/v3/account"]["assets"][0]["availableBalance"] = "0"
    assert "INSUFFICIENT_VENUE_MARGIN" in inspect(venue)["blockers"]


def test_missing_usdt_is_not_global_balance_fallback():
    venue = Venue()
    venue.rows["/fapi/v3/account"]["assets"] = []
    assert "INVALID_VENUE_READINESS_RESPONSE" in inspect(venue)["blockers"]


def test_explicit_external_position_does_not_mask_target_or_orders():
    venue = Venue()
    venue.rows["/fapi/v3/positionRisk"] = [
        {"symbol": "ZORAUSDT", "positionSide": "BOTH", "positionAmt": "26399"}
    ]
    readiness = TestnetVenueReadiness(
        venue,
        scope=SCOPE,
        clock_ms=lambda: 1000,
        excluded_position_symbols=("ZORAUSDT",),
    )
    assert readiness.inspect(
        symbol="BTCUSDT",
        quantity="1",
        leverage=2,
        margin_type="ISOLATED",
        reference=REF,
    )["status"] == "READY_FOR_GUARDED_SUBMISSION"
    with pytest.raises(ValueError, match="excluded"):
        readiness.inspect(
            symbol="ZORAUSDT",
            quantity="1",
            leverage=2,
            margin_type="ISOLATED",
            reference={**REF, "symbol": "ZORAUSDT"},
        )
    venue.rows["/fapi/v1/openOrders"] = [{"symbol": "ZORAUSDT", "orderId": 1}]
    result = readiness.inspect(
        symbol="BTCUSDT",
        quantity="1",
        leverage=2,
        margin_type="ISOLATED",
        reference=REF,
    )
    assert "EXISTING_ORDINARY_ORDERS" in result["blockers"]


def test_persisted_local_exposure_blocks_even_if_venue_claims_flat(case):
    data, order, _, _, runner, sent = case
    data.orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    data.ledger.record_fill(
        order_id=order,
        exchange_fill_id="1",
        quantity="0.001",
        price="100",
        fee="0",
        fee_currency="USDT",
        occurred_at_ms=1,
        evidence={"source": "qa"},
    )
    from v2_core.errors import SubmissionNotSent

    with pytest.raises(SubmissionNotSent, match="VENUE_READINESS_BLOCKED"):
        runner.submit(runner.snapshot(order))
    assert not sent


def test_tampered_order_symbol_cannot_pass_db_identity(case):
    data, order, _, gate, runner, sent = case
    data.orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    snapshot = runner.snapshot(order)
    snapshot["side"] = "SELL"
    from v2_core.errors import SubmissionNotSent

    with pytest.raises(SubmissionNotSent, match="VENUE_READINESS_BLOCKED"):
        gate(snapshot)
    assert not sent


@pytest.mark.parametrize("mismatch", [False, True])
def test_directional_factory_uses_persisted_actual_strategy_terms(database, mismatch):
    from test_v2_directional_admission import formal
    from test_v2_directional_replay import scenario

    from services.v2_directional_execution import bind_directional_venue_gate
    from v2_core.errors import SubmissionNotSent

    _, runtime, signal, context = scenario(database)
    admission = formal(runtime)
    result = admission.consume(signal, context=context)
    desired = admission.decision(signal)["snapshot"]["features"]["evaluation"][
        "market_plan"
    ]
    venue, sent = Venue(), []
    venue.rows["/fapi/v1/symbolConfig"][0].update(
        leverage=20 if mismatch else desired["leverage"],
        marginType=desired["margin_mode"],
    )

    def submit(order):
        sent.append(order)
        return "sent"

    runtime.execution.submit = submit
    gate = bind_directional_venue_gate(
        runtime,
        venue,
        mark_reference=lambda _: {**REF, "observed_at_ms": 3},
        enabled=True,
    )
    with pytest.raises(ValueError, match="already bound"):
        bind_directional_venue_gate(runtime, venue, mark_reference=lambda _: REF)
    # Test the post-CAS binding, not the runtime's separate reservation policy.
    runtime.data.orders.transition(
        result["order_id"], expected_version=1, status="SUBMITTING", evidence={}
    )
    order = runtime.execution.snapshot(result["order_id"])
    if mismatch:
        with pytest.raises(SubmissionNotSent):
            gate(order)
        assert not sent
    else:
        assert gate(order) == "sent"
        assert len(sent) == 1


def test_entry_deadline_not_extended_by_fresh_account_read(database, case):
    _, order, _, gate, runner, sent = case
    # Original evidence expires at 11000; fresh quote cannot extend it.
    gate.readiness.clock = lambda: 12000
    gate.readiness.inventory.clock_ms = lambda: 12000
    gate.reference = lambda _: {**REF, "observed_at_ms": 12000}
    assert runner.dispatch(order) == "REJECTED"
    assert not sent
