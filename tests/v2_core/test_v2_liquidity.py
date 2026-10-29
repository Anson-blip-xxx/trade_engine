"""Pure snapshot gate checks; no exchange, database, service or filesystem I/O."""

import json
from copy import deepcopy
from decimal import Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest

from v2_core.evidence import canonical, digest
from v2_core.liquidity import evaluate_execution_quote, evaluate_liquidity

NOW = 1_800_000_000_000


def snapshot():
    return {
        "version": "binance-depth-v1",
        "symbol": "BTCUSDT",
        "source_environment": "LIVE",
        "source_host": "fapi.binance.com",
        "observed_at_ms": NOW - 300,
        "received_at_ms": NOW - 100,
        "last_update_id": 987654321,
        "bids": [["99.99", "1"], ["99.98", "2"], ["99.97", "3"]],
        "asks": [["100.01", "1"], ["100.02", "2"], ["100.03", "3"]],
    }


def evaluate(*, evidence=None, controls=None, **changes):
    return evaluate_liquidity(
        **{
            "evidence": snapshot() if evidence is None else evidence,
            "controls": {"mode": "ENFORCE", **(controls or {})},
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "2",
            "reference_price": "100",
            "now_ms": NOW,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "side,vwap,worst,best",
    [
        ("BUY", "100.015", "100.02", "100.01"),
        ("SELL", "99.985", "99.98", "99.99"),
    ],
)
def test_consumes_correct_side_with_partial_final_level(side, vwap, worst, best):
    proof = evaluate(side=side)
    assert proof["version"] == "liquidity-gate-v1"
    assert proof["reason"] == "LIQUIDITY_PASSED"
    assert proof["passed"] is proof["authorized"] is True
    assert proof["would_block"] is proof["blocked"] is False
    metrics = proof["observed"]
    assert metrics["vwap_price"] == vwap
    assert metrics["worst_price"] == worst
    assert metrics["side_best_price"] == best
    assert metrics["mid_price"] == "100"
    assert metrics["spread_bps"] == "2"
    assert metrics["price_basis_bps"] == "0"
    assert metrics["available_quantity"] == "6"
    assert metrics["consumed_levels"] == "2"
    assert Decimal(metrics["vwap_impact_bps"]) > 0
    assert Decimal(metrics["worst_impact_bps"]) > Decimal(metrics["vwap_impact_bps"])
    assert proof["evidence_digest"] == digest(canonical(snapshot()))
    assert all(isinstance(value, str) for value in metrics.values())
    assert json.loads(json.dumps(proof, allow_nan=False)) == proof


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_shallow_executable_side_never_borrows_opposite_book_or_extrapolates(side):
    evidence = snapshot()
    shallow = "asks" if side == "BUY" else "bids"
    other = "bids" if side == "BUY" else "asks"
    evidence[shallow] = evidence[shallow][:1]
    evidence[other][0][1] = "9999"
    proof = evaluate(evidence=evidence, side=side)
    assert proof["reason"] == "LIQUIDITY_INSUFFICIENT_DEPTH"
    assert proof["blocked"] is True
    assert proof["authorized"] is False
    assert proof["observed"]["quantity_shortfall"] == "1"
    assert "vwap_price" not in proof["observed"]
    assert proof["missing"] == ["executable_depth"]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_small_tail_beyond_impact_limit_blocks_even_when_vwap_is_acceptable(side):
    evidence = snapshot()
    evidence["bids"] = [["99.99", "100"], ["99", "100"]]
    evidence["asks"] = [["100.01", "100"], ["101", "100"]]
    proof = evaluate(evidence=evidence, side=side, quantity="100.01")
    assert proof["reason"] == "LIQUIDITY_IMPACT_EXCEEDED"
    assert Decimal(proof["observed"]["vwap_impact_bps"]) < 15
    assert Decimal(proof["observed"]["worst_impact_bps"]) > 15


def test_exact_level_boundary_does_not_consume_next_bad_level():
    evidence = snapshot()
    evidence["asks"] = [["100.01", "2"], ["1000", "10"]]
    proof = evaluate(evidence=evidence, quantity="2")
    assert proof["reason"] == "LIQUIDITY_PASSED"
    assert proof["observed"]["consumed_levels"] == "1"
    assert proof["observed"]["worst_impact_bps"] == "0"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("version", "other-v1", "LIQUIDITY_EVIDENCE_VERSION_INVALID"),
        ("symbol", "ETHUSDT", "LIQUIDITY_SYMBOL_MISMATCH"),
        ("source_environment", "TESTNET", "LIQUIDITY_SOURCE_ENVIRONMENT_INVALID"),
        ("source_environment", "live", "LIQUIDITY_SOURCE_ENVIRONMENT_INVALID"),
        ("source_host", "testnet.binancefuture.com", "LIQUIDITY_SOURCE_HOST_INVALID"),
        (
            "source_host",
            "fapi.binance.com.evil.invalid",
            "LIQUIDITY_SOURCE_HOST_INVALID",
        ),
        ("source_host", "https://fapi.binance.com", "LIQUIDITY_SOURCE_HOST_INVALID"),
        ("observed_at_ms", NOW + 1, "LIQUIDITY_FUTURE_EVIDENCE"),
        ("received_at_ms", NOW + 1, "LIQUIDITY_FUTURE_EVIDENCE"),
        ("observed_at_ms", NOW - 50, "LIQUIDITY_TIMESTAMP_ORDER_INVALID"),
        ("observed_at_ms", str(NOW - 300), "LIQUIDITY_TIMESTAMP_INVALID"),
        ("received_at_ms", True, "LIQUIDITY_TIMESTAMP_INVALID"),
        ("observed_at_ms", -1, "LIQUIDITY_TIMESTAMP_INVALID"),
        ("last_update_id", True, "LIQUIDITY_UPDATE_ID_INVALID"),
        ("last_update_id", "987654321", "LIQUIDITY_UPDATE_ID_INVALID"),
        ("last_update_id", 0, "LIQUIDITY_UPDATE_ID_INVALID"),
    ],
)
def test_invalid_identity_provenance_and_clocks_fail_closed(field, value, reason):
    evidence = snapshot()
    evidence[field] = value
    proof = evaluate(evidence=evidence)
    assert proof["reason"] == reason
    assert proof["authorized"] is False
    assert proof["blocked"] is True


def test_fresh_receive_time_cannot_hide_stale_observation_and_deadline_is_exclusive():
    evidence = snapshot()
    evidence["observed_at_ms"] = NOW - 4999
    evidence["received_at_ms"] = NOW
    assert evaluate(evidence=evidence)["reason"] == "LIQUIDITY_PASSED"
    evidence["observed_at_ms"] -= 1
    proof = evaluate(evidence=evidence)
    assert proof["reason"] == "LIQUIDITY_STALE"
    assert proof["observed"]["age_ms"] == "5000"
    assert proof["missing"] == ["fresh_order_book"]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [["100.01", "1"]] * 101,
        [["100.01"]],
        [["100.01", "1", "extra"]],
        [("100.01", "1")],
        {"price": "100.01", "quantity": "1"},
        [["NaN", "1"]],
        [["sNaN", "1"]],
        [["Infinity", "1"]],
        [["100.01", "NaN"]],
        [["0", "1"]],
        [["-1", "1"]],
        [["100.01", "0"]],
        [["100.01", "-1"]],
        [[100.01, "1"]],
        [["100.01", 1]],
        [["100.01", True]],
        [["100.01", Decimal(1)]],
        [[" 100.01", "1"]],
        [["100.01 ", "1"]],
        [["1_00.01", "1"]],
        [["1e999999999", "1"]],
    ],
)
def test_invalid_depth_has_no_authorization(rows):
    evidence = snapshot()
    evidence["asks"] = rows
    proof = evaluate(evidence=evidence)
    assert proof["reason"] == "LIQUIDITY_DEPTH_INVALID"
    assert proof["authorized"] is False
    json.dumps(proof, allow_nan=False)


@pytest.mark.parametrize("book", ["bids", "asks"])
@pytest.mark.parametrize("mutation", ["reverse", "duplicate"])
def test_depth_requires_strict_order_and_unique_price(book, mutation):
    evidence = snapshot()
    if mutation == "reverse":
        evidence[book].reverse()
    else:
        evidence[book][1][0] = evidence[book][0][0]
    assert evaluate(evidence=evidence)["reason"] == "LIQUIDITY_DEPTH_UNSORTED"


@pytest.mark.parametrize("best_bid", ["100.01", "100.02"])
def test_locked_and_crossed_books_are_rejected(best_bid):
    evidence = snapshot()
    evidence["bids"][0][0] = best_bid
    assert evaluate(evidence=evidence)["reason"] == "LIQUIDITY_BOOK_CROSSED"


@pytest.mark.parametrize("field", ["quantity", "reference_price"])
@pytest.mark.parametrize(
    "value", ["0", "-1", "NaN", "Infinity", "1_0", "", 1, 1.0, True]
)
def test_invalid_order_amounts_reject_without_float_conversion(field, value):
    assert evaluate(**{field: value})["reason"] == "LIQUIDITY_REQUEST_INVALID"


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "BTCUSDC"},
        {"symbol": "btcusdt"},
        {"side": "buy"},
        {"side": []},
        {"now_ms": True},
        {"now_ms": str(NOW)},
    ],
)
def test_request_identity_and_clock_are_strict(changes):
    assert evaluate(**changes)["reason"] == "LIQUIDITY_REQUEST_INVALID"


def test_all_threshold_boundaries_are_inclusive_and_compared_exactly():
    evidence = snapshot()
    evidence["bids"] = [["99.98", "1"]]
    evidence["asks"] = [["100", "1"], ["100.15", "2"]]
    proof = evaluate(
        evidence=evidence,
        quantity="2",
        reference_price="99.99",
        controls={"max_spread_bps": "3", "max_impact_bps": "15"},
    )
    assert proof["reason"] == "LIQUIDITY_PASSED"
    assert proof["observed"]["worst_impact_bps"] == "15"
    evidence["asks"][1][0] = "100.150000000000000001"
    assert evaluate(evidence=evidence)["reason"] == "LIQUIDITY_IMPACT_EXCEEDED"
    assert evaluate(controls={"max_spread_bps": "2"})["reason"] == "LIQUIDITY_PASSED"
    assert (
        evaluate(controls={"max_spread_bps": "1.999999999999999999"})["reason"]
        == "LIQUIDITY_SPREAD_EXCEEDED"
    )
    assert evaluate(reference_price="101")["reason"] == "LIQUIDITY_PASSED"
    assert (
        evaluate(reference_price="101.000000000000000001")["reason"]
        == "LIQUIDITY_PRICE_BASIS_EXCEEDED"
    )


@pytest.mark.parametrize("reference", ["90", "110"])
def test_testnet_reference_divergence_from_live_mid_blocks_both_directions(reference):
    proof = evaluate(reference_price=reference)
    assert proof["reason"] == "LIQUIDITY_PRICE_BASIS_EXCEEDED"
    assert proof["observed"]["price_basis_bps"] == "1000"


@pytest.mark.parametrize(
    "quantity,reason",
    [("2", "LIQUIDITY_PASSED"), ("7", "LIQUIDITY_INSUFFICIENT_DEPTH")],
)
def test_shadow_only_reports_counterfactual_and_never_authorizes(quantity, reason):
    proof = evaluate(controls={"mode": "SHADOW"}, quantity=quantity)
    assert proof["reason"] == reason
    assert proof["would_block"] == (reason != "LIQUIDITY_PASSED")
    assert proof["authorized"] is proof["blocked"] is False


def test_off_does_not_inspect_evidence_or_other_execution_inputs():
    class Unreadable(dict):
        def get(self, *args):
            raise AssertionError("OFF read evidence")

        def __getitem__(self, key):
            raise AssertionError("OFF read evidence")

    proof = evaluate(
        evidence=Unreadable(),
        controls={"mode": "OFF", "max_age_ms": "invalid"},
        symbol=None,
        side=None,
        quantity=object(),
        reference_price=object(),
        now_ms=object(),
    )
    assert proof["reason"] == "LIQUIDITY_DISABLED"
    assert proof["authorized"] is proof["blocked"] is proof["would_block"] is False
    assert proof["observed"] == {}
    assert proof["evidence_digest"] is None


def test_missing_and_unavailable_evidence_are_explicit_and_redacted():
    proof = evaluate_liquidity(
        evidence=None,
        controls={"mode": "ENFORCE"},
        symbol="BTCUSDT",
        side="BUY",
        quantity="1",
        reference_price="100",
        now_ms=NOW,
    )
    assert proof["reason"] == "LIQUIDITY_EVIDENCE_MISSING"
    assert proof["missing"] == ["order_book"]
    unavailable = {
        "version": "binance-depth-v1",
        "status": "UNAVAILABLE",
        "reason": "DEPTH_TIMEOUT",
    }
    proof = evaluate(evidence=unavailable)
    assert proof["reason"] == "LIQUIDITY_UNAVAILABLE"
    assert proof["observed"]["unavailable_reason"] == "DEPTH_TIMEOUT"
    assert proof["evidence_digest"] == digest(canonical(unavailable))
    unavailable["reason"] = "request failed with api_key=do-not-expose"
    proof = evaluate(evidence=unavailable)
    assert proof["observed"]["unavailable_reason"] == "UNSPECIFIED"
    assert "do-not-expose" not in json.dumps(proof)
    evidence = snapshot()
    del evidence["asks"]
    proof = evaluate(evidence=evidence)
    assert proof["reason"] == "LIQUIDITY_EVIDENCE_MISSING"
    assert proof["missing"] == ["asks"]


def test_numeric_outputs_are_deterministic_and_do_not_mutate_input_or_use_ambient_context():
    evidence = snapshot()
    controls = {"mode": "ENFORCE"}
    before = deepcopy((evidence, controls))
    baseline = evaluate(evidence=evidence, controls=controls, quantity="3")
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        actual = evaluate(evidence=evidence, controls=controls, quantity="3")
    assert actual == baseline
    assert (evidence, controls) == before
    assert Fraction(actual["observed"]["executable_notional"]) == Fraction("300.05")
    assert actual["limitations"]
    assert "probability" in " ".join(actual["limitations"])
    assert "fill guarantee" in " ".join(actual["limitations"])
    assert "confidence" not in actual


@pytest.mark.parametrize(
    "controls",
    [
        {"mode": "INVALID"},
        {"mode": []},
        {"max_age_ms": True},
        {"max_age_ms": -1},
        {"max_spread_bps": "NaN"},
        {"max_impact_bps": "-1"},
        {"max_price_basis_bps": 100.0},
    ],
)
def test_active_invalid_controls_do_not_authorize(controls):
    proof = evaluate(controls=controls)
    assert proof["reason"] == "LIQUIDITY_CONTROLS_INVALID"
    assert proof["authorized"] is False


def execution_quote():
    return {
        "symbol": "BTCUSDT",
        "book_at_ms": NOW - 1000,
        "spread_fraction": "0.002",
        "top_notional": "10000",
        "tiers": [],
        "environment": "SANDBOX",
        "source_host": "demo-fapi.binance.com",
    }


def evaluate_quote(*, evidence=None, controls=None, **changes):
    return evaluate_execution_quote(
        **{
            "evidence": execution_quote() if evidence is None else evidence,
            "controls": {"mode": "ENFORCE", **(controls or {})},
            "symbol": "BTCUSDT",
            "now_ms": NOW,
            **changes,
        }
    )


def test_execution_quote_uses_mid_spread_and_has_no_trade_side_dependency():
    evidence = execution_quote()
    before = deepcopy(evidence)
    baseline = evaluate_quote(evidence=evidence)
    assert baseline["reason"] == "LIQUIDITY_EXECUTION_PASSED"
    assert baseline["passed"] is baseline["authorized"] is True
    assert baseline["would_block"] is baseline["blocked"] is False
    metrics = baseline["observed"]
    assert metrics["spread_fraction"] == "0.002"
    assert Decimal(metrics["spread_bps"]) < 20
    assert abs(Fraction(metrics["spread_bps"]) - Fraction(20000, 1001)) < Fraction(
        1, 10**75
    )
    assert all(isinstance(value, str) for value in metrics.values())
    assert evidence == before
    for side in ("BUY", "SELL"):
        evidence["side"] = side
        assert evaluate_quote(evidence=evidence) == baseline
    assert json.loads(json.dumps(baseline, allow_nan=False)) == baseline
    assert "fill guarantee" in " ".join(baseline["limitations"])
    assert "multi-level depth" in " ".join(baseline["limitations"])
    assert "quantity" not in metrics
    assert "confidence" not in baseline


def test_execution_quote_does_not_treat_top_notional_or_tiers_as_depth_evidence():
    evidence = execution_quote()
    baseline = evaluate_quote(evidence=evidence)
    for value in ("0", "10000000000000000000", float("nan"), object()):
        evidence["top_notional"] = value
        evidence["tiers"] = value
        assert evaluate_quote(evidence=evidence) == baseline
    del evidence["top_notional"]
    del evidence["tiers"]
    assert evaluate_quote(evidence=evidence) == baseline


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("symbol", "ETHUSDT", "LIQUIDITY_EXECUTION_SYMBOL_MISMATCH"),
        ("environment", "LIVE", "LIQUIDITY_EXECUTION_SOURCE_ENVIRONMENT_INVALID"),
        ("environment", "TESTNET", "LIQUIDITY_EXECUTION_SOURCE_ENVIRONMENT_INVALID"),
        ("source_host", "fapi.binance.com", "LIQUIDITY_EXECUTION_SOURCE_HOST_INVALID"),
        (
            "source_host",
            "demo-fapi.binance.com.evil.invalid",
            "LIQUIDITY_EXECUTION_SOURCE_HOST_INVALID",
        ),
        ("book_at_ms", NOW + 1, "LIQUIDITY_EXECUTION_FUTURE_EVIDENCE"),
        ("book_at_ms", -1, "LIQUIDITY_EXECUTION_TIMESTAMP_INVALID"),
        ("book_at_ms", True, "LIQUIDITY_EXECUTION_TIMESTAMP_INVALID"),
        ("book_at_ms", str(NOW), "LIQUIDITY_EXECUTION_TIMESTAMP_INVALID"),
        ("book_at_ms", NOW * 1.0, "LIQUIDITY_EXECUTION_TIMESTAMP_INVALID"),
    ],
)
def test_execution_quote_rejects_wrong_identity_provenance_and_clock(
    field, value, reason
):
    evidence = execution_quote()
    evidence[field] = value
    proof = evaluate_quote(evidence=evidence)
    assert proof["reason"] == reason
    assert proof["blocked"] is True
    assert proof["authorized"] is False


def test_execution_quote_age_is_independent_from_live_depth_and_exclusive():
    evidence = execution_quote()
    evidence["book_at_ms"] = NOW - 29999
    assert evaluate_quote(evidence=evidence)["reason"] == "LIQUIDITY_EXECUTION_PASSED"
    evidence["book_at_ms"] -= 1
    proof = evaluate_quote(evidence=evidence)
    assert proof["reason"] == "LIQUIDITY_EXECUTION_STALE"
    assert proof["observed"]["age_ms"] == "30000"
    assert proof["missing"] == ["fresh_execution_quote"]
    evidence["book_at_ms"] = NOW - 2000
    assert (
        evaluate_quote(evidence=evidence, controls={"max_age_ms": 1})["reason"]
        == "LIQUIDITY_EXECUTION_PASSED"
    )
    assert (
        evaluate_quote(
            evidence=evidence, controls={"max_execution_quote_age_ms": 2001}
        )["reason"]
        == "LIQUIDITY_EXECUTION_PASSED"
    )
    assert (
        evaluate_quote(
            evidence=evidence, controls={"max_execution_quote_age_ms": 1999}
        )["reason"]
        == "LIQUIDITY_EXECUTION_STALE"
    )


@pytest.mark.parametrize(
    "spread",
    ["NaN", "sNaN", "Infinity", "-0.01", "", " 0.01", "0.0_1", 0.01, 0, True, None],
)
def test_execution_quote_spread_requires_nonnegative_finite_decimal_string(spread):
    evidence = execution_quote()
    evidence["spread_fraction"] = spread
    proof = evaluate_quote(evidence=evidence)
    assert proof["reason"] == "LIQUIDITY_EXECUTION_SPREAD_INVALID"
    assert proof["authorized"] is False
    json.dumps(proof, allow_nan=False)


def test_execution_quote_spread_threshold_equality_and_zero_are_inclusive():
    evidence = execution_quote()
    evidence["spread_fraction"] = "0"
    assert (
        evaluate_quote(evidence=evidence, controls={"max_spread_bps": "0"})["reason"]
        == "LIQUIDITY_EXECUTION_PASSED"
    )
    evidence["spread_fraction"] = "0.048"
    proof = evaluate_quote(evidence=evidence, controls={"max_spread_bps": "468.75"})
    assert proof["reason"] == "LIQUIDITY_EXECUTION_PASSED"
    assert proof["observed"]["spread_bps"] == "468.75"
    assert (
        evaluate_quote(
            evidence=evidence,
            controls={"max_spread_bps": "468.749999999999999999"},
        )["reason"]
        == "LIQUIDITY_EXECUTION_SPREAD_EXCEEDED"
    )
    evidence["spread_fraction"] = "0.0002"
    assert (
        evaluate_quote(
            evidence=evidence,
            controls={"max_spread_bps": "1.999800019998000199"},
        )["reason"]
        == "LIQUIDITY_EXECUTION_SPREAD_EXCEEDED"
    )
    assert (
        evaluate_quote(
            evidence=evidence,
            controls={"max_spread_bps": "1.999800019998000200"},
        )["reason"]
        == "LIQUIDITY_EXECUTION_PASSED"
    )


def test_execution_quote_missing_evidence_is_explicit():
    proof = evaluate_execution_quote(
        evidence=None,
        controls={"mode": "ENFORCE"},
        symbol="BTCUSDT",
        now_ms=NOW,
    )
    assert proof["reason"] == "LIQUIDITY_EXECUTION_EVIDENCE_MISSING"
    assert proof["missing"] == ["execution_quote"]
    evidence = execution_quote()
    del evidence["spread_fraction"]
    proof = evaluate_quote(evidence=evidence)
    assert proof["reason"] == "LIQUIDITY_EXECUTION_EVIDENCE_MISSING"
    assert proof["missing"] == ["spread_fraction"]
    assert (
        evaluate_quote(evidence=[])["reason"] == "LIQUIDITY_EXECUTION_EVIDENCE_INVALID"
    )


@pytest.mark.parametrize("spread,passed", [("0.002", True), ("0.01", False)])
def test_execution_quote_shadow_never_authorizes_or_blocks(spread, passed):
    evidence = execution_quote()
    evidence["spread_fraction"] = spread
    proof = evaluate_quote(evidence=evidence, controls={"mode": "SHADOW"})
    assert proof["passed"] is passed
    assert proof["would_block"] is not passed
    assert proof["authorized"] is proof["blocked"] is False


def test_execution_quote_off_does_not_inspect_evidence():
    class Unreadable(dict):
        def get(self, *args):
            raise AssertionError("OFF read quote")

        def __getitem__(self, key):
            raise AssertionError("OFF read quote")

        def __contains__(self, key):
            raise AssertionError("OFF read quote")

    proof = evaluate_quote(
        evidence=Unreadable(),
        controls={"mode": "OFF", "max_execution_quote_age_ms": "invalid"},
        symbol=None,
        now_ms=object(),
    )
    assert proof["reason"] == "LIQUIDITY_EXECUTION_DISABLED"
    assert proof["authorized"] is proof["blocked"] is proof["would_block"] is False
    assert proof["observed"] == {}
    assert proof["evidence_digest"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "BTCUSDC"},
        {"symbol": "btcusdt"},
        {"symbol": []},
        {"now_ms": True},
        {"now_ms": str(NOW)},
    ],
)
def test_execution_quote_request_identity_and_clock_are_strict(changes):
    assert evaluate_quote(**changes)["reason"] == "LIQUIDITY_EXECUTION_REQUEST_INVALID"


@pytest.mark.parametrize(
    "controls",
    [
        {"mode": "INVALID"},
        {"mode": []},
        {"max_execution_quote_age_ms": True},
        {"max_execution_quote_age_ms": -1},
        {"max_execution_quote_age_ms": "30000"},
        {"max_spread_bps": "NaN"},
        {"max_spread_bps": "-1"},
        {"max_spread_bps": 20.0},
    ],
)
def test_execution_quote_invalid_controls_cannot_authorize(controls):
    assert (
        evaluate_quote(controls=controls)["reason"]
        == "LIQUIDITY_EXECUTION_CONTROLS_INVALID"
    )


def test_execution_quote_ignores_ambient_decimal_context():
    baseline = evaluate_quote()
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert evaluate_quote() == baseline
