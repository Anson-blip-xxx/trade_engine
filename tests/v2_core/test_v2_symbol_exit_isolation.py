"""Two-symbol, real-PG exit isolation; venue HTTP is always substituted.

BTC has a verified NEW stop and a normal strategy exit. ETH has unresolved
bound CLOSE evidence and no confirmed stop. Isolation may reduce BTC only;
it must never claim that the account or ETH has become safe.
"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_credential_console import add
from test_v2_directional_exit_stage import ExitVenue, observation, setup_exit
from test_v2_directional_lifecycle import SCOPE, opening
from test_v2_execution_routes import case, routes, setup
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_exit import DirectionalExitStage
from v2_core.binance import BinanceFutures
from v2_core.guarded_transport import GuardedSignedRequest
from v2_core.runtime_policy import PolicyStore
from v2_core.transport import ExchangeTransportError

database = database_fixture
__all__ = ["case", "routes", "setup"]


class TwoSymbolVenue(ExitVenue):
    def __call__(self, method, path, params):
        if (
            path == "/fapi/v1/order"
            and method == "GET"
            and (params["symbol"] == "ETHUSDT" or self.close is None)
        ):
            self.calls.append((method, path, deepcopy(params)))
            raise ExchangeTransportError(
                "EXCHANGE_RESPONSE_ERROR", status=400, code=-2013
            )
        if path == "/fapi/v1/order" and method == "POST":
            assert params["symbol"] == "BTCUSDT", "unsafe symbol must never submit"
            assert params["reduceOnly"] == "true"
        return super().__call__(method, path, params)


def pending_close(runtime, episode, *, exchange_id="801"):
    oid, _ = runtime.data.orders.prepare(
        episode,
        leg="CLOSE",
        quantity="1.25",
        request_key="qa-unconfirmed-close",
        evidence={"source": "qa historical submission"},
    )
    assert runtime.data.orders.transition(
        oid, expected_version=1, status="SUBMITTING", evidence={"source": "qa"}
    )
    assert runtime.data.orders.transition(
        oid,
        expected_version=2,
        status="ACKNOWLEDGED",
        exchange_order_id=exchange_id,
        evidence={"source": "qa accepted response, no authoritative fills"},
    )
    return oid


def build_case(database, *, enabled=True, bound_close=True):
    runtime, btc, original, _ = setup_exit(database, allow_writes=True)
    # Preserve the genuinely registered BTC protection and fill evidence.
    venue = TwoSymbolVenue()
    venue.rows = deepcopy(original.rows)
    venue.parent = deepcopy(original.parent)
    _, eth = opening(database, "S8", symbol="ETHUSDT")
    venue.rows["/fapi/v3/positionRisk"].append(
        {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "-1.25"}
    )
    unresolved = pending_close(
        runtime, eth["intent_id"], exchange_id="801" if bound_close else None
    )
    if enabled:
        policy = PolicyStore(database, SCOPE)
        policy.patch(
            {"exit.symbol_isolation_enabled": True},
            expected_version=policy.read().version,
            reason="QA bounded reduce-only symbol isolation",
        )
    clock = [600003]
    adapter = BinanceFutures(
        venue, account_id=SCOPE.account_id, environment=SCOPE.environment
    )
    runtime.execution.submit, runtime.execution.query = adapter.submit, adapter.query

    def stage():
        return DirectionalExitStage(
            database,
            venue,
            runtime=runtime,
            scope=SCOPE,
            observe=lambda symbol: observation(
                clock[0], symbol=symbol, funding_rate="0.006"
            ),
            clock_ms=lambda: clock[0],
            reference=lambda _: {},
            allow_writes=True,
        )

    return SimpleNamespace(
        runtime=runtime,
        btc=btc,
        eth=eth,
        unresolved=unresolved,
        venue=venue,
        clock=clock,
        restart=stage,
        stage=stage(),
    )


def assert_no_new_orders(case, result):
    assert result["status"] == "BLOCKED", result
    assert result["execution_authorized"] is False
    assert not case.venue.order_writes
    assert case.runtime.execution.snapshot(case.unresolved)["status"] == "ACKNOWLEDGED"


def test_other_symbol_unknown_close_does_not_prevent_normal_protected_reduction(
    database,
):
    case = build_case(database)
    before = case.runtime.execution.snapshot(case.unresolved)
    coverage = case.stage.audit.run_once()
    assert coverage["status"] == "ACCOUNT_COVERAGE_BLOCKED"
    assert set(coverage["blockers"]) == {
        "LOCAL_ORDERS_NOT_FINAL",
        "MISSING_CONFIRMED_STOP",
    }
    result = case.stage.run_once()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "CLOSE_RECOVERY_PENDING"
    assert result["execution_authorized"] is False
    assert result["recovery"][case.unresolved] == "UNKNOWN"
    closed = result["exits"][case.btc["intent_id"]]
    assert closed["status"] == "DISPATCHED", result
    assert closed["reason"] == "ADVERSE_FUNDING"
    assert closed["outcome"] == "ACKNOWLEDGED"
    assert len(case.venue.order_writes) == 1
    wire = case.venue.order_writes[0]
    assert (wire["symbol"], wire["side"], wire["quantity"], wire["reduceOnly"]) == (
        "BTCUSDT",
        "SELL",
        "1.25",
        "true",
    )
    assert case.runtime.execution.snapshot(case.unresolved) == before
    assert case.venue.parent["algoStatus"] == "NEW"
    after = case.stage.audit.run_once()
    assert after["status"] == "ACCOUNT_COVERAGE_BLOCKED"
    assert set(coverage["blockers"]) <= set(after["blockers"])


def test_repeat_and_process_restart_recover_existing_identity_without_reposting(
    database,
):
    case = build_case(database)
    first = case.stage.run_once()
    close = first["exits"][case.btc["intent_id"]]
    assert close["status"] == "DISPATCHED", first
    for stage in (case.stage, case.restart()):
        result = stage.run_once()
        assert result["status"] == "BLOCKED"
        assert len(case.venue.order_writes) == 1
        assert result["recovery"][case.unresolved] == "UNKNOWN"
    btc_closes = [
        row
        for row in case.runtime.data.trace(case.btc["intent_id"])["orders"]
        if row["leg"] == "CLOSE"
    ]
    assert len(btc_closes) == 1 and btc_closes[0]["order_id"] == close["order_id"]


@pytest.mark.parametrize(
    "defect",
    [
        "unknown_open",
        "unbound_close",
        "unknown_position",
        "target_ordinary_order",
        "target_pending_close",
        "triggered_stop",
        "quantity_mismatch",
        "disabled",
    ],
)
def test_uncertain_or_unowned_exposure_never_qualifies_for_symbol_isolation(
    database, defect
):
    case = build_case(
        database, enabled=defect != "disabled", bound_close=defect != "unbound_close"
    )
    if defect == "unknown_open":
        _, pending = opening(database, "S6", filled=False, symbol="SOLUSDT")
        orders = case.runtime.data.orders
        assert orders.transition(
            pending["order_id"], expected_version=1, status="SUBMITTING", evidence={}
        )
        assert orders.transition(
            pending["order_id"], expected_version=2, status="UNKNOWN", evidence={}
        )
    elif defect == "unknown_position":
        case.venue.rows["/fapi/v3/positionRisk"].append(
            {"symbol": "SOLUSDT", "positionSide": "BOTH", "positionAmt": "1"}
        )
    elif defect == "target_ordinary_order":
        case.venue.rows["/fapi/v1/openOrders"] = [{"symbol": "BTCUSDT", "orderId": 222}]
    elif defect == "target_pending_close":
        pending_close(case.runtime, case.btc["intent_id"], exchange_id="902")
    elif defect == "triggered_stop":
        case.venue.parent["algoStatus"] = "TRIGGERING"
        case.venue.rows["/fapi/v1/openAlgoOrders"][0]["algoStatus"] = "TRIGGERING"
    elif defect == "quantity_mismatch":
        case.venue.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = "1.2"
    assert_no_new_orders(case, case.stage.run_once())


def test_stale_inventory_is_not_a_reduce_only_authorization(database, monkeypatch):
    case = build_case(database)
    original = case.stage.audit.inventory.collect

    def stale(*args, **kwargs):
        result = original(*args, **kwargs)
        return {
            **result,
            "started_at_ms": case.clock[0] - 10000,
            "finished_at_ms": case.clock[0] - 10000,
        }

    monkeypatch.setattr(case.stage.audit.inventory, "collect", stale)
    assert_no_new_orders(case, case.stage.run_once())


def test_local_facts_changing_during_preflight_cannot_authorize_exit(
    database, monkeypatch
):
    case = build_case(database)
    original = case.stage.audit.facts
    calls = [0]

    def changing():
        result = original()
        calls[0] += 1
        result["episodes"][0]["revision"] += calls[0]
        return result

    monkeypatch.setattr(case.stage.audit, "facts", changing)
    assert_no_new_orders(case, case.stage.run_once())


@pytest.mark.parametrize("change", ["clock", "triggered_stop", "policy", "ledger_fill"])
def test_change_after_prepare_is_revalidated_before_actual_submit(database, change):
    case = build_case(database)
    checks = []

    def changed(request):
        assert request["leg"] == "CLOSE" and request["symbol"] == "BTCUSDT"
        checks.append(request["order_id"])
        if change == "clock":
            case.clock[0] += 10000
        elif change == "triggered_stop":
            case.venue.parent["algoStatus"] = "TRIGGERING"
            case.venue.rows["/fapi/v1/openAlgoOrders"][0]["algoStatus"] = "TRIGGERING"
        elif change == "policy":
            policy = PolicyStore(database, SCOPE)
            selected = policy.read()
            # Normal policy.patch correctly waits on the account lock held by
            # this dispatch. Inject a noncooperating administrative CAS here to
            # exercise the final version fence without self-deadlocking QA.
            assert (
                policy.store.change(
                    policy.key,
                    expected_version=selected.version,
                    request_key="qa-policy-revoked-before-submit",
                    payload={
                        "values": {
                            **selected.values,
                            "exit.symbol_isolation_enabled": False,
                        },
                        "reason": "QA noncooperating policy change",
                    },
                    reason="RUNTIME_POLICY_ACTIVATED",
                ).code
                == "APPLIED"
            )
        else:
            # A concurrent authoritative fill on the quarantined symbol changes
            # the frozen *global* facts even though BTC's quantity is unchanged.
            case.runtime.data.ledger.record_fill(
                order_id=case.unresolved,
                exchange_fill_id="qa-concurrent-eth-fill",
                quantity="0.1",
                price="100",
                fee="0.004",
                fee_currency="USDT",
                occurred_at_ms=case.clock[0],
                evidence={"source": "qa concurrently observed venue fill"},
            )
        return True

    case.runtime.risk_check = changed
    result = case.stage.run_once()
    assert checks, result
    assert_no_new_orders(case, result)
    assert result["exits"][case.btc["intent_id"]]["outcome"] == "REJECTED"
    assert case.runtime.execution.snapshot(checks[0])["status"] == "REJECTED"


def test_route_epoch_revocation_still_fences_isolated_reduce_only_write(
    database, routes, monkeypatch
):
    case = build_case(database)
    store, console = routes
    registry = add(console)["registry_id"]
    token = str(uuid4())
    args = {
        "tenant_id": console.tenant,
        "environment": "SANDBOX",
        "registry_id": registry,
        "binding_version": 1,
        "epoch": 1,
        "worker_token": token,
    }
    with console.connect() as conn:
        conn.execute(
            "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,target_registry,"
            "binding_version,phase,worker_token) VALUES (%s,'SANDBOX',1,%s,1,'ACTIVE',%s)",
            (console.tenant, registry, token),
        )
    guarded = GuardedSignedRequest(
        case.venue,
        account_id=SCOPE.account_id,
        environment=SCOPE.environment,
        guard=lambda **options: store.submit_guard(**args, **options),
    )
    adapter = BinanceFutures(
        guarded, account_id=SCOPE.account_id, environment=SCOPE.environment
    )
    case.runtime.execution.submit, case.runtime.execution.query = (
        adapter.submit,
        adapter.query,
    )
    dispatch = case.runtime.execution.dispatch

    def revoke(order_id, **options):
        with console.connect() as conn:
            conn.execute(
                "UPDATE v2_execution_routes SET epoch=epoch+1 WHERE tenant_id=%s AND environment='SANDBOX'",
                (console.tenant,),
            )
        return dispatch(order_id, **options)

    monkeypatch.setattr(case.runtime.execution, "dispatch", revoke)
    result = case.stage.run_once()
    assert_no_new_orders(case, result)
    assert result["exits"][case.btc["intent_id"]]["outcome"] == "REJECTED"
