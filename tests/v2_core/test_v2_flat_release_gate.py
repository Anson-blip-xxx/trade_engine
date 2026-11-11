"""Release proof contract: fake GET transport and fake read-only DB only."""

import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts.v2_flat_release_gate import FlatGateError, verify_flat
from v2_core.account_inventory import AccountInventory
from v2_core.account_risk import AccountScope


@pytest.fixture
def case():
    scope = AccountScope("BINANCE", "qa-private-account", "SANDBOX", "FUTURES")
    baseline = {
        "tenant": str(uuid4()),
        "registry": str(uuid4()),
        "binding": 3,
        "epoch": 17,
        "request_epoch": 17,
        "pending": False,
        "exchange": "BINANCE",
        "account": scope.account_id,
        "product": "FUTURES",
    }
    route = (
        baseline["registry"],
        3,
        17,
        str(uuid4()),
        "ACTIVE",
        "BINANCE",
        scope.account_id,
        "SANDBOX",
        "FUTURES",
        3,
        False,
        17,
    )
    state = {
        "route": route,
        "after_route": None,
        "counts": (0, 0, 0),
        "after_counts": None,
        "connections": 0,
        "sql": [],
        "gets": [],
        "now": 1000,
        "db_failure": None,
        "venue_failure": None,
        "positions_reads": [],
        "require_role": False,
    }
    raw = {
        "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
        "/fapi/v1/multiAssetsMargin": {"multiAssetsMargin": False},
        "/fapi/v3/positionRisk": [],
        "/fapi/v3/account": {
            "totalWalletBalance": "1000",
            "availableBalance": "1000",
            "totalMarginBalance": "1000",
            "positions": [],
        },
        "/fapi/v1/openOrders": [],
        "/fapi/v1/openAlgoOrders": [],
    }

    class Request:
        account_id, environment = scope.account_id, scope.environment

        def __call__(self, method, path, params):
            assert method == "GET" and params == {}
            state["gets"].append(path)
            if state["venue_failure"]:
                raise state["venue_failure"]
            if path == "/fapi/v3/positionRisk" and state["positions_reads"]:
                return deepcopy(state["positions_reads"].pop(0))
            return deepcopy(raw[path])

    request = Request()
    inventory = AccountInventory(request, scope=scope, clock_ms=lambda: state["now"])

    @contextmanager
    def connect():
        state["connections"] += 1
        ordinal = state["connections"]
        transaction_readonly = [False]
        active_role = [None]

        def execute(sql, args=None):
            state["sql"].append((sql, args))
            assert sql.startswith(("SELECT", "SET", "RESET ROLE")), (
                "DB writes are forbidden"
            )
            if sql.startswith("SET TRANSACTION"):
                assert (
                    sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                transaction_readonly[0] = True
            else:
                assert transaction_readonly[0]
            if sql.startswith("SET LOCAL ROLE"):
                assert sql == "SET LOCAL ROLE trade_v2_execution_operator"
                active_role[0] = "trade_v2_execution_operator"
            elif sql == "RESET ROLE":
                active_role[0] = None
            if state["db_failure"] == "query" and "FROM v2_execution_routes" in sql:
                raise ConnectionError("SECRET_DSN=hidden")
            if "FROM v2_execution_routes" in sql:
                if state["require_role"]:
                    assert active_role[0] == "trade_v2_execution_operator"
                assert args == (baseline["tenant"], "SANDBOX")
                current = (state["after_route"] if ordinal == 2 else None) or state[
                    "route"
                ]
                return SimpleNamespace(fetchall=lambda: [current] if current else [])
            if "SELECT count(*) FROM v2_orders" in sql:
                assert active_role[0] is None, "ledger cannot use routing role"
                assert args == ("BINANCE", scope.account_id, "SANDBOX", "FUTURES") * 3
                counts = state["after_counts"] if ordinal == 2 else None
                return SimpleNamespace(
                    fetchone=lambda: counts if counts is not None else state["counts"]
                )
            return SimpleNamespace()

        yield SimpleNamespace(execute=execute)
        if state["db_failure"] == "commit":
            raise ConnectionError("SECRET_DSN=hidden")

    def run(**overrides):
        return verify_flat(
            **{
                "connect": connect,
                "inventory": inventory,
                "scope": scope,
                "baseline": baseline,
                "clock_ms": lambda: state["now"],
                **overrides,
            }
        )

    return SimpleNamespace(
        state=state,
        raw=raw,
        inventory=inventory,
        scope=scope,
        baseline=baseline,
        request=request,
        connect=connect,
        run=run,
    )


@pytest.mark.parametrize("phase", ["ACTIVE", "DRAINING"])
def test_flat_proof_is_readonly_sanitized_and_not_an_execution_permit(
    case, phase, capsys
):
    route = list(case.state["route"])
    route[4] = phase
    case.state["route"] = tuple(route)
    result = case.run()
    assert result["status"] == "FLAT_VERIFIED"
    assert result["same_binding"] is True
    assert result["route_phase"] == phase
    assert result["route_epoch"] == 17
    assert result["execution_authorized"] is result["atomic_snapshot"] is False
    assert not any(result["venue_counts"].values())
    assert not any(result["local_counts"].values())
    assert case.state["connections"] == 2
    assert len(case.state["gets"]) == 7
    assert case.state["gets"].count("/fapi/v3/positionRisk") == 2
    assert case.scope.account_id not in json.dumps(result)
    assert case.baseline["tenant"] not in json.dumps(result)
    assert case.baseline["registry"] not in json.dumps(result)
    assert capsys.readouterr().out == ""


def _position(qty="1"):
    return {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": qty}


@pytest.mark.parametrize(
    "path", ["/fapi/v3/positionRisk", "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"]
)
def test_live_position_or_any_order_blocks_even_if_locally_flat(case, path):
    case.raw[path] = (
        [_position()]
        if "positionRisk" in path
        else [{"symbol": "BTCUSDT", "algoId" if "Algo" in path else "orderId": 123}]
    )
    with pytest.raises(FlatGateError, match="INVENTORY_NOT_CLEAR"):
        case.run()


def test_account_endpoint_positions_independently_block_false_flat_position_risk(case):
    case.raw["/fapi/v3/account"]["positions"] = [_position("-0.00001")]
    with pytest.raises(FlatGateError, match="VENUE_NOT_FLAT"):
        case.run()


def test_position_bracket_detects_race(case):
    case.state["positions_reads"] = [[], [_position()]]
    with pytest.raises(FlatGateError, match="INVENTORY_NOT_CLEAR"):
        case.run()


@pytest.mark.parametrize("quantity", ["0", "0.000000", "0E-8", "-0.0"])
def test_zero_uses_decimal_not_string_comparison(case, quantity):
    case.raw["/fapi/v3/account"]["positions"] = [_position(quantity)]
    case.raw["/fapi/v3/positionRisk"] = [_position(quantity)]
    assert case.run()["status"] == "FLAT_VERIFIED"


@pytest.mark.parametrize("counts", [(1, 0, 0), (0, 1, 0), (0, 0, 1)])
@pytest.mark.parametrize("after", [False, True])
def test_any_local_pending_work_blocks_before_or_after_venue(case, counts, after):
    case.state["after_counts" if after else "counts"] = counts
    with pytest.raises(FlatGateError, match="LOCAL_RELEASE_WORK_PENDING"):
        case.run()
    assert bool(case.state["gets"]) is after


@pytest.mark.parametrize(
    "column,value,code",
    [
        (0, str(uuid4()), "ROUTE_BINDING_CHANGED"),
        (1, 4, "ROUTE_BINDING_CHANGED"),
        (2, 18, "ROUTE_BINDING_CHANGED"),
        (3, str(uuid4()), "ROUTE_WORKER_CHANGED"),
        (4, "STOPPED", "ROUTE_PHASE_UNSAFE"),
        (6, "other-account", "ROUTE_BINDING_CHANGED"),
        (7, "LIVE", "ROUTE_BINDING_CHANGED"),
        (9, 4, "ROUTE_BINDING_CHANGED"),
        (10, True, "ROUTE_BINDING_CHANGED"),
        (11, 18, "CONCURRENT_ROUTE_REQUEST"),
        (11, 16, "CONCURRENT_ROUTE_REQUEST"),
        (11, 0, "CONCURRENT_ROUTE_REQUEST"),
    ],
)
def test_scope_rotation_route_change_or_pending_request_during_inventory_blocks(
    case, column, value, code
):
    changed = list(case.state["route"])
    changed[column] = value
    case.state["after_route"] = tuple(changed)
    with pytest.raises(FlatGateError, match=code):
        case.run()


def test_active_to_draining_same_worker_is_valid_during_probe(case):
    changed = list(case.state["route"])
    changed[4] = "DRAINING"
    case.state["after_route"] = tuple(changed)
    assert case.run()["route_phase"] == "DRAINING"


@pytest.mark.parametrize("failure", ["query", "commit"])
def test_database_failures_are_redacted_and_never_report_flat(case, failure, capsys):
    case.state["db_failure"] = failure
    with pytest.raises(FlatGateError, match="^DATABASE_CHECK_UNAVAILABLE$") as caught:
        case.run()
    assert "SECRET" not in str(caught.value)
    assert not case.state["gets"]
    assert capsys.readouterr().out == ""


def test_venue_failure_is_redacted(case):
    case.state["venue_failure"] = RuntimeError("APIKEY=SECRET")
    with pytest.raises(FlatGateError, match="^INVENTORY_NOT_CLEAR$"):
        case.run()


def test_exclusions_are_rejected_before_any_io(case):
    case.inventory.excluded_position_symbols = ("BTCUSDT",)
    with pytest.raises(FlatGateError, match="POSITION_EXCLUSIONS_FORBIDDEN"):
        case.run()
    assert case.state["connections"] == 0 and not case.state["gets"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("pending", True),
        ("request_epoch", 18),
        ("binding", True),
        ("epoch", 0),
        ("tenant", "private-invalid-id"),
    ],
)
def test_invalid_capture_never_reads_venue_or_db(case, field, value):
    case.baseline[field] = value
    with pytest.raises(FlatGateError, match="INVALID_ROUTE_BASELINE"):
        case.run()
    assert case.state["connections"] == 0 and not case.state["gets"]


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda o: o["responses"].pop("positions_after"), "INCOMPLETE_INVENTORY_PROOF"),
        (lambda o: o["response_digests"].clear(), "INVENTORY_DIGEST_MISMATCH"),
        (
            lambda o: o.update(excluded_position_symbols=["BTCUSDT"]),
            "POSITION_EXCLUSIONS_FORBIDDEN",
        ),
        (lambda o: o.update(execution_authorized=True), "INVENTORY_NOT_CLEAR"),
        (lambda o: o.update(started_at_ms=999), "INVENTORY_TIME_UNVERIFIED"),
        (lambda o: o.update(observation_id=str(uuid4())), "INVENTORY_SCOPE_MISMATCH"),
        (
            lambda o: o["summary"].update(positions=[_position()]),
            "INVENTORY_SUMMARY_MISMATCH",
        ),
    ],
)
def test_incomplete_or_tampered_observation_cannot_pass(
    case, monkeypatch, mutation, code
):
    collect = case.inventory.collect

    def changed(identifier):
        result = collect(identifier)
        mutation(result)
        return result

    monkeypatch.setattr(case.inventory, "collect", changed)
    with pytest.raises(FlatGateError, match=code):
        case.run()


def test_missing_account_position_array_is_not_assumed_empty(case):
    del case.raw["/fapi/v3/account"]["positions"]
    with pytest.raises(FlatGateError, match="FLAT_CHECK_UNAVAILABLE"):
        case.run()


def test_final_database_check_cannot_make_old_venue_proof_fresh(case):
    @contextmanager
    def slow_after():
        with case.connect() as conn:
            yield conn
        if case.state["connections"] == 2:
            case.state["now"] = 6001

    with pytest.raises(FlatGateError, match="FLAT_EVIDENCE_STALE"):
        case.run(connect=slow_after)


def test_account_balance_or_raw_response_does_not_enter_proof(case):
    case.raw["/fapi/v3/account"]["sensitive_account_identifier"] = (
        "PRIVATE-ACCOUNT-NUMBER"
    )
    encoded = json.dumps(case.run())
    assert "PRIVATE-ACCOUNT-NUMBER" not in encoded
    assert "balance" not in encoded


def test_combined_account_config_still_requires_every_inventory_read(case):
    case.inventory.combined_account_config = True
    case.raw["/fapi/v1/accountConfig"] = {
        "dualSidePosition": False,
        "multiAssetsMargin": False,
    }
    assert case.run()["status"] == "FLAT_VERIFIED"
    assert len(case.state["gets"]) == 6
    assert "/fapi/v1/openAlgoOrders" in case.state["gets"]
    assert case.state["gets"][0] == case.state["gets"][-1] == "/fapi/v3/positionRisk"


def test_transport_scope_cannot_be_swapped_after_inventory_construction(case):
    case.request.environment = "LIVE"
    with pytest.raises(FlatGateError, match="INVENTORY_TRANSPORT_SCOPE_MISMATCH"):
        case.run()
    assert case.state["connections"] == 0 and not case.state["gets"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_age_ms", True),
        ("max_age_ms", 60001),
        ("query_timeout_ms", 0),
        ("query_timeout_ms", 10001),
    ],
)
def test_timing_limits_are_explicit_and_bounded_before_io(case, field, value):
    with pytest.raises(FlatGateError, match="BOUNDED_GATE_TIMING_REQUIRED"):
        case.run(**{field: value})
    assert case.state["connections"] == 0 and not case.state["gets"]


def test_sql_covers_orphan_intents_without_misclassifying_settled_partial_fills(case):
    case.run()
    statements = [
        sql for sql, _ in case.state["sql"] if "SELECT count(*) FROM v2_orders" in sql
    ]
    assert len(statements) == 2
    sql = statements[0]
    assert "o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED')" in sql
    assert "e.status NOT IN ('SETTLED','ABORTED')" in sql
    assert "i.status NOT IN ('REJECTED','EXPIRED','CANCELLED')" in sql
    assert (
        "NOT EXISTS(SELECT 1 FROM v2_episodes e WHERE e.episode_id=i.intent_id)" in sql
    )
    assert "FOR UPDATE" not in sql and "FOR SHARE" not in sql


def test_route_role_is_scoped_to_route_select_and_reset_before_ledger(case):
    case.state["require_role"] = True
    assert (
        case.run(route_role="trade_v2_execution_operator")["status"] == "FLAT_VERIFIED"
    )
    statements = [sql for sql, _ in case.state["sql"]]
    assert statements.count("SET LOCAL ROLE trade_v2_execution_operator") == 2
    assert statements.count("RESET ROLE") == 2
    for index, sql in enumerate(statements):
        if "FROM v2_execution_routes" in sql:
            assert statements[index - 1] == "SET LOCAL ROLE trade_v2_execution_operator"
            assert statements[index + 1] == "RESET ROLE"
    assert (
        statements.count("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        == 2
    )


@pytest.mark.parametrize(
    "role",
    [
        "postgres",
        "trade_v2_vault_operator",
        "ubuntu",
        "",
        "trade_v2_execution_operator; DELETE FROM v2_orders",
        1,
    ],
)
def test_arbitrary_roles_are_rejected_before_any_io(case, role):
    with pytest.raises(FlatGateError, match="FIXED_ROUTE_ROLE_REQUIRED"):
        case.run(route_role=role)
    assert case.state["connections"] == 0 and not case.state["gets"]
