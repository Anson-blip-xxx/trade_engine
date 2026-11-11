"""Native stop creation binds observed entry precision; never a cash epsilon."""

from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Inexact, localcontext
from time import time_ns

import pytest
from test_v2_observed_entry_precision import inventory
from test_v2_precision_contract import SCOPE, cash_case

from services.v2_directional_cash import _entry_observations, reconcile_cash
from tests.support.v2_admission import database as database_fixture
from v2_core.account_inventory import persist_inventory
from v2_core.evidence import canonical, digest
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.settlement_precision import OBSERVED_ENTRY_MODEL, rounding_proof
from v2_core.state import BusinessState, StateKey

database = database_fixture
OPENED = 1793783868891
UPDATED = 1793783868898
STARTED = 1793783873507
FINISHED = 1793783874073
CLOSED = 1793786470095


def native_case(side="SELL"):
    pnl = "-0.28829999" if side == "SELL" else "0.28829999"
    trace, report, rows = cash_case(
        side, [("25", ".2039"), ("351", ".2034")], [("376", ".2042", pnl)]
    )
    trace["request"]["symbol"] = "ICNTUSDT"
    close_side = "BUY" if side == "SELL" else "SELL"
    for index, fill in enumerate(trace["fills"]):
        fill["occurred_at_ms"] = OPENED if index < 2 else CLOSED
    for row in rows:
        row["symbol"] = "ICNTUSDT"
        row["occurred_at_ms"] = OPENED if int(row["source_id"]) < 2 else CLOSED
    observed = inventory(
        SCOPE,
        symbol="ICNTUSDT",
        side=side,
        quantity="376",
        entry="0.2034332446809",
        updated=UPDATED,
        start=STARTED,
        end=FINISHED,
    )
    spec = ProtectionSpec(
        trace["intent_id"],
        "ICNTUSDT",
        close_side,
        "STOP_MARKET",
        ".2041" if side == "SELL" else ".2029",
    )

    class Transport:
        account_id, environment = SCOPE.account_id, SCOPE.environment

        def __call__(self, *_args):
            raise AssertionError("precision tests must never call the exchange")

    protection = TestnetProtection(None, Transport(), scope=SCOPE)
    key = protection.key(spec)
    client = protection.params(spec)["clientAlgoId"]
    raw = {
        "symbol": spec.symbol,
        "side": close_side,
        "positionSide": "BOTH",
        "type": "MARKET",
        "origType": "STOP_MARKET",
        "reduceOnly": True,
        "closePosition": True,
        "orderId": 190001,
        "clientOrderId": client,
        "origQty": "376",
        "executedQty": "376",
        "status": "FILLED",
        "time": CLOSED,
        "updateTime": CLOSED,
    }
    binding = {
        "origin": "BINANCE_EXTERNAL_CLOSE",
        "kind": "EXTERNAL_REDUCE_ONLY",
        "exchange_order_id": "190001",
        "venue_client_order_id": client,
        "venue_order": raw,
    }
    close = trace["orders"][-1]
    close.update(
        symbol="ICNTUSDT",
        side=close_side,
        order_type="MARKET",
        reduce_only=True,
        exchange_order_id="190001",
        client_order_id="local-adopted-close-id",
        request_evidence=binding,
    )
    trace["fills"][-1]["evidence"].update(
        order_id="190001", symbol="ICNTUSDT", external_close_binding=deepcopy(binding)
    )
    creation = {
        "spec": asdict(spec),
        "status": "SENDING",
        "creation_proof": {
            "source": "exclusive-testnet-stop-install-v1",
            "account_scope": asdict(SCOPE),
            "spec": asdict(spec),
            "inventory": deepcopy(observed),
            "facts_digest": digest(canonical({"fixture": "exclusive-position"})),
            "reference": {
                "symbol": "ICNTUSDT",
                "environment": "SANDBOX",
                "observed_at_ms": FINISHED,
                "mark_price": ".2038",
                "tick_size": ".0001",
            },
            "deadline_ms": FINISHED + 10000,
        },
    }
    submitted = {
        **protection.params(spec),
        "orderType": "STOP_MARKET",
        "closePosition": True,
        "priceProtect": False,
        "algoId": 190000,
        "algoStatus": "NEW",
    }
    current = {
        **deepcopy(creation),
        "status": "EXTERNAL_FLAT_RECONCILED",
        "submission": deepcopy(submitted),
        "observation": {**submitted, "actualOrderId": "190001"},
    }
    witness = {
        "inventory": observed,
        "protection_creation": creation,
        "protection_state": current,
        "protection_state_id": key.identity,
        "protection_creation_version": 1,
        "protection_state_version": 3,
        "creation_recorded_at_ms": FINISHED + 10,
        "protection_owner": {
            "episode": trace["intent_id"],
            "binding": deepcopy(binding),
        },
    }
    return trace, report, rows, {close["order_id"]: witness}, key


def _coherent(witness, trace):
    """Update dependent facts to isolate the guard under test, not a stale copy."""
    observation = witness["inventory"]
    observation["response_digests"] = {
        name: digest(canonical({"response": value}))
        for name, value in observation["responses"].items()
    }
    creation = witness["protection_creation"]
    creation["creation_proof"]["inventory"] = deepcopy(observation)
    witness["protection_state"]["creation_proof"] = deepcopy(creation["creation_proof"])
    binding = trace["orders"][-1]["request_evidence"]
    witness["protection_owner"]["binding"] = deepcopy(binding)
    trace["fills"][-1]["evidence"]["external_close_binding"] = deepcopy(binding)


def as_native_child(trace, witness):
    request = trace["orders"][-1]["request_evidence"]
    binding = {
        "origin": "BINANCE_ALGO_CHILD",
        "parent_algo_id": 190000,
        "parent_client_algo_id": request["venue_client_order_id"],
        "exchange_order_id": request["exchange_order_id"],
        "venue_client_order_id": "exchange-generated-child",
    }
    trace["orders"][-1]["request_evidence"] = binding
    proof = trace["fills"][-1]["evidence"]
    del proof["external_close_binding"]
    # Real native-child fills retain the adapter's order_id/symbol facts;
    # the nested parent binding belongs to the order/owner, not every fill.
    witness["protection_owner"]["binding"] = deepcopy(binding)


@pytest.mark.parametrize("precision", [6, 28, 100])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("origin", ["BINANCE_EXTERNAL_CLOSE", "BINANCE_ALGO_CHILD"])
def test_native_creation_proves_actual_icnt_precision_without_epsilon(
    precision, side, origin
):
    trace, report, rows, witnesses, key = native_case(side)
    witness = next(iter(witnesses.values()))
    if origin == "BINANCE_ALGO_CHILD":
        as_native_child(trace, witness)
    original = deepcopy((trace, report, rows, witnesses))
    with localcontext() as context:
        context.prec = precision
        context.traps[Inexact] = True
        with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
            reconcile_cash(trace, report, rows, scope=SCOPE)
        proof = reconcile_cash(
            trace, report, rows, scope=SCOPE, entry_observations=witnesses
        )
        pure = rounding_proof(trace, report, entry_observations=witnesses)
    assert proof["status"] == "CASH_MATCHED"
    assert proof["settlement_authorized"] is False
    assert proof["precision_reconciliation"] == pure
    assert pure["model"] == OBSERVED_ENTRY_MODEL
    assert pure["amount"] == ("0.00000001" if side == "SELL" else "-0.00000001")
    entry = pure["fills"][0]["entry_observation"]
    assert entry["source"] == "native-protection-creation-v1"
    assert entry["entry_price"] == "0.2034332446809"
    assert entry["entry_decimal_places"] == 13
    assert entry["protection_state_id"] == key.identity
    assert (trace, report, rows, witnesses) == original


@pytest.mark.parametrize("origin", ["BINANCE_EXTERNAL_CLOSE", "BINANCE_ALGO_CHILD"])
def test_real_trace_without_top_level_exchange_order_id_uses_bound_fill_facts(origin):
    trace, report, rows, witnesses, _ = native_case()
    if origin == "BINANCE_ALGO_CHILD":
        as_native_child(trace, next(iter(witnesses.values())))
        assert "native_child_binding" not in trace["fills"][-1]["evidence"]
    del trace["orders"][-1]["exchange_order_id"]
    assert (
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)[
            "status"
        ]
        == "CASH_MATCHED"
    )


@pytest.mark.parametrize("origin", ["BINANCE_EXTERNAL_CLOSE", "BINANCE_ALGO_CHILD"])
@pytest.mark.parametrize(
    "defect",
    [
        "order_id",
        "symbol",
        "missing_order",
        "missing_symbol",
        "boolean_order",
        "integer_order",
    ],
)
def test_fill_requires_exact_venue_order_id_and_symbol(origin, defect):
    trace, report, rows, witnesses, _ = native_case()
    if origin == "BINANCE_ALGO_CHILD":
        as_native_child(trace, next(iter(witnesses.values())))
    proof = trace["fills"][-1]["evidence"]
    if defect == "order_id":
        proof["order_id"] = "190002"
    elif defect == "symbol":
        proof["symbol"] = "OTHERUSDT"
    elif defect == "missing_order":
        del proof["order_id"]
    elif defect == "missing_symbol":
        del proof["symbol"]
    elif defect == "boolean_order":
        proof["order_id"] = True
    else:
        proof["order_id"] = 190001
    with pytest.raises(ValueError, match="PNL_ENTRY_OBSERVATION_INVALID"):
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)


def test_native_optional_nested_binding_if_present_must_match():
    trace, report, rows, witnesses, _ = native_case()
    as_native_child(trace, next(iter(witnesses.values())))
    binding = trace["orders"][-1]["request_evidence"]
    proof = trace["fills"][-1]["evidence"]
    proof["native_child_binding"] = deepcopy(binding)
    assert (
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)[
            "status"
        ]
        == "CASH_MATCHED"
    )
    proof["native_child_binding"]["exchange_order_id"] = "190002"
    with pytest.raises(ValueError, match="PNL_ENTRY_OBSERVATION_INVALID"):
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)


@pytest.mark.parametrize(
    "defect",
    [
        "creation_account",
        "inventory_account",
        "creation_source",
        "creation_spec",
        "current_spec",
        "current_creation",
        "state_id",
        "creation_version",
        "state_version",
        "facts_digest",
        "reference_symbol",
        "reference_environment",
        "deadline",
        "created_before_inventory",
        "created_after_close",
        "owner_episode",
        "owner_binding",
        "origin",
        "liquidation",
        "manual",
        "exchange_order",
        "venue_order_id",
        "venue_symbol",
        "venue_side",
        "venue_hedge",
        "venue_type",
        "venue_status",
        "venue_reduce_only",
        "venue_not_close_all",
        "venue_quantity",
        "venue_executed_quantity",
        "venue_client",
        "fill_binding",
        "submission_client",
        "observation_client",
        "submission_symbol",
        "observation_side",
        "submission_hedge",
        "observation_type",
        "submission_not_close_all",
        "observation_not_close_all",
        "submission_algo_type",
        "observation_algo_type",
        "submission_working_type",
        "observation_working_type",
        "submission_price_protect",
        "observation_price_protect",
        "inventory_digest",
        "inventory_quantity",
        "inventory_entry",
        "inventory_disagreement",
        "inventory_after_close",
        "inventory_before_open",
        "inventory_failures",
        "inventory_blocker",
        "coarse_entry",
        "income",
    ],
)
def test_native_creation_rejects_unbound_or_corrupt_evidence(defect):
    trace, report, rows, witnesses, _ = native_case()
    witness = next(iter(witnesses.values()))
    creation, current = witness["protection_creation"], witness["protection_state"]
    cp, observation = creation["creation_proof"], witness["inventory"]
    close = trace["orders"][-1]
    request, raw = close["request_evidence"], close["request_evidence"]["venue_order"]
    before = observation["responses"]["positions_before"][0]
    if defect == "creation_account":
        cp["account_scope"]["account_id"] = "another-account"
    elif defect == "inventory_account":
        observation["account_scope"]["account_id"] = "another-account"
    elif defect == "creation_source":
        cp["source"] = "manual-stop"
    elif defect == "creation_spec":
        cp["spec"]["symbol"] = "OTHERUSDT"
    elif defect == "current_spec":
        current["spec"]["trigger_price"] = ".205"
    elif defect == "current_creation":
        current["creation_proof"]["facts_digest"] = "0" * 64
    elif defect == "state_id":
        witness["protection_state_id"] = trace["intent_id"]
    elif defect == "creation_version":
        witness["protection_creation_version"] = True
    elif defect == "state_version":
        witness["protection_state_version"] = True
    elif defect == "facts_digest":
        cp["facts_digest"] = "not-a-digest"
    elif defect.startswith("reference_"):
        cp["reference"][defect.removeprefix("reference_")] = "OTHER"
    elif defect == "deadline":
        cp["deadline_ms"] = FINISHED
    elif defect == "created_before_inventory":
        witness["creation_recorded_at_ms"] = FINISHED - 1
    elif defect == "created_after_close":
        witness["creation_recorded_at_ms"] = CLOSED
    elif defect == "owner_episode":
        witness["protection_owner"]["episode"] = observation["observation_id"]
    elif defect == "owner_binding":
        witness["protection_owner"]["binding"]["exchange_order_id"] = "190002"
    elif defect == "origin":
        request["origin"] = "APPROVED_TESTNET_MAINTENANCE"
    elif defect == "liquidation":
        request["kind"] = "LIQUIDATION"
        request["venue_client_order_id"] = raw["clientOrderId"] = "autoclose-190001"
    elif defect == "manual":
        request["venue_client_order_id"] = raw["clientOrderId"] = "manual-close"
    elif defect == "exchange_order":
        close["exchange_order_id"] = "190002"
    elif defect.startswith("venue_"):
        field, value = {
            "venue_order_id": ("orderId", 190002),
            "venue_symbol": ("symbol", "OTHERUSDT"),
            "venue_side": ("side", "SELL"),
            "venue_hedge": ("positionSide", "SHORT"),
            "venue_type": ("type", "LIMIT"),
            "venue_status": ("status", "PARTIALLY_FILLED"),
            "venue_reduce_only": ("reduceOnly", False),
            "venue_not_close_all": ("closePosition", False),
            "venue_quantity": ("origQty", "375"),
            "venue_executed_quantity": ("executedQty", "375"),
            "venue_client": ("clientOrderId", "v2p-" + "0" * 28),
        }[defect]
        raw[field] = value
    elif defect == "fill_binding":
        trace["fills"][-1]["evidence"]["external_close_binding"][
            "exchange_order_id"
        ] = "190002"
    elif defect.startswith(("submission_", "observation_")):
        target, change = defect.split("_", 1)
        field, value = {
            "client": ("clientAlgoId", "v2p-" + "0" * 28),
            "symbol": ("symbol", "OTHERUSDT"),
            "side": ("side", "SELL"),
            "hedge": ("positionSide", "SHORT"),
            "type": ("orderType", "TAKE_PROFIT_MARKET"),
            "not_close_all": ("closePosition", False),
            "algo_type": ("algoType", "OTHER"),
            "working_type": ("workingType", "CONTRACT_PRICE"),
            "price_protect": ("priceProtect", True),
        }[change]
        current[target][field] = value
    elif defect == "inventory_digest":
        observation["response_digests"]["positions_before"] = "0" * 64
    elif defect == "inventory_quantity":
        before["positionAmt"] = "-375"
    elif defect in {"inventory_entry", "inventory_disagreement"}:
        before["entryPrice"] = "0.2034332446808"
    elif defect == "inventory_after_close":
        observation["finished_at_ms"] = CLOSED
    elif defect == "inventory_before_open":
        before["updateTime"] = OPENED - 1
    elif defect == "inventory_failures":
        observation["failures"] = {"positions": "UNAVAILABLE"}
    elif defect == "inventory_blocker":
        observation["blockers"].append("POSITIONS_CHANGED_DURING_READ")
    elif defect == "coarse_entry":
        before["entryPrice"] = "0.203433245"
    elif defect == "income":
        rows[-1]["amount"] = "-0.28829998"
    else:
        raise AssertionError(defect)
    if defect != "inventory_disagreement":
        observation["responses"]["positions_after"][0] = deepcopy(before)
    if defect not in {
        "current_creation",
        "owner_binding",
        "fill_binding",
        "inventory_digest",
    }:
        _coherent(witness, trace)
    with pytest.raises(ValueError):
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)


@pytest.mark.parametrize("pnl", ["-0.28829998", "-0.28830001"])
def test_native_entry_does_not_accept_neighboring_cash_quantum(pnl):
    trace, report, rows, witnesses, _ = native_case()
    trace["fills"][-1]["evidence"]["venue_realized_pnl"] = pnl
    rows[-1]["amount"] = pnl
    with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)


@pytest.mark.parametrize("algo_id", [True, 0, -1, "190000", None])
def test_matching_parent_ids_still_require_positive_integer_identity(algo_id):
    trace, report, rows, witnesses, _ = native_case()
    current = next(iter(witnesses.values()))["protection_state"]
    current["submission"]["algoId"] = current["observation"]["algoId"] = algo_id
    with pytest.raises(ValueError, match="PNL_ENTRY_OBSERVATION_INVALID"):
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_same_native_close_multiple_fills_prove_each_cash_rounding(side):
    trace, report, rows, witnesses, _ = native_case(side)
    pnl = "-0.14414999" if side == "SELL" else "0.14414999"
    closing = trace["fills"][-1]
    closing["quantity"] = "188"
    closing["evidence"]["venue_realized_pnl"] = pnl
    second = deepcopy(closing)
    second["occurred_at_ms"] += 1
    second["evidence"]["trade_id"] = "3"
    trace["fills"].append(second)
    rows[-1]["amount"] = pnl
    for row in deepcopy(rows[-2:]):
        row["income_id"] = row["income_type"] + "-3"
        row["source_id"] = row["evidence"]["trade_id"] = "3"
        row["occurred_at_ms"] += 1
        rows.append(row)
    report["fees"], report["accounting_revision"] = "0.0004", 4
    # The aggregate correction may exceed one quantum because each fill is
    # truncated separately. It must be derived, never admitted by an epsilon.
    original = deepcopy((trace, report, rows, witnesses))
    with localcontext() as context:
        context.prec = 6
        context.traps[Inexact] = True
        with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
            reconcile_cash(trace, report, rows, scope=SCOPE)
        proof = reconcile_cash(
            trace, report, rows, scope=SCOPE, entry_observations=witnesses
        )
    precision = proof["precision_reconciliation"]
    assert precision["model"] == OBSERVED_ENTRY_MODEL
    assert precision["amount"] == ("0.00000002" if side == "SELL" else "-0.00000002")
    assert len(precision["fills"]) == 2
    assert all(
        fill["method"] == "OBSERVED_ENTRY_TRUNCATE_TOWARD_ZERO_8"
        for fill in precision["fills"]
    )
    assert proof["settlement_authorized"] is False
    assert (trace, report, rows, witnesses) == original
    # Even with a valid larger aggregate correction, a one-quantum error in an
    # individual fill still fails rather than borrowing tolerance from the rest.
    changed = "-0.14414998" if side == "SELL" else "0.14414998"
    trace["fills"][-1]["evidence"]["venue_realized_pnl"] = changed
    rows[-1]["amount"] = changed
    with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)


@pytest.mark.parametrize(
    "defect", ["parent_client", "parent_id", "child_id", "fill_binding"]
)
def test_native_child_requires_exact_parent_and_child_binding(defect):
    trace, report, rows, witnesses, _ = native_case()
    witness = next(iter(witnesses.values()))
    as_native_child(trace, witness)
    binding = trace["orders"][-1]["request_evidence"]
    if defect == "parent_client":
        binding["parent_client_algo_id"] = "v2p-" + "0" * 28
    elif defect == "parent_id":
        binding["parent_algo_id"] = 190099
    elif defect == "child_id":
        witness["protection_state"]["observation"]["actualOrderId"] = "190099"
    else:
        trace["fills"][-1]["evidence"]["native_child_binding"] = {
            **deepcopy(binding),
            "exchange_order_id": "190099",
        }
    if defect != "fill_binding":
        witness["protection_owner"]["binding"] = deepcopy(binding)
    with pytest.raises(ValueError):
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)


def _persist_native(
    database,
    *,
    origin="BINANCE_EXTERNAL_CLOSE",
    creation_reason="TESTNET_PROTECTION_PROTOCOL",
    owner_reason=None,
):
    trace, report, rows, witnesses, key = native_case()
    witness = next(iter(witnesses.values()))
    # Real history created_at is authoritative. Shift the synthetic venue facts
    # around now instead of editing the immutable history timestamp.
    shift = time_ns() // 1_000_000 - FINISHED - 3000
    for fill in trace["fills"]:
        fill["occurred_at_ms"] += shift
    for row in rows:
        row["occurred_at_ms"] += shift
    observed = witness["inventory"]
    observed["started_at_ms"] += shift
    observed["finished_at_ms"] += shift
    for name in ("positions_before", "positions_after"):
        observed["responses"][name][0]["updateTime"] += shift
    cp = witness["protection_creation"]["creation_proof"]
    cp["reference"]["observed_at_ms"] += shift
    cp["deadline_ms"] += shift
    raw = trace["orders"][-1]["request_evidence"]["venue_order"]
    raw["time"] += shift
    raw["updateTime"] += shift
    _coherent(witness, trace)
    if origin == "BINANCE_ALGO_CHILD":
        as_native_child(trace, witness)
    persist_inventory(database, scope=SCOPE, observation=observed)
    state = BusinessState(database)
    state.change(
        key,
        expected_version=0,
        request_key="creation",
        reason=creation_reason,
        payload=witness["protection_creation"],
    )
    state.change(
        key,
        expected_version=1,
        request_key="latest",
        reason="QA",
        payload=witness["protection_state"],
    )
    owner_key = StateKey(
        **asdict(SCOPE), namespace="native-child-owner-v1", key="ICNTUSDT:190001"
    )
    state.change(
        owner_key,
        expected_version=0,
        request_key="owner",
        reason=owner_reason
        or (
            "CONFIRMED_EXTERNAL_CLOSE"
            if origin == "BINANCE_EXTERNAL_CLOSE"
            else "CONFIRMED_PARENT_CHILD_IDENTITY"
        ),
        payload=witness["protection_owner"],
    )
    return trace, report, rows, key, owner_key


@pytest.mark.parametrize("origin", ["BINANCE_EXTERNAL_CLOSE", "BINANCE_ALGO_CHILD"])
def test_pg_native_loader_uses_creation_history_with_latest_reconciled_head(
    database, origin
):
    trace, report, rows, key, _ = _persist_native(database, origin=origin)
    with database() as conn:
        witnesses = _entry_observations(conn, SCOPE, trace)
    witness = witnesses[trace["orders"][-1]["order_id"]]
    assert witness["protection_state_id"] == key.identity
    assert witness["protection_creation_version"] == 1
    assert witness["protection_state_version"] == 2
    assert witness["protection_creation"]["status"] == "SENDING"
    assert witness["protection_state"]["status"] == "EXTERNAL_FLAT_RECONCILED"
    assert witness["creation_recorded_at_ms"] >= witness["inventory"]["finished_at_ms"]
    assert (
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)[
            "precision_reconciliation"
        ]["amount"]
        == "0.00000001"
    )


@pytest.mark.parametrize(
    "defect", ["wrong_scope", "missing_state", "wrong_owner", "changed_creation"]
)
def test_pg_native_loader_never_searches_unrelated_protection_history(database, defect):
    trace, report, rows, key, owner_key = _persist_native(database)
    state = BusinessState(database)
    selected_scope = SCOPE
    if defect == "wrong_scope":
        selected_scope = replace(SCOPE, account_id="another-account")
    elif defect == "missing_state":
        trace["intent_id"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    elif defect == "wrong_owner":
        state.change(
            owner_key,
            expected_version=1,
            request_key="moved-owner",
            reason="QA",
            payload={"episode": "another-episode", "binding": {}},
        )
    else:
        import json

        current = json.loads(state.read(key).payload_json)
        current["creation_proof"]["facts_digest"] = "0" * 64
        state.change(
            key,
            expected_version=2,
            request_key="changed-proof",
            reason="QA",
            payload=current,
        )
    with database() as conn, pytest.raises(ValueError):
        witnesses = _entry_observations(conn, selected_scope, trace)
        reconcile_cash(
            trace, report, rows, scope=selected_scope, entry_observations=witnesses
        )


@pytest.mark.parametrize("origin", ["BINANCE_EXTERNAL_CLOSE", "BINANCE_ALGO_CHILD"])
@pytest.mark.parametrize("defect", ["creation_reason", "owner_reason"])
def test_pg_loader_requires_original_protocol_history_reason(database, origin, defect):
    trace, _, _, _, _ = _persist_native(database, origin=origin, **{defect: "MANUAL"})
    with (
        database() as conn,
        pytest.raises(ValueError, match="PNL_ENTRY_OBSERVATION_INVALID"),
    ):
        _entry_observations(conn, SCOPE, trace)


@pytest.mark.parametrize("kind", ["EXTERNAL_REDUCE_ONLY", "LIQUIDATION"])
def test_loader_does_not_search_for_unknown_manual_or_liquidation_entry(kind):
    trace, report, rows, _, _ = native_case()
    request = trace["orders"][-1]["request_evidence"]
    request["kind"] = kind
    request["venue_client_order_id"] = request["venue_order"]["clientOrderId"] = (
        "manual-close" if kind == "EXTERNAL_REDUCE_ONLY" else "autoclose-190001"
    )

    class NoQuery:
        def execute(self, *_args):
            raise AssertionError(
                "unowned manual/liquidation entry must not search history"
            )

    witnesses = _entry_observations(NoQuery(), SCOPE, trace)
    assert witnesses == {}
    with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
        reconcile_cash(trace, report, rows, scope=SCOPE, entry_observations=witnesses)
    # Ordinary exact economics remain valid, without inventing entry evidence.
    trace["fills"][-1]["evidence"]["venue_realized_pnl"] = "-0.2883"
    rows[-1]["amount"] = "-0.2883"
    proof = reconcile_cash(
        trace, report, rows, scope=SCOPE, entry_observations=witnesses
    )
    assert proof["status"] == "CASH_MATCHED"
    assert proof["precision_reconciliation"]["amount"] == "0"
