from copy import deepcopy
from decimal import Decimal

import pytest
from test_v2_quant_indicators import frame, minutes
from test_v2_quant_research import Archive

from services.v2_quant_exit import QuantExitMarket


def case(side="BUY"):
    rows = minutes(breakout=True)
    archive = Archive(rows)
    proof = {"environment": "SANDBOX", "symbol": "BTCUSDT", "frame": frame(rows)}
    facts = {
        "entry_proof": proof,
        "symbol": "BTCUSDT",
        "side": side,
        "opened_at_ms": 86400000,
        "entry_price": "102",
        "hard_stop": "100" if side == "BUY" else "104",
        "mark_price": "102",
        "previous": None,
        "now": 86401000,
    }
    return QuantExitMarket(archive), facts


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_trail_monotonic_restart_and_frozen_multiplier(side):
    port, facts = case(side)
    before = deepcopy(facts)
    first = port(**facts)
    assert first["status"] == "WAIT"
    extreme = "105" if side == "BUY" else "99"
    second = port(
        **{**facts, "previous": first, "mark_price": extreme, "now": 86402000}
    )
    assert second["status"] == "WAIT"
    # Reconstruct with the same archived data: PG payload is sufficient state.
    restarted = QuantExitMarket(port.archive)
    third = restarted(
        **{**facts, "previous": second, "mark_price": "102", "now": 86403000}
    )
    assert third["status"] == "CLOSE"
    assert (
        Decimal(third["trailing_stop"]) >= Decimal(second["trailing_stop"])
        if side == "BUY"
        else Decimal(third["trailing_stop"]) <= Decimal(second["trailing_stop"])
    )
    assert facts == before
    assert third["mode"] == "SOFTWARE_REDUCE_ONLY_WITH_NATIVE_HARD_STOP"


def test_pre_entry_wick_never_becomes_post_entry_high_water():
    port, facts = case()
    # Historical high affects ATR, but cannot falsely tighten a new position.
    port.archive.rows[1]["h"] = "140"
    result = port(**facts)
    assert result["high_water"] == "102"
    assert result["trailing_stop"] == facts["hard_stop"]


@pytest.mark.parametrize("defect", ["history", "future", "scope", "entry"])
def test_invalid_exit_input_never_fabricates_atr(defect):
    port, facts = case()
    first = port(**facts)
    if defect == "history":
        port.archive.rows.pop(0)
    elif defect == "future":
        facts["previous"] = {**first, "observed_at_ms": facts["now"] + 1}
    elif defect == "scope":
        facts["symbol"] = "ETHUSDT"
    else:
        facts["previous"] = {**first, "entry_proof_digest": "other"}
    with pytest.raises(ValueError):
        port(**facts)
