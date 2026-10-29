"""Pure isolation proof: no database, TCP transport or execution permission."""

from copy import deepcopy
from dataclasses import asdict, replace
from uuid import UUID

import pytest

from v2_core.account_coverage import coverage_from_facts
from v2_core.account_inventory import AccountInventory
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.protection import ProtectionSpec
from v2_core.state import StateKey
from v2_core.symbol_reduction import symbol_reduction_proof

SCOPE = AccountScope("BINANCE", "symbol-reduction-qa", "SANDBOX", "FUTURES")
TARGET, OTHER = "NILUSDT", "BRUSDT"
EPISODE, OTHER_EPISODE = str(UUID(int=1)), str(UUID(int=2))


def protection(episode, symbol, identity, *, side="BUY"):
    spec = ProtectionSpec(
        episode, symbol, "SELL" if side == "BUY" else "BUY", "STOP_MARKET", "90"
    )
    key = StateKey(
        **asdict(SCOPE),
        namespace="testnet-protection-v1",
        key=canonical(
            {"episode": spec.episode, "symbol": spec.symbol, "kind": spec.kind}
        ),
    )
    live = {
        "symbol": symbol,
        "algoId": identity,
        "algoStatus": "NEW",
        "clientAlgoId": "v2p-" + digest(key.identity)[:28],
        "side": spec.side,
        "positionSide": "BOTH",
        "orderType": spec.kind,
        "algoType": "CONDITIONAL",
        "closePosition": True,
        "workingType": "MARK_PRICE",
        "priceProtect": False,
        "triggerPrice": "90",
    }
    return {
        "state_id": key.identity,
        "payload": {
            "spec": asdict(spec),
            "status": "NEW",
            "observation": deepcopy(live),
        },
        "version": 1,
    }, live


class Account:
    account_id, environment = SCOPE.account_id, SCOPE.environment

    def __init__(self, *, side="BUY"):
        self.rows = {
            "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
            "/fapi/v1/multiAssetsMargin": {"multiAssetsMargin": False},
            "/fapi/v3/positionRisk": [
                {
                    "symbol": TARGET,
                    "positionSide": "BOTH",
                    "positionAmt": "1" if side == "BUY" else "-1",
                },
                {"symbol": OTHER, "positionSide": "BOTH", "positionAmt": "2"},
            ],
            "/fapi/v3/account": {
                "totalWalletBalance": "1000",
                "availableBalance": "900",
                "totalMarginBalance": "1000",
            },
            "/fapi/v1/openOrders": [],
            "/fapi/v1/openAlgoOrders": [],
        }

    def __call__(self, method, path, params):
        assert method == "GET" and params == {}
        return deepcopy(self.rows[path])


def case(*, pending=True, missing_stop=True, side="BUY", exclusions=()):
    target, target_live = protection(EPISODE, TARGET, 101, side=side)
    other, other_live = protection(OTHER_EPISODE, OTHER, 102)
    facts = {
        "episodes": [
            {
                "episode": EPISODE,
                "symbol": TARGET,
                "side": side,
                "remaining": "1",
                "revision": 1,
            },
            {
                "episode": OTHER_EPISODE,
                "symbol": OTHER,
                "side": "BUY",
                "remaining": "2",
                "revision": 1,
            },
        ],
        "orders": [
            {
                "order_id": str(UUID(int=11)),
                "symbol": TARGET,
                "exchange_order_id": "111",
                "status": "FILLED",
                "version": 3,
                "leg": "OPEN",
                "has_fills": True,
            },
            {
                "order_id": str(UUID(int=12)),
                "symbol": OTHER,
                "exchange_order_id": "112",
                "status": "FILLED",
                "version": 3,
                "leg": "OPEN",
                "has_fills": True,
            },
        ],
        "protections": [target, other],
    }
    if pending:
        facts["orders"].append(
            {
                "order_id": str(UUID(int=13)),
                "symbol": OTHER,
                "exchange_order_id": "113",
                "status": "UNKNOWN",
                "version": 4,
                "leg": "CLOSE",
                "has_fills": False,
            }
        )
    account = Account(side=side)
    account.rows["/fapi/v1/openAlgoOrders"] = [target_live]
    if missing_stop:
        other["payload"]["status"] = "TRIGGERING"
    else:
        account.rows["/fapi/v1/openAlgoOrders"].append(other_live)
    inventory = collect(account, exclusions=exclusions)
    return facts, inventory, account


def collect(account, *, exclusions=()):
    return AccountInventory(
        account,
        scope=SCOPE,
        clock_ms=lambda: 1000,
        excluded_position_symbols=exclusions,
    ).collect(str(UUID(int=100)))


def prove(facts, inventory, **changes):
    return symbol_reduction_proof(
        changes.pop("scope", SCOPE),
        facts,
        inventory,
        **{
            "episode": EPISODE,
            "symbol": TARGET,
            "side": "BUY",
            "remaining": "1",
            "now_ms": 1000,
            **changes,
        },
    )


def blocked(result):
    assert result["status"] == "SYMBOL_REDUCTION_BLOCKED", result
    assert result["blockers"]
    assert result["execution_authorized"] is False


@pytest.mark.parametrize(
    "pending,missing_stop", [(False, False), (True, False), (False, True), (True, True)]
)
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_independent_reduction_never_clears_account_gate(pending, missing_stop, side):
    facts, inventory, _ = case(pending=pending, missing_stop=missing_stop, side=side)
    original = deepcopy((facts, inventory))
    result = prove(facts, inventory, side=side)
    assert result["status"] == "SYMBOL_REDUCTION_READY", result
    assert result["blockers"] == []
    assert result["execution_authorized"] is False
    expected = sorted(
        (["LOCAL_ORDERS_NOT_FINAL"] if pending else [])
        + (["MISSING_CONFIRMED_STOP"] if missing_stop else [])
    )
    assert (
        result["account_blockers"]
        == coverage_from_facts(SCOPE, facts, inventory)
        == expected
    )
    assert result["quarantined_symbols"] == ([OTHER] if pending or missing_stop else [])
    assert result["facts_digest"] == digest(canonical(facts))
    assert result["inventory_digest"] == digest(canonical(inventory))
    assert (facts, inventory) == original


@pytest.mark.parametrize(
    "status", ["PREPARED", "SUBMITTING", "UNKNOWN", "ACKNOWLEDGED"]
)
def test_target_nonterminal_never_permits_second_close(status):
    facts, inventory, _ = case()
    facts["orders"].append(
        {
            **facts["orders"][-1],
            "order_id": str(UUID(int=14)),
            "symbol": TARGET,
            "status": status,
        }
    )
    result = prove(facts, inventory)
    blocked(result)
    assert result["blockers"] == ["TARGET_ORDERS_NOT_FINAL"]


def test_prepared_target_open_is_not_ignored_by_existing_global_exception():
    facts, inventory, _ = case(pending=False, missing_stop=False)
    facts["orders"].append(
        {
            **facts["orders"][0],
            "order_id": str(UUID(int=14)),
            "status": "PREPARED",
            "exchange_order_id": None,
            "has_fills": False,
        }
    )
    assert coverage_from_facts(SCOPE, facts, inventory) == []
    blocked(prove(facts, inventory))


@pytest.mark.parametrize("status", ["SUBMITTING", "UNKNOWN", "ACKNOWLEDGED"])
def test_only_bound_other_close_can_be_quarantined(status):
    facts, inventory, _ = case()
    facts["orders"][-1]["status"] = status
    assert prove(facts, inventory)["status"] == "SYMBOL_REDUCTION_READY"
    facts["orders"][-1]["exchange_order_id"] = None
    blocked(prove(facts, inventory))


@pytest.mark.parametrize(
    "change",
    [
        {"leg": "OPEN"},
        {"status": "PREPARED"},
        {"exchange_order_id": "bad-secret"},
        {"exchange_order_id": True},
        {"exchange_order_id": "0"},
        {"exchange_order_id": "0113"},
        {"symbol": "ETHUSDT"},
    ],
)
def test_other_pending_must_be_bound_close_with_unique_live_owner(change):
    facts, inventory, _ = case()
    facts["orders"][-1].update(change)
    blocked(prove(facts, inventory))


@pytest.mark.parametrize(
    "change",
    [
        {"episode": OTHER_EPISODE},
        {"symbol": OTHER},
        {"side": "SELL"},
        {"remaining": "1.001"},
        {"remaining": "0"},
    ],
)
def test_target_identity_and_remaining_are_exact(change):
    facts, inventory, _ = case()
    blocked(prove(facts, inventory, **change))


@pytest.mark.parametrize(
    "failure",
    [
        "foreign_position",
        "quantity",
        "direction",
        "double_owner",
        "overclosed",
        "no_target_orders",
        "foreign_order",
        "known_order",
        "foreign_algo",
        "hedge",
        "position_side",
        "multi_asset",
        "cash_zero",
    ],
)
def test_account_wide_ambiguity_is_never_hidden_by_projection(failure):
    facts, _, account = case()
    position = account.rows["/fapi/v3/positionRisk"][0]
    if failure == "foreign_position":
        account.rows["/fapi/v3/positionRisk"].append(
            {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "1"}
        )
    elif failure == "quantity":
        position["positionAmt"] = "1.1"
    elif failure == "direction":
        position["positionAmt"] = "-1"
    elif failure == "double_owner":
        facts["episodes"].append(
            {**facts["episodes"][1], "episode": str(UUID(int=3)), "remaining": "1"}
        )
        facts["episodes"][1]["remaining"] = "1"
    elif failure == "overclosed":
        facts["episodes"].append(
            {**facts["episodes"][1], "episode": str(UUID(int=3)), "remaining": "-1"}
        )
    elif failure == "no_target_orders":
        facts["orders"] = [row for row in facts["orders"] if row["symbol"] != TARGET]
    elif failure in {"foreign_order", "known_order"}:
        account.rows["/fapi/v1/openOrders"] = [
            {"symbol": OTHER, "orderId": 999 if failure == "foreign_order" else 113}
        ]
    elif failure == "foreign_algo":
        account.rows["/fapi/v1/openAlgoOrders"].append(
            {"symbol": "ETHUSDT", "algoId": 999}
        )
    elif failure == "hedge":
        account.rows["/fapi/v1/positionSide/dual"]["dualSidePosition"] = True
    elif failure == "position_side":
        position["positionSide"] = "LONG"
    elif failure == "multi_asset":
        account.rows["/fapi/v1/multiAssetsMargin"]["multiAssetsMargin"] = True
    elif failure == "cash_zero":
        account.rows["/fapi/v3/account"]["availableBalance"] = "0"
    blocked(prove(facts, collect(account)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("side", "BUY"),
        ("positionSide", "LONG"),
        ("orderType", "TAKE_PROFIT_MARKET"),
        ("algoStatus", "TRIGGERING"),
        ("closePosition", False),
        ("workingType", "CONTRACT_PRICE"),
        ("priceProtect", True),
        ("triggerPrice", "91"),
        ("clientAlgoId", "foreign"),
        ("algoId", 999),
    ],
)
def test_target_stop_must_match_every_native_term(field, value):
    facts, _, account = case()
    account.rows["/fapi/v1/openAlgoOrders"][0][field] = value
    blocked(prove(facts, collect(account)))


@pytest.mark.parametrize(
    "state", ["TRIGGERING", "TRIGGERED", "FINISHED", "UNKNOWN", "REJECTED", "NEW"]
)
def test_absent_target_stop_never_uses_other_symbol_quarantine(state):
    facts, _, account = case()
    facts["protections"][0]["payload"]["status"] = state
    account.rows["/fapi/v1/openAlgoOrders"] = []
    blocked(prove(facts, collect(account)))


def test_other_symbol_bad_stop_terms_are_not_an_isolatable_missing_stop():
    facts, _, account = case(missing_stop=False)
    account.rows["/fapi/v1/openAlgoOrders"][1]["triggerPrice"] = "91"
    result = prove(facts, collect(account))
    blocked(result)
    assert "PROTECTION_TERMS_MISMATCH" in result["account_blockers"]


@pytest.mark.parametrize(
    "failure",
    [
        "missing_route",
        "extra_route",
        "missing_digest",
        "wrong_digest",
        "raw_changed",
        "summary_changed",
        "failures",
        "stripped_blocker",
        "extra_blocker",
        "wrong_status",
        "permission",
        "future",
        "reversed",
        "moving",
        "wrong_scope",
        "wrong_exclusions",
    ],
)
def test_incomplete_or_self_inconsistent_inventory_never_permits_reduction(failure):
    facts, inventory, _ = case()
    if failure == "missing_route":
        inventory["responses"].pop("account")
    elif failure == "extra_route":
        inventory["responses"]["invented"] = {}
    elif failure == "missing_digest":
        inventory["response_digests"].pop("mode")
    elif failure == "wrong_digest":
        inventory["response_digests"]["mode"] = "wrong"
    elif failure == "raw_changed":
        inventory["responses"]["account"]["availableBalance"] = "1"
    elif failure == "summary_changed":
        inventory["summary"]["positions"][0]["quantity"] = "900"
    elif failure == "failures":
        inventory["failures"] = {"account": "ACCOUNT_READ_UNAVAILABLE"}
    elif failure == "stripped_blocker":
        inventory["blockers"] = []
    elif failure == "extra_blocker":
        inventory["blockers"].append("ACQUISITION_DEADLINE")
    elif failure == "wrong_status":
        inventory["status"] = "CLEAR_FOR_RECOVERY_CHECKS"
    elif failure == "permission":
        inventory["execution_authorized"] = True
    elif failure == "future":
        inventory["finished_at_ms"] = 1001
    elif failure == "reversed":
        inventory["started_at_ms"] = 1001
    elif failure == "moving":
        inventory["responses"]["positions_before"][0]["positionAmt"] = "2"
        inventory["response_digests"]["positions_before"] = digest(
            canonical({"response": inventory["responses"]["positions_before"]})
        )
    elif failure == "wrong_scope":
        inventory["account_scope"]["account_id"] = "other"
    elif failure == "wrong_exclusions":
        inventory["excluded_position_symbols"] = [OTHER]
    blocked(prove(facts, inventory))


@pytest.mark.parametrize(
    "change",
    [
        {"now_ms": 11000},
        {"now_ms": 999},
        {"now_ms": True},
        {"max_age_ms": 0},
        {"max_age_ms": True},
        {"max_age_ms": 60001},
        {"scope": replace(SCOPE, environment="LIVE")},
        {"scope": replace(SCOPE, account_id="other")},
    ],
)
def test_scope_and_freshness_fail_closed(change):
    facts, inventory, _ = case()
    blocked(prove(facts, inventory, **change))


def test_freshness_includes_acquisition_interval_not_only_finish_age():
    facts, inventory, _ = case()
    inventory["started_at_ms"] = 0
    inventory["finished_at_ms"] = 10000
    blocked(prove(facts, inventory, now_ms=10000))


def test_explicit_external_exclusion_is_retained_but_never_hides_orders():
    exclusions = ("EXTERNALUSDT",)
    facts, _, account = case()
    account.rows["/fapi/v3/positionRisk"].append(
        {"symbol": exclusions[0], "positionSide": "BOTH", "positionAmt": "3"}
    )
    result = prove(
        facts,
        collect(account, exclusions=exclusions),
        excluded_position_symbols=exclusions,
    )
    assert result["status"] == "SYMBOL_REDUCTION_READY", result
    account.rows["/fapi/v1/openOrders"] = [{"symbol": exclusions[0], "orderId": 888}]
    blocked(
        prove(
            facts,
            collect(account, exclusions=exclusions),
            excluded_position_symbols=exclusions,
        )
    )


def test_target_and_local_owner_cannot_be_excluded():
    facts, _, account = case()
    for target in (TARGET, OTHER):
        blocked(
            prove(
                facts,
                collect(account, exclusions=(target,)),
                excluded_position_symbols=(target,),
            )
        )


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate_episode",
        "duplicate_order",
        "duplicate_protection",
        "bad_revision",
        "bad_order_status",
        "bad_protection_scope",
        "secret",
    ],
)
def test_malformed_facts_fail_closed_without_echoing_input(failure):
    facts, inventory, _ = case()
    if failure.startswith("duplicate_"):
        field = {
            "duplicate_episode": "episodes",
            "duplicate_order": "orders",
            "duplicate_protection": "protections",
        }[failure]
        facts[field].append(deepcopy(facts[field][0]))
    elif failure == "bad_revision":
        facts["episodes"][0]["revision"] = True
    elif failure == "bad_order_status":
        facts["orders"][-1]["status"] = "private-secret-value"
    elif failure == "bad_protection_scope":
        facts["protections"][0]["state_id"] = "private-secret-value"
    elif failure == "secret":
        facts["privatekey"] = "private-secret-value"
    result = prove(facts, inventory)
    blocked(result)
    assert "private-secret-value" not in canonical(result)
