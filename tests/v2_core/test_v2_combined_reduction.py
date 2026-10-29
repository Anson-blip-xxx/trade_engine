"""Combined fresh inventory must retain the strict reduce-only proof fences."""

from copy import deepcopy
from dataclasses import asdict
from uuid import UUID

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_symbol_exit_isolation import build_case
from test_v2_symbol_reduction import SCOPE, blocked, case, prove

from v2_core.account_inventory import AccountInventory
from v2_core.evidence import canonical, digest
from v2_core.state import BusinessState, StateKey

database = database_fixture


def combined_case():
    facts, _, reader = case()
    reader.rows["/fapi/v1/accountConfig"] = {
        "dualSidePosition": False,
        "multiAssetsMargin": False,
        "canTrade": True,
        "futureVenueField": "retained-not-interpreted",
    }
    inventory = AccountInventory(
        reader,
        scope=SCOPE,
        clock_ms=lambda: 1000,
        combined_account_config=True,
    ).collect(str(UUID(int=100)))
    return facts, inventory


def test_combined_inventory_allows_only_same_strict_reduction_proof():
    facts, inventory = combined_case()
    before = deepcopy(inventory)
    result = prove(facts, inventory)
    assert result["status"] == "SYMBOL_REDUCTION_READY", result
    assert result["execution_authorized"] is False
    assert inventory == before
    assert "mode" not in inventory["responses"]
    assert len(inventory["responses"]) == 6


@pytest.mark.parametrize(
    "mutation", ["missing", "source", "digest", "value", "raw", "mixed"]
)
def test_combined_projection_cannot_forge_account_mode_or_source(mutation):
    facts, inventory = combined_case()
    if mutation == "missing":
        inventory.pop("derived_responses")
    elif mutation == "source":
        inventory["derived_responses"]["mode"]["source_path"] = "/forged"
    elif mutation == "digest":
        inventory["derived_responses"]["mode"]["source_digest"] = "0" * 64
    elif mutation == "value":
        inventory["derived_responses"]["mode"]["response"]["dualSidePosition"] = True
    elif mutation == "raw":
        inventory["responses"]["account_config"]["dualSidePosition"] = True
        inventory["response_digests"]["account_config"] = digest(
            canonical({"response": inventory["responses"]["account_config"]})
        )
    else:
        inventory["responses"]["mode"] = {"dualSidePosition": False}
        inventory["response_digests"]["mode"] = digest(
            canonical({"response": inventory["responses"]["mode"]})
        )
    blocked(prove(facts, inventory))


@pytest.mark.parametrize("hedge", [False, True])
def test_real_pg_symbol_exit_uses_combined_fresh_read_not_cached_permission(
    database, hedge
):
    test = build_case(database)
    test.venue.rows["/fapi/v1/accountConfig"] = {
        "dualSidePosition": hedge,
        "multiAssetsMargin": False,
        "canTrade": True,
    }
    result = BusinessState(database).change(
        StateKey(
            **asdict(test.stage.scope),
            namespace="account-inventory-policy-v1",
            key="current",
        ),
        expected_version=0,
        request_key="qa-combined-inventory",
        payload={"combined_account_config": True},
        reason="QA",
    )
    assert result.code == "APPLIED"
    before = len(test.venue.calls)
    outcome = test.stage.run_once()
    assert outcome["status"] == "BLOCKED"  # Unrelated historical symbol stays blocked.
    new_reads = test.venue.calls[before:]
    # The inherited inventory fake logs paths; recovery logs method/path/params.
    paths = [call if isinstance(call, str) else call[1] for call in new_reads]
    assert "/fapi/v1/accountConfig" in paths
    if hedge:
        assert not test.venue.order_writes
    else:
        target = outcome["exits"][test.btc["intent_id"]]
        assert target["status"] == "DISPATCHED", outcome
        assert len(test.venue.order_writes) == 1
        assert test.venue.order_writes[0]["reduceOnly"] == "true"
        # Preliminary diagnostics and final under-lock dispatch are distinct reads.
        assert paths.count("/fapi/v1/accountConfig") >= 2
