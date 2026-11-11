"""Prospective evidence only: missing snapshots never become fabricated proof."""

import ast
import inspect
import json
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import localcontext
from uuid import UUID

import pytest

from services import v2_exit_research_snapshot as adapter
from tests.support.v2_exit_research import HOUR, snapshot
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.state import StateKey

SCOPE = AccountScope("BINANCE", "private-research-account", "SANDBOX", "FUTURES")
EPISODE = str(UUID(int=123))


def inputs(side="BUY", *, raw_rows=False):
    saved = snapshot(side)
    at, facts = saved["at_ms"], saved["facts"]
    market = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "source": "binance-public-exit-v1",
        **saved["observation"],
        **{
            key: facts[key]
            for key in (
                "mark_price",
                "funding_rate",
                "ema9_1h",
                "ema20_1h",
                "exit_fee_rate",
                "momentum_closes_15m",
            )
        },
    }
    rows = saved["hourly_history"]
    if raw_rows:
        rows = [
            [
                bar["t"],
                bar["o"],
                bar["h"],
                bar["l"],
                bar["c"],
                bar["v"],
                bar["t"] + HOUR - 1,
                "0",
                1,
                "0",
                "0",
                "0",
            ]
            for bar in rows
        ]
    spec = {
        "episode": EPISODE,
        "symbol": "BTCUSDT",
        "side": "SELL" if side == "BUY" else "BUY",
        "kind": "STOP_MARKET",
        "trigger_price": facts["stop_price"],
    }
    key = StateKey(
        **asdict(SCOPE),
        namespace="testnet-protection-v1",
        key=canonical({"episode": EPISODE, "symbol": "BTCUSDT", "kind": "STOP_MARKET"}),
    )
    response = {
        "symbol": "BTCUSDT",
        "side": spec["side"],
        "positionSide": "BOTH",
        "orderType": "STOP_MARKET",
        "algoType": "CONDITIONAL",
        "algoStatus": "NEW",
        "workingType": "MARK_PRICE",
        "closePosition": True,
        "priceProtect": False,
        "triggerPrice": facts["stop_price"],
        "algoId": 123,
        "clientAlgoId": "v2p-" + digest(key.identity)[:28],
        "quantity": "0",
        "updateTime": 0,
    }
    return {
        "scope": SCOPE,
        "episode": EPISODE,
        "symbol": "BTCUSDT",
        "at_ms": at,
        "opened_at_ms": at - facts["held_ms"],
        "facts": facts,
        "baseline": saved["baseline"],
        "baseline_policy": saved["baseline_policy"],
        "observation": market,
        "hourly": {
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "interval": "1h",
            "market_source": "BINANCE_USDM_SANDBOX",
            "rows": rows,
            "available_at_ms": at,
        },
        "native": {
            "account_scope": asdict(SCOPE),
            "spec": spec,
            "response": response,
            "observed_at_ms": at - 20,
            "available_at_ms": at - 10,
            "quantity_evidence": {
                "account_scope": asdict(SCOPE),
                "observed_at_ms": at - 40,
                "available_at_ms": at - 30,
                "response": {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "1" if side == "BUY" else "-1",
                },
            },
        },
    }


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("raw_rows", [False, True])
def test_valid_detached_snapshot_preserves_original_evidence_without_authority(
    side, raw_rows
):
    values = inputs(side, raw_rows=raw_rows)
    before = deepcopy(values)
    result = adapter.build_exit_research_snapshot(**values)
    assert values == before
    assert result["status"] == "AVAILABLE" and result["qualification_gaps"] == []
    assert result["execution_authorized"] is result["promotion_authorized"] is False
    assert result["actual_pnl"] is result["historical_code_identity_proven"] is False
    assert result["binding"] == {
        "episode": EPISODE,
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "account_scope_digest": digest(canonical(asdict(SCOPE))),
        "opened_at_ms": values["opened_at_ms"],
        "at_ms": values["at_ms"],
    }
    frozen = result["snapshot"]
    assert frozen["native_protection"]["observed_at_ms"] == values["at_ms"] - 40
    assert frozen["native_protection"]["available_at_ms"] == values["at_ms"] - 10
    assert frozen["native_protection"]["protected_quantity"] == "1"
    assert (
        result["components"]["observation"]["indicator_version"]
        == "binance-public-exit-v1"
    )
    text = json.dumps(result)
    assert SCOPE.account_id not in text
    assert values["native"]["response"]["clientAlgoId"] not in text


def test_legacy_twenty_hours_retained_but_never_invented_warmup():
    values = inputs(raw_rows=True)
    values["hourly"]["rows"] = values["hourly"]["rows"][-20:]
    result = adapter.build_exit_research_snapshot(**values)
    assert result["status"] == "PARTIAL"
    assert result["qualification_gaps"] == ["HOURLY_TREND_WARMUP_INSUFFICIENT"]
    assert len(result["snapshot"]["hourly_history"]) == 20
    assert result["snapshot"]["baseline"] == values["baseline"]
    assert result["components"]["hourly_history"]["closed_bar_count"] == 20


def test_close_all_zero_quantity_without_separate_position_is_unknown():
    values = inputs()
    values["native"].pop("quantity_evidence")
    result = adapter.build_exit_research_snapshot(**values)
    assert result["status"] == "PARTIAL"
    assert result["components"]["native_terms"]["status"] == "AVAILABLE"
    assert (
        result["components"]["native_terms"]["evidence"]["time_basis"]
        == "LOCAL_QUERY_INTERVAL_NOT_VENUE_ORDER_UPDATE_TIME"
    )
    assert (
        result["components"]["native_terms"]["evidence"]["quantity_coverage_proven"]
        is False
    )
    assert (
        result["components"]["native_quantity"]["reason"]
        == "NATIVE_POSITION_QUANTITY_NOT_SUPPLIED"
    )
    assert result["snapshot"]["native_protection"] is None
    assert result["snapshot"]["observation"] is not None
    assert len(result["snapshot"]["hourly_history"]) == 24


def test_actual_existing_status_id_only_query_preserves_known_market():
    values = inputs()
    values["native"] = {
        "status": "NEW",
        "algo_id": 123,
        "client_algo_id": "existing-id",
    }
    result = adapter.build_exit_research_snapshot(**values)
    assert (
        result["status"] == "PARTIAL"
        and result["snapshot"]["native_protection"] is None
    )
    assert result["components"]["observation"]["status"] == "AVAILABLE"
    assert result["components"]["hourly_history"]["status"] == "AVAILABLE"


@pytest.mark.parametrize("field", ["observed_at_ms", "available_at_ms"])
@pytest.mark.parametrize("part", ["observation", "native", "position"])
def test_missing_acquisition_times_not_repaired_from_now_or_order_update(field, part):
    values = inputs()
    record = (
        values["native"]["quantity_evidence"] if part == "position" else values[part]
    )
    record.pop(field)
    values["native"]["response"]["updateTime"] = values["at_ms"]
    result = adapter.build_exit_research_snapshot(**values)
    component = {
        "observation": "observation",
        "native": "native_terms",
        "position": "native_quantity",
    }[part]
    assert result["components"][component]["status"] == "UNKNOWN"
    assert result["status"] == "PARTIAL"


@pytest.mark.parametrize("part", ["observation", "native", "position"])
@pytest.mark.parametrize(
    "mutation", ["future_seen", "future_receipt", "before_seen", "stale", "boolean"]
)
def test_future_stale_or_invalid_times_never_prove_availability(part, mutation):
    values = inputs()
    record = (
        values["native"]["quantity_evidence"] if part == "position" else values[part]
    )
    if mutation == "future_seen":
        record["observed_at_ms"] = values["at_ms"] + 1
    elif mutation == "future_receipt":
        record["available_at_ms"] = values["at_ms"] + 1
    elif mutation == "before_seen":
        record["available_at_ms"] = record["observed_at_ms"] - 1
    elif mutation == "stale":
        record["observed_at_ms"] = values["at_ms"] - 10000
    else:
        record["observed_at_ms"] = True
    result = adapter.build_exit_research_snapshot(**values)
    component = {
        "observation": "observation",
        "native": "native_terms",
        "position": "native_quantity",
    }[part]
    assert result["components"][component]["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ETHUSDT"},
        {"environment": "LIVE"},
        {"source": "UNKNOWN_PRIVATE_SOURCE"},
        {"market_source": "BINANCE_USDM_LIVE"},
        {"mark_price": "100.6"},
        {"funding_rate": "0.1"},
        {"ema9_1h": "99"},
        {"ema20_1h": "100"},
        {"exit_fee_rate": "0.001"},
        {"momentum_closes_15m": ["1"] * 4},
        {"closed_1h_at_ms": 0},
        {"closed_15m_at_ms": 0},
    ],
)
def test_market_scope_and_actual_exit_fact_binding(changes):
    values = inputs()
    values["observation"].update(changes)
    result = adapter.build_exit_research_snapshot(**values)
    assert result["components"]["observation"]["status"] == "UNKNOWN"
    assert result["components"]["native_terms"]["status"] == "AVAILABLE"
    assert "UNKNOWN_PRIVATE_SOURCE" not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ETHUSDT"},
        {"side": "BUY"},
        {"positionSide": "LONG"},
        {"orderType": "TAKE_PROFIT_MARKET"},
        {"algoType": "OTHER"},
        {"algoStatus": "TRIGGERED"},
        {"workingType": "CONTRACT_PRICE"},
        {"closePosition": "true"},
        {"priceProtect": True},
        {"triggerPrice": "94"},
        {"algoId": True},
        {"algoId": 0},
        {"clientAlgoId": "foreign-id"},
    ],
)
def test_native_stop_terms_and_registered_identity(changes):
    values = inputs()
    values["native"]["response"].update(changes)
    result = adapter.build_exit_research_snapshot(**values)
    assert result["components"]["native_terms"]["status"] == "UNKNOWN"
    assert result["components"]["native_quantity"]["status"] == "AVAILABLE"
    assert result["snapshot"]["native_protection"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"positionAmt": "0"},
        {"positionAmt": "-1"},
        {"positionAmt": "0.9"},
        {"positionAmt": "NaN"},
        {"positionAmt": 1.0},
        {"positionAmt": True},
        {"symbol": "ETHUSDT"},
        {"positionSide": "SHORT"},
    ],
)
def test_native_quantity_not_assumed_from_stop_or_ledger_remaining(changes):
    values = inputs()
    values["native"]["quantity_evidence"]["response"].update(changes)
    result = adapter.build_exit_research_snapshot(**values)
    assert result["components"]["native_terms"]["status"] == "AVAILABLE"
    assert result["components"]["native_quantity"]["status"] == "UNKNOWN"
    assert result["snapshot"]["native_protection"] is None


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_sub_remaining_position_cannot_round_up_to_covered(side):
    values = inputs(side)
    values["native"]["quantity_evidence"]["response"]["positionAmt"] = (
        "0.9999999" if side == "BUY" else "-0.9999999"
    )
    with localcontext() as context:
        context.prec = 5
        result = adapter.build_exit_research_snapshot(**values)
    assert (
        result["components"]["native_quantity"]["reason"]
        == "NATIVE_POSITION_DIRECTION_OR_QUANTITY_MISMATCH"
    )
    assert result["snapshot"]["native_protection"] is None


def test_exact_maximum_seventy_two_hour_rows_is_accepted():
    values = inputs(raw_rows=True)
    shift = 72 * HOUR
    values["at_ms"] += shift
    values["opened_at_ms"] += shift
    for record in (
        values["observation"],
        values["native"],
        values["native"]["quantity_evidence"],
    ):
        record["observed_at_ms"] += shift
        record["available_at_ms"] += shift
    values["observation"]["closed_1h_at_ms"] += shift
    values["observation"]["closed_15m_at_ms"] += shift
    values["hourly"]["available_at_ms"] += shift
    template = values["hourly"]["rows"][-1]
    values["hourly"]["rows"] = [
        [(24 + hour) * HOUR, *template[1:6], (25 + hour) * HOUR - 1, *template[7:]]
        for hour in range(72)
    ]
    result = adapter.build_exit_research_snapshot(**values)
    assert result["status"] == "AVAILABLE"
    assert len(result["snapshot"]["hourly_history"]) == 72


@pytest.mark.parametrize("part", ["native", "position"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "foreign"),
        ("environment", "LIVE"),
        ("exchange", "OTHER"),
        ("product", "SPOT"),
    ],
)
def test_native_cross_account_environment_and_product_rejected(part, field, value):
    values = inputs()
    record = (
        values["native"]["quantity_evidence"] if part == "position" else values[part]
    )
    record["account_scope"][field] = value
    result = adapter.build_exit_research_snapshot(**values)
    assert result["snapshot"]["native_protection"] is None
    assert result["status"] == "PARTIAL"


@pytest.mark.parametrize(
    "changes",
    [
        {"episode": str(UUID(int=124))},
        {"symbol": "ETHUSDT"},
        {"side": "BUY"},
        {"kind": "TAKE_PROFIT_MARKET"},
        {"trigger_price": "94"},
    ],
)
def test_native_original_spec_binding(changes):
    values = inputs()
    values["native"]["spec"].update(changes)
    result = adapter.build_exit_research_snapshot(**values)
    assert (
        result["components"]["native_terms"]["reason"] == "NATIVE_STOP_BINDING_UNPROVEN"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_receipt",
        "future_receipt",
        "future_row",
        "before_close",
        "gap",
        "duplicate",
        "reorder",
        "bad_ohlc",
        "negative_volume",
        "no_high",
        "unknown_per_row_receipt",
        "bad_close_time",
    ],
)
def test_hourly_unproven_or_invalid_data_is_not_reconstructed(mutation):
    values = inputs(raw_rows=mutation == "bad_close_time")
    hourly = values["hourly"]
    rows = hourly["rows"]
    if mutation == "missing_receipt":
        hourly.pop("available_at_ms")
        for row in rows:
            row.pop("available_at_ms")
    elif mutation == "future_receipt":
        hourly["available_at_ms"] = values["at_ms"] + 1
    elif mutation == "future_row":
        rows[-1]["t"] += HOUR
    elif mutation == "before_close":
        rows[-1]["available_at_ms"] = rows[-1]["t"]
    elif mutation == "gap":
        rows.pop(10)
    elif mutation == "duplicate":
        rows[10] = deepcopy(rows[9])
    elif mutation == "reorder":
        rows.reverse()
    elif mutation == "bad_ohlc":
        rows[-1]["h"] = "1"
    elif mutation == "negative_volume":
        rows[-1]["v"] = "-1"
    elif mutation == "no_high":
        rows[-1].pop("h")
    elif mutation == "unknown_per_row_receipt":
        rows[-1]["available_at_ms"] = None
    else:
        rows[-1][6] += 1
    result = adapter.build_exit_research_snapshot(**values)
    assert result["components"]["hourly_history"]["status"] == "UNKNOWN"
    assert result["snapshot"]["hourly_history"] == []
    assert result["components"]["observation"]["status"] == "AVAILABLE"


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "LIVE"},
        {"symbol": "ETHUSDT"},
        {"interval": "15m"},
        {"market_source": "BINANCE_USDM_LIVE"},
    ],
)
def test_hourly_scope_source_and_interval(changes):
    values = inputs()
    values["hourly"].update(changes)
    result = adapter.build_exit_research_snapshot(**values)
    assert (
        result["components"]["hourly_history"]["reason"]
        == "HOURLY_HISTORY_MARKET_SCOPE_MISMATCH"
    )


@pytest.mark.parametrize("rows", [[], [{}] * 73, None])
def test_hourly_bound_checked_before_traversal(rows):
    values = inputs()
    values["hourly"]["rows"] = rows
    result = adapter.build_exit_research_snapshot(**values)
    assert result["components"]["hourly_history"]["reason"] == "HOURLY_HISTORY_BOUND"


@pytest.mark.parametrize(
    "mutation",
    [
        "policy_incomplete",
        "baseline_changed",
        "holding_clock",
        "wrong_scope",
        "facts_extra",
        "bad_episode",
        "bad_symbol",
        "true_time",
    ],
)
def test_unbound_baseline_or_scope_cannot_form_research_snapshot(mutation):
    values = inputs()
    if mutation == "policy_incomplete":
        values["baseline_policy"].pop("exit.stagnation_minutes")
    elif mutation == "baseline_changed":
        values["baseline"]["reason"] = "UNKNOWN_SECRET"
    elif mutation == "holding_clock":
        values["opened_at_ms"] -= 1
    elif mutation == "wrong_scope":
        values["scope"] = replace(SCOPE, environment="LIVE")
    elif mutation == "facts_extra":
        values["facts"]["secret"] = "UNKNOWN_SECRET"
    elif mutation == "bad_episode":
        values["episode"] = "not-an-episode"
    elif mutation == "bad_symbol":
        values["symbol"] = "SECRET/OTHER"
    else:
        values["at_ms"] = True
    result = adapter.build_exit_research_snapshot(**values)
    assert result["status"] == "UNKNOWN" and result["snapshot"] is None
    assert "UNKNOWN_SECRET" not in json.dumps(result)


@pytest.mark.parametrize(
    "bounds",
    [
        {"max_age_ms": True},
        {"max_age_ms": 30001},
        {"minimum_hourly_bars": 73},
        {"minimum_hourly_bars": 0},
    ],
)
def test_invalid_bounds_fail_without_touching_nested_evidence(bounds):
    result = adapter.build_exit_research_snapshot(**{**inputs(), **bounds})
    assert result["reason"] == "EXIT_RESEARCH_BOUNDS_INVALID"


def test_missing_all_optional_evidence_retains_valid_original_baseline():
    values = inputs()
    values.update(observation=None, hourly=None, native=None)
    result = adapter.build_exit_research_snapshot(**values)
    assert result["status"] == "UNKNOWN"
    assert result["snapshot"]["baseline"] == values["baseline"]
    assert result["snapshot"]["native_protection"] is None
    assert result["snapshot"]["observation"] is None


def test_module_has_no_io_ports_clocks_or_research_to_execution_mutations():
    tree = ast.parse(inspect.getsource(adapter))
    names = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    assert not names & {
        "open",
        "read",
        "write",
        "execute",
        "connect",
        "request",
        "post",
        "time",
        "time_ns",
        "monotonic",
        "sleep",
        "save",
        "change",
    }
