from dataclasses import replace
from decimal import Decimal

import pytest
from test_v2_quant_replay import scenario

from v2_core.evidence import canonical
from v2_core.exit_comparison import ExitPolicy, _episode, compare_exits, exhaustion
from v2_core.quant_indicators import Bar
from v2_core.quant_registry import parameters
from v2_core.quant_replay import ReplayPolicy, replay


@pytest.mark.parametrize(
    "changes",
    [
        {"volume_bars": True},
        {"volume_bars": 201},
        {"partial_fraction": "1"},
        {"min_profit_r": "NaN"},
        {"tight_atr": 1.0},
        {"max_directional_close": "0"},
        {"min_rejection_wick": "2"},
    ],
)
def test_exit_policy_rejects_bad_values(changes):
    with pytest.raises(ValueError):
        ExitPolicy(**changes)


@pytest.mark.parametrize("variant", ["timeout", "target", "ambiguous", "gap", "open"])
def test_episode_baseline_matches_original_replay(variant):
    bars = scenario(4)
    policy = ReplayPolicy(max_hold_bars=2 if variant == "timeout" else 24)
    if variant in ("target", "ambiguous", "gap"):
        bars[-2] = Bar(
            bars[-2].t,
            *{
                "target": ("102", "110", "101.9", "108", "300"),
                "ambiguous": ("102", "110", "90", "108", "300"),
                "gap": ("90", "92", "89", "91", "300"),
            }[variant],
        )
    baseline = replay(bars, timeframe_minutes=5, policy=policy)
    report = compare_exits(bars, policy=policy)
    canonical(report)
    assert len(report["pairs"]) == len(baseline["entries"])
    closed = [
        p["baseline"] for p in report["pairs"] if p["baseline"]["status"] == "CLOSED"
    ]
    assert len(closed) == len(baseline["trades"])
    for result, trade in zip(closed, baseline["trades"], strict=True):
        assert result["closed_bar_at_ms"] == trade["closed_bar_at_ms"]
        assert abs(
            Decimal(result["net_realized"]) - Decimal(trade["net_pnl"])
        ) < Decimal("1e-14")
        assert result["fills"][-1]["reason"] == trade["reason"]
    for pair in report["pairs"]:
        assert (
            pair["baseline"]["entry"]
            == pair["tighten"]["entry"]
            == pair["partial"]["entry"]
        )


def fixture(side="BUY", quantity="2"):
    bars = [Bar(i * 300000, "102", "103", "101", "102", "100") for i in range(21)]
    bars[-1] = Bar(bars[-1].t, "104", "108", "103", "104", "300")
    bars.append(Bar(21 * 300000, "104", "104.5", "102.5", "103", "100"))
    entry = {
        "side": side,
        "opened_at_ms": 20 * 300000,
        "signal_at_ms": 20 * 300000,
        "entry_price": "100",
        "initial_stop": "98",
        "target": "120",
        "quantity": quantity,
    }
    if side == "SELL":
        bars = [Bar(b.t, 200 - b.o, 200 - b.l, 200 - b.h, 200 - b.c, b.v) for b in bars]
        entry.update(initial_stop="102", target="80")
    frames = [{"current": {"atr": "3"}} for _ in bars]
    return bars, frames, entry


def episode(
    side="BUY", variant="partial", quantity="2", bars_override=None, policy=None
):
    bars, frames, entry = fixture(side, quantity)
    if bars_override:
        bars = bars_override(bars)
    return _episode(
        bars,
        frames,
        entry,
        variant=variant,
        policy=policy or ReplayPolicy(),
        exits=ExitPolicy(),
        params=parameters(),
        width=300000,
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_exhaustion_partial_causal_accounting_and_short_symmetry(side):
    bars, _, entry = fixture(side)
    assert exhaustion(bars[:-1], entry, ExitPolicy())
    assert not exhaustion([replace(b, v=0) for b in bars[:-1]], entry, ExitPolicy())
    result = episode(side)
    assert result["status"] == "CENSORED_OPEN"
    assert result["fills"][0]["bar_at_ms"] == 21 * 300000
    assert result["fills"][0]["quantity"] == "1"
    assert result["remaining_quantity"] == "1"
    opening_fee = Decimal(2) * 100 * Decimal(".0005")
    assert Decimal(result["fees"]) == opening_fee + sum(
        Decimal(f["fee"]) for f in result["fills"]
    )
    assert Decimal(result["net_realized"]) == Decimal(
        result["gross_realized"]
    ) - Decimal(result["fees"])
    tight = episode(side, "tighten")
    assert tight["observations"][0]["at_ms"] == 21 * 300000
    assert not tight["fills"]  # tightening cannot retroactively stop the signal bar


def test_gap_stop_precedes_partial_and_costs_are_not_lost():
    def gap(bars):
        return bars[:-1] + [Bar(bars[-1].t, "90", "95", "89", "94", "100")]

    result = episode(bars_override=gap)
    assert result["status"] == "CLOSED"
    assert len(result["fills"]) == 1 and result["fills"][0]["quantity"] == "2"
    assert Decimal(result["fills"][0]["price"]) < 90


def test_partial_respects_quantity_notional_and_no_repeat():
    result = episode(quantity="0.05")
    assert not result["fills"]
    assert result["observations"][-1]["event"] == "PARTIAL_BELOW_FILTERS"


def test_partial_notional_checks_final_tick_rounded_price():
    result = episode(policy=ReplayPolicy(price_tick="1", minimum_notional="103.9"))
    # 104 * (1-.0005)=103.948 passes 103.9 before rounding, but fill=103 fails.
    assert not result["fills"]
    assert result["observations"][-1]["event"] == "PARTIAL_BELOW_FILTERS"


def test_stop_ambiguous_after_partial_and_remaining_quantity_reconciles():
    def ambiguous(bars):
        return bars[:-1] + [Bar(bars[-1].t, "104", "125", "97", "104", "100")]

    result = episode(bars_override=ambiguous)
    assert result["status"] == "CLOSED"
    assert [f["reason"] for f in result["fills"]] == [
        "EXHAUSTION_PARTIAL_NEXT_OPEN",
        "STOP_FIRST_AMBIGUOUS_BAR",
    ]
    assert sum(Decimal(f["quantity"]) for f in result["fills"]) == 2


def test_frozen_entry_end_prefix_invariance_and_gap_rejection():
    bars = scenario(4)
    start = 86400000
    no_entry = replay(bars, timeframe_minutes=5, entry_end_ms=start)
    assert not no_entry["entries"]
    before = compare_exits(bars[:-1], entry_start_ms=start, entry_end_ms=start + 300000)
    bars[-1] = Bar(bars[-1].t, "102", "120", "90", "105", "100")
    after = compare_exits(bars, entry_start_ms=start, entry_end_ms=start + 300000)
    assert before["cohort_digest"] == after["cohort_digest"]
    for variant in ("baseline", "tighten", "partial"):
        old, new = before["pairs"][0][variant], after["pairs"][0][variant]
        assert old["observations"] == [
            o for o in new["observations"] if o["at_ms"] <= bars[-1].t
        ]
    with pytest.raises(ValueError, match="GAP"):
        compare_exits(bars[:-2] + bars[-1:])


def test_partial_only_once_and_timeout_closes_remaining():
    bars, frames, entry = fixture()
    bars += [replace(bars[-2], t=22 * 300000), replace(bars[-1], t=23 * 300000)]
    frames += frames[-2:]
    result = _episode(
        bars,
        frames,
        entry,
        variant="partial",
        policy=ReplayPolicy(max_hold_bars=3),
        exits=ExitPolicy(),
        params=parameters(),
        width=300000,
    )
    assert [f["reason"] for f in result["fills"]] == [
        "EXHAUSTION_PARTIAL_NEXT_OPEN",
        "TIME_EXIT",
    ]
    assert result["status"] == "CLOSED"
