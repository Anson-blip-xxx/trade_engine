"""Offline only: exact fill-aware accounting and intentionally incomplete labels."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from services.v2_trade_path_review import enrich_path_inputs
from v2_core.quant_indicators import Bar
from v2_core.trade_path import PathFill, analyze_path


def fill(key, t, leg, q, p):
    return PathFill(key, t, leg, str(q), str(p))


def bar(t, h=110, l=90, c=100):
    return Bar(t, str(c), str(h), str(l), str(c), "1")


def example():
    return [
        fill("a", 0, "OPEN", 2, 100),
        fill("b", 120000, "CLOSE", 1, 110),
        fill("c", 240000, "CLOSE", 1, 105),
    ], [
        bar(0, 115, 95, 110),
        bar(60000, 120, 105, 118),
        bar(120000, 140, 90, 130),
        bar(180000, 115, 100, 105),
    ]


def result(fills, bars, **kw):
    return analyze_path(
        fills, bars, side=kw.get("side", "BUY"), as_of_ms=kw.get("as_of_ms", 999999)
    )


def test_partial_close_marks_remaining_quantity_not_original_size():
    out = result(*example())
    assert out["status"] == "AVAILABLE_LOWER_BOUND"
    assert Decimal(out["mfe_gross_usdt_lower_bound"]) == 50  # NOT 80 from original qty
    assert Decimal(out["mae_gross_usdt_lower_bound"]) == 10
    assert Decimal(out["realized_gross_usdt"]) == 15
    assert Decimal(out["giveback_gross_usdt_lower_bound"]) == 35
    assert Decimal(out["observed_peak_capture_ratio"]) == Decimal("0.3")
    assert not out["execution_authorized"]


def test_short_side_mirror():
    fills, bars = example()
    mirrored_fills = [replace(f, price=200 - f.price) for f in fills]
    mirrored = [Bar(b.t, 200 - b.o, 200 - b.l, 200 - b.h, 200 - b.c, b.v) for b in bars]
    long, short = result(fills, bars), result(mirrored_fills, mirrored, side="SELL")
    for field in (
        "mfe_gross_usdt_lower_bound",
        "mae_gross_usdt_lower_bound",
        "realized_gross_usdt",
        "giveback_gross_usdt_lower_bound",
    ):
        assert long[field] == short[field]


def test_boundary_minutes_are_not_included():
    fills = [fill("a", 30000, "OPEN", 1, 100), fill("b", 150000, "CLOSE", 1, 105)]
    out = result(fills, [bar(60000)])
    assert out["expected_full_minutes"] == 1
    assert Decimal(out["mfe_gross_usdt_lower_bound"]) == 10
    with pytest.raises(ValueError, match="BAR_WINDOW"):
        result(fills, [bar(0, 900), bar(60000)])


def test_fill_changing_minute_extremes_cannot_invent_profit():
    fills = [
        fill("a", 0, "OPEN", 2, 100),
        fill("b", 90000, "CLOSE", 1, 110),
        fill("c", 180000, "CLOSE", 1, 100),
    ]
    out = result(fills, [bar(0), bar(60000, 900, 1), bar(120000, 130)])
    assert out["fill_changing_minutes_excluded"] == 1
    assert out["constant_inventory_minutes"] == 2
    assert Decimal(out["mfe_gross_usdt_lower_bound"]) == 40


def test_scale_in_weighted_cost_and_same_time_split_fills():
    fills = [
        fill("a", 0, "OPEN", 1, 90),
        fill("b", 0, "OPEN", 3, 110),
        fill("c", 60000, "CLOSE", 1, 120),
        fill("d", 120000, "CLOSE", 3, 100),
    ]
    out = result(fills, [bar(0, 120), bar(60000, 120)])
    assert Decimal(out["realized_gross_usdt"]) == 0
    assert Decimal(out["opening_notional_usdt"]) == 420


def test_stagnation_is_close_sample_count_not_intrabar_claim():
    fills = [fill("a", 0, "OPEN", 1, 100), fill("b", 240000, "CLOSE", 1, 100)]
    out = result(
        fills,
        [bar(0, c=105), bar(60000, c=104), bar(120000, c=104), bar(180000, c=106)],
    )
    assert out["longest_no_new_peak_sample_minutes"] == 2
    assert Decimal(out["profitable_close_sample_fraction"]) == 1


def test_loss_after_profit_retains_negative_capture():
    fills = [fill("a", 0, "OPEN", 1, 100), fill("b", 120000, "CLOSE", 1, 95)]
    out = result(fills, [bar(0), bar(60000)])
    assert Decimal(out["observed_peak_capture_ratio"]) == Decimal("-0.5")
    assert Decimal(out["giveback_gross_usdt_lower_bound"]) == 15


@pytest.mark.parametrize("bars", [[], [bar(0)], [bar(60000)]])
def test_gaps_suppress_excursions_but_preserve_actual_gross(bars):
    out = result(
        [fill("a", 0, "OPEN", 1, 100), fill("b", 120000, "CLOSE", 1, 105)], bars
    )
    assert out["status"] == "INCOMPLETE_HISTORY"
    assert out["mfe_gross_usdt_lower_bound"] is None
    assert Decimal(out["realized_gross_usdt"]) == 5


def test_subminute_trade_does_not_make_up_zero_excursions():
    out = result(
        [fill("a", 1000, "OPEN", 1, 100), fill("b", 2000, "CLOSE", 1, 101)], []
    )
    assert out["status"] == "INSUFFICIENT_MINUTE_RESOLUTION"
    assert out["mae_gross_usdt_lower_bound"] is None


def test_zero_favorable_excursion_capture_is_unknown():
    fills = [fill("a", 0, "OPEN", 1, 100), fill("b", 60000, "CLOSE", 1, 90)]
    out = result(fills, [bar(0, 99, 89, 95)])
    assert Decimal(out["mfe_gross_usdt_lower_bound"]) == 0
    assert out["observed_peak_capture_ratio"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("t", True),
        ("t", -1),
        ("quantity", "0"),
        ("quantity", "NaN"),
        ("price", "Infinity"),
        ("price", 1.0),
        ("leg", "BUY"),
        ("identity", ""),
    ],
)
def test_invalid_fill(field, value):
    args = {"identity": "x", "t": 0, "leg": "OPEN", "quantity": "1", "price": "100"}
    args[field] = value
    with pytest.raises(ValueError):
        PathFill(**args)


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "future",
        "overclose",
        "open",
        "ambiguous",
        "roundtrip",
        "bar_duplicate",
        "bar_reverse",
        "misaligned",
    ],
)
def test_invalid_sequences_fail_closed(kind):
    fills, bars = example()
    if kind == "duplicate":
        fills.append(fills[0])
    if kind == "future":
        fills[-1] = replace(fills[-1], t=1000000)
    if kind == "overclose":
        fills[-1] = replace(fills[-1], quantity="3")
    if kind == "open":
        fills[-1] = replace(fills[-1], quantity="0.5")
    if kind == "ambiguous":
        fills[1] = replace(fills[1], t=0)
    if kind == "roundtrip":
        fills = [
            fill("a", 0, "OPEN", 1, 100),
            fill("b", 60000, "CLOSE", 1, 100),
            fill("c", 120000, "OPEN", 1, 100),
            fill("d", 240000, "CLOSE", 1, 100),
        ]
    if kind == "bar_duplicate":
        bars[1] = bars[0]
    if kind == "bar_reverse":
        bars.reverse()
    if kind == "misaligned":
        bars[0] = replace(bars[0], t=1)
    with pytest.raises(ValueError):
        result(fills, bars)


def test_input_order_independent_and_decimal_context_independent():
    fills, bars = example()
    expected = result(fills, bars)
    with localcontext() as ctx:
        ctx.prec = 3
        assert result(list(reversed(fills)), bars) == expected


def test_tied_closing_prices_do_not_invent_an_intramillisecond_sequence():
    fills = [
        fill("a", 0, "OPEN", 3, 100),
        fill("b", 90000, "CLOSE", 1, 200),
        fill("c", 90000, "CLOSE", 1, 50),
        fill("d", 180000, "CLOSE", 1, 100),
    ]
    bars = [bar(0, 100, 100), bar(60000, 200, 50), bar(120000, 100, 100)]
    out = result(fills, bars)
    assert out["ambiguous_fill_price_groups_excluded"] == 1
    assert Decimal(out["realized_gross_usdt"]) == 50
    assert Decimal(out["mfe_gross_usdt_lower_bound"]) == 50
    # Changing identities must not change which unknown sub-ms path is assumed.
    fills[1], fills[2] = (
        replace(fills[1], identity="c"),
        replace(fills[2], identity="b"),
    )
    assert result(fills, bars) == out


def inputs():
    fills, _ = example()
    from dataclasses import asdict

    return {
        "kind": "OFFLINE_FACTUAL_TRADE_PATH_REVIEW",
        "account_scope": {"environment": "SANDBOX", "account_id": "test"},
        "as_of_ms": 999999,
        "cases": [
            {
                "trade": {
                    "id": "trade",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "source": "tv_bridge",
                    "strategy_version": "v1",
                    "config_digest": "digest",
                    "net_pnl": "14",
                    "opened_ms": 0,
                    "closed_ms": 240000,
                    "opening_quantity": "2",
                    "closing_quantity": "2",
                    "opening_notional": "200",
                    "closing_notional": "215",
                },
                "fills": [
                    {**asdict(f), "quantity": str(f.quantity), "price": str(f.price)}
                    for f in fills
                ],
            }
        ],
    }


def reader(*args):
    return [b.payload() for b in reversed(example()[1])]


def test_enrichment_net_is_not_deducted_twice_and_has_bound_provenance():
    source = inputs()
    original = deepcopy(source)
    out = enrich_path_inputs(source, read_bars=reader)
    row = out["reports"][0]
    assert row["settled_net_pnl_usdt"] == "14"
    assert Decimal(row["net_minus_gross_usdt"]) == -1
    assert len(row["input_digest"]) == 64
    assert source == original
    source["account_scope"]["account_id"] = "other"
    assert (
        enrich_path_inputs(source, read_bars=reader)["reports"][0]["input_digest"]
        != row["input_digest"]
    )


@pytest.mark.parametrize(
    "mutation,status",
    [
        ("quantity", "INVALID_OR_INCOMPLETE_FILL_EVIDENCE"),
        ("budget", "TRADE_HISTORY_BUDGET_EXCEEDED"),
        ("batch", "BATCH_HISTORY_BUDGET_EXCEEDED"),
        ("failure", "ARCHIVE_UNAVAILABLE_OR_INVALID"),
        ("gap", "INCOMPLETE_HISTORY"),
        ("wrongpage", "ARCHIVE_UNAVAILABLE_OR_INVALID"),
    ],
)
def test_enrichment_missing_inputs_never_become_zero(mutation, status):
    source, options = inputs(), {}

    def read(*args):
        if mutation == "failure":
            raise RuntimeError("secret transport text")
        if mutation == "gap":
            return []
        if mutation == "wrongpage":
            return [bar(999999).payload()]
        return reader(*args)

    if mutation == "quantity":
        source["cases"][0]["trade"]["opening_quantity"] = "9"
    if mutation == "budget":
        options["max_minutes"] = 1
    if mutation == "batch":
        options["max_total_minutes"] = 1
    out = enrich_path_inputs(source, read_bars=read, **options)
    assert out["reports"][0]["path"]["status"] == status
    assert "secret" not in str(out)


def test_live_scope_cannot_read_archive():
    source = inputs()
    source["account_scope"]["environment"] = "LIVE"
    with pytest.raises(ValueError):
        enrich_path_inputs(source, read_bars=lambda *a: pytest.fail("unexpected read"))


def test_seeded_scale_in_out_gross_matches_cash_flow_identity():
    import random
    from fractions import Fraction

    rng = random.Random(42)
    for _ in range(100):
        prices = [rng.randint(80, 120) for _ in range(5)]
        fills = [
            fill("a", 0, "OPEN", 1, prices[0]),
            fill("b", 60000, "OPEN", 3, prices[1]),
            fill("c", 120000, "CLOSE", 2, prices[2]),
            fill("d", 180000, "OPEN", 1, prices[3]),
            fill("e", 240000, "CLOSE", 3, prices[4]),
        ]
        bars = [bar(t, 120, 80, 100) for t in range(0, 240000, 60000)]
        for side, direction in (("BUY", 1), ("SELL", -1)):
            cash = (
                sum(
                    (
                        Fraction(f.quantity)
                        * Fraction(f.price)
                        * (1 if f.leg == "CLOSE" else -1)
                        for f in fills
                    ),
                    Fraction(0),
                )
                * direction
            )
            out = result(fills, bars, side=side)
            assert Fraction(out["realized_gross_usdt"]) == cash
            assert Fraction(out["mfe_gross_usdt_lower_bound"]) >= max(0, cash)
            assert Fraction(out["mae_gross_usdt_lower_bound"]) >= max(0, -cash)
