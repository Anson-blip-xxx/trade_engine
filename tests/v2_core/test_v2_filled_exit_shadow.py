from decimal import Decimal

import pytest

from v2_core.evidence import canonical
from v2_core.exit_comparison import ExitPolicy, exhaustion
from v2_core.filled_exit_shadow import (
    WIDTH,
    analyze,
    conditions,
    entry_from_facts,
    funnel,
)
from v2_core.quant_indicators import Bar
from v2_core.quant_registry import warmup


def facts(source="s3", side="BUY"):
    return {
        "source": source,
        "environment": "SANDBOX",
        "side": side,
        "maintenance": False,
        "opening_terminal": True,
        "stop_price": "98" if side == "BUY" else "102",
        "decided_ms": WIDTH * 100 - 2000,
        "evidence_ref": "evidence-hash",
        "fills": [
            {
                "identity": "fill-1",
                "leg": "OPEN",
                "t": WIDTH * 100 + 1000,
                "quantity": "2",
                "price": "100",
                "fee": "0.12",
                "fee_currency": "USDT",
            }
        ],
    }


def bars_for(f):
    entry, _ = entry_from_facts(f)
    safe = (entry["opened_at_ms"] + WIDTH - 1) // WIDTH * WIDTH
    return [
        Bar(t, "100", "100.2", "99.8", "100", "100")
        for t in range(safe - warmup() * WIDTH, safe + 25 * WIDTH, WIDTH)
    ]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_funnel_matches_frozen_exhaustion_boolean_and_prefix(side):
    f = facts(side=side)
    entry, _ = entry_from_facts(f)
    bars = bars_for(f)
    bar = Bar(bars[-1].t, "103", "109", "102", "104", "400")
    if side == "SELL":
        bar = Bar(bar.t, 200 - bar.o, 200 - bar.l, 200 - bar.h, 200 - bar.c, bar.v)
    bars[-1] = bar
    for i in range(20, len(bars)):
        result = conditions(bars[: i + 1], entry, ExitPolicy())
        assert result["trigger"] == exhaustion(bars[: i + 1], entry, ExitPolicy())
    original = funnel(bars[:-1], entry)
    later = funnel(bars, entry)
    assert original["observations"] == later["observations"][:-1]
    assert later["trigger_count"] == 1
    assert list(later["cumulative_pass"].values()) == sorted(
        later["cumulative_pass"].values(), reverse=True
    )
    canonical(later)


def test_unknown_volume_flat_bar_and_ordered_failure():
    entry, _ = entry_from_facts(facts())
    bars = [Bar(i * WIDTH, "100", "100", "100", "100", "0") for i in range(21)]
    r = conditions(bars, entry, ExitPolicy())
    assert not r["trigger"] and r["facts"]["relative_volume"] is None
    assert not conditions(bars[:2], entry, ExitPolicy())["ready"]


@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_real_fills_fees_and_excluded_opening_boundary(source, side):
    f = facts(source, side)
    bars = bars_for(f)
    # Entry bar has an impossible-to-order extreme: it must NOT stop the replay.
    bars[warmup() - 1] = Bar(bars[warmup() - 1].t, "100", "200", "1", "100", "100")
    result = analyze(f, bars)
    assert result["opening_boundary_excluded_ms"] == WIDTH - 1000
    assert (
        result["entry"]["quantity"] == "2" and result["entry"]["entry_price"] == "100"
    )
    for v in result["variants"].values():
        assert v["fills"][-1]["reason"] == "TIME_EXIT"
        assert v["fills"][-1]["bar_at_ms"] > f["fills"][0]["t"] + WIDTH
        assert Decimal(v["fees"]) == Decimal("0.12") + sum(
            Decimal(x["fee"]) for x in v["fills"]
        )
        assert not v["release_eligible"]
    assert result["actual_full_inventory_funnel"]["closed_bars_evaluated"] == 25
    assert not result["execution_authorized"]


def test_early_close_and_interleaving_not_fabricated_as_eligible():
    f = facts()
    f["fills"].append(
        {**f["fills"][0], "identity": "close", "leg": "CLOSE", "t": WIDTH * 100 + 2000}
    )
    with pytest.raises(ValueError, match="BEFORE_SAFE_WINDOW"):
        analyze(f, bars_for(f))
    f["fills"][-1]["t"] = f["fills"][0]["t"]
    with pytest.raises(ValueError, match="INTERLEAVED"):
        entry_from_facts(f)


def test_multifill_vwap_and_fee_sum_no_second_entry_slippage():
    f = facts()
    f["fills"].append(
        {**f["fills"][0], "identity": "fill-2", "t": WIDTH * 100 + 2000, "price": "101"}
    )
    entry, fee = entry_from_facts(f)
    assert (
        entry["entry_price"] == "100.5" and entry["quantity"] == "4" and fee == "0.24"
    )
    r = analyze(f, bars_for(f))
    assert r["entry"]["entry_price"] == "100.5"


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"source": "quant"}, "SCOPE"),
        ({"environment": "LIVE"}, "SCOPE"),
        ({"maintenance": True}, "MAINTENANCE"),
        ({"opening_terminal": False}, "NOT_COMPLETE"),
        ({"stop_price": "105"}, "LOSS_SIDE"),
    ],
)
def test_invalid_scope_and_evidence_fail_closed(patch, code):
    with pytest.raises(ValueError, match=code):
        entry_from_facts({**facts(), **patch})


def test_duplicate_currency_and_gap_fail_closed():
    f = facts()
    f["fills"] *= 2
    with pytest.raises(ValueError, match="DUPLICATE"):
        entry_from_facts(f)
    f = facts()
    f["fills"][0]["fee_currency"] = "BNB"
    with pytest.raises(ValueError, match="CURRENCY"):
        entry_from_facts(f)
    f = facts()
    bars = bars_for(f)
    with pytest.raises(ValueError, match="GAP"):
        analyze(f, bars[:80] + bars[81:])


def test_factual_funnel_stops_at_first_partial_close():
    f = facts()
    bars = bars_for(f)
    f["fills"].append(
        {
            **f["fills"][0],
            "identity": "close",
            "leg": "CLOSE",
            "t": WIDTH * 103 + 1000,
            "quantity": "1",
        }
    )
    r = analyze(f, bars)
    assert r["actual_full_inventory_funnel"]["closed_bars_evaluated"] == 2
    assert r["baseline_funnel"]["closed_bars_evaluated"] > 2
    assert r["facts"]["fills"] == f["fills"]
