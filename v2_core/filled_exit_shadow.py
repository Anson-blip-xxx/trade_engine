"""Shadow exits anchored to real Sandbox fills; no live policy/execution calls.

Opening quantities/prices/fees come from immutable fills. Five-minute OHLC cannot
resolve the opening boundary: only bars starting after the LAST opening fill
are eligible. Multi-fill entries with interleaved closes are explicitly excluded.
The actual account settlement is a separate fact, not a shadow performance delta.
"""

from collections import Counter
from dataclasses import asdict
from decimal import Decimal, localcontext

from v2_core.evidence import canonical, digest
from v2_core.exit_comparison import VARIANTS, ExitPolicy, _episode
from v2_core.quant_indicators import Bar, decimal, encode
from v2_core.quant_registry import feature_frame, parameters, warmup
from v2_core.quant_replay import ReplayPolicy

VERSION = "actual-filled-exit-shadow-v1"
WIDTH = 300000
STAGES = ("valid_volume_range", "profit", "volume", "wick", "close_location")


def conditions(bars, entry, policy):
    """Individual checks AND ordered cumulative funnel, same frozen P2 formula."""
    if len(bars) < policy.volume_bars + 1:
        return {"ready": False, "reason": "WARMUP", "checks": {}}
    bar = bars[-1]
    mean = (
        sum((b.v for b in bars[-policy.volume_bars - 1 : -1]), Decimal(0))
        / policy.volume_bars
    )
    span = bar.h - bar.l
    sign = 1 if entry["side"] == "BUY" else -1
    price, stop = decimal(entry["entry_price"]), decimal(entry["initial_stop"])
    risk = abs(price - stop)
    if risk <= 0:
        raise ValueError("SHADOW_INITIAL_RISK_INVALID")
    wick = bar.h - max(bar.o, bar.c) if sign > 0 else min(bar.o, bar.c) - bar.l
    clv = (
        ((bar.c - bar.l) if sign > 0 else (bar.h - bar.c)) / span if span > 0 else None
    )
    profit_r = sign * (bar.c - price) / risk
    rv = bar.v / mean if mean > 0 else None
    rejection = wick / span if span > 0 else None
    checks = {
        "valid_volume_range": mean > 0 and span > 0,
        "profit": profit_r >= decimal(policy.min_profit_r),
        "volume": rv is not None and rv >= decimal(policy.min_relative_volume),
        "wick": rejection is not None
        and rejection >= decimal(policy.min_rejection_wick),
        "close_location": clv is not None
        and clv <= decimal(policy.max_directional_close),
    }
    return {
        "ready": True,
        "at_ms": bar.t + WIDTH,
        "checks": checks,
        "trigger": all(checks.values()),
        "facts": {
            "profit_r": encode(profit_r),
            "relative_volume": encode(rv),
            "rejection_wick": encode(rejection),
            "directional_close": encode(clv),
        },
    }


def funnel(bars, entry, *, exits=None, end_ms=None):
    exits = exits or ExitPolicy()
    individual, cumulative, first_failure = Counter(), Counter(), Counter()
    observations = []
    for i, bar in enumerate(bars):
        # Only FULL bars contained within the observed holding window.
        if bar.t < entry["opened_at_ms"] or (
            end_ms is not None and bar.t + WIDTH > end_ms
        ):
            continue
        row = conditions(bars[max(0, i - exits.volume_bars) : i + 1], entry, exits)
        if not row["ready"]:
            first_failure[row["reason"]] += 1
            continue
        observations.append(row)
        alive = True
        for stage in STAGES:
            if row["checks"][stage]:
                individual[stage] += 1
            if alive and row["checks"][stage]:
                cumulative[stage] += 1
            elif alive:
                first_failure[stage] += 1
                alive = False
    return {
        "closed_bars_evaluated": len(observations),
        "stage_order": list(STAGES),
        "individual_pass": {s: individual[s] for s in STAGES},
        "cumulative_pass": {s: cumulative[s] for s in STAGES},
        "first_failure": dict(first_failure),
        "trigger_count": sum(o["trigger"] for o in observations),
        "observations": observations,
        "interpretation": "BAR_COUNTS_NOT_INDEPENDENT_TRADE_SAMPLES",
    }


def entry_from_facts(facts):
    if (
        facts["source"] not in ("s3", "tv_bridge")
        or facts["environment"] != "SANDBOX"
        or facts["side"] not in ("BUY", "SELL")
    ):
        raise ValueError("SHADOW_FACT_SCOPE")
    if facts["maintenance"]:
        raise ValueError("SHADOW_MAINTENANCE_EXCLUDED")
    fills = facts["fills"]
    if (
        not fills
        or len(fills) > 1000
        or len({f["identity"] for f in fills}) != len(fills)
    ):
        raise ValueError("SHADOW_FILL_BOUND_OR_DUPLICATE")
    if any(
        f["leg"] not in ("OPEN", "CLOSE")
        or type(f["t"]) is not int
        or f["t"] < 0
        or decimal(f["quantity"]) <= 0
        or decimal(f["price"]) <= 0
        for f in fills
    ):
        raise ValueError("SHADOW_FILL_INVALID")
    opens = [f for f in fills if f["leg"] == "OPEN"]
    if not opens or not facts["opening_terminal"]:
        raise ValueError("SHADOW_OPENING_NOT_COMPLETE")
    last_open = max(f["t"] for f in opens)
    if any(f["t"] <= last_open for f in fills if f["leg"] == "CLOSE"):
        raise ValueError("SHADOW_INTERLEAVED_FILLS")
    if any(f["fee_currency"] != "USDT" for f in opens):
        raise ValueError("SHADOW_OPEN_FEE_CURRENCY_UNKNOWN")
    with localcontext() as ctx:
        ctx.prec = 60
        qty = sum((decimal(f["quantity"]) for f in opens), Decimal(0))
        notional = sum(
            (decimal(f["quantity"]) * decimal(f["price"]) for f in opens), Decimal(0)
        )
        stop = decimal(facts["stop_price"])
        direction = 1 if facts["side"] == "BUY" else -1
        if stop <= 0 or direction * (notional - qty * stop) <= 0:
            raise ValueError("SHADOW_STOP_NOT_ON_LOSS_SIDE")
        price = notional / qty
        entry = {
            "side": facts["side"],
            "opened_at_ms": last_open,
            "first_fill_at_ms": min(f["t"] for f in opens),
            "entry_price": encode(price),
            "quantity": encode(qty),
            "initial_stop": encode(stop),
            "signal_at_ms": facts["decided_ms"],
            "signal_digest": facts["evidence_ref"],
        }
        return entry, encode(sum((decimal(f["fee"]) for f in opens), Decimal(0)))


def analyze(facts, bars, *, policy=None, exits=None, params=None):
    policy, exits, p = (
        policy or ReplayPolicy(),
        exits or ExitPolicy(),
        parameters(params),
    )
    entry, opening_fee = entry_from_facts(facts)
    safe_start = (entry["opened_at_ms"] + WIDTH - 1) // WIDTH * WIDTH
    if any(f["leg"] == "CLOSE" and f["t"] <= safe_start for f in facts["fills"]):
        raise ValueError("SHADOW_CLOSE_BEFORE_SAFE_WINDOW")
    expected_start = safe_start - warmup(p) * WIDTH
    expected_end = safe_start + (policy.max_hold_bars + 1) * WIDTH
    if (
        not bars
        or len(bars) > 1000
        or any(not isinstance(b, Bar) or b.t % WIDTH for b in bars)
        or any(b.t != bars[0].t + i * WIDTH for i, b in enumerate(bars))
    ):
        raise ValueError("SHADOW_HISTORY_GAP")
    if bars[0].t != expected_start or bars[-1].t + WIDTH != expected_end:
        raise ValueError("SHADOW_HISTORY_BOUNDARY")
    entry["target"] = encode(
        decimal(entry["entry_price"])
        + (1 if entry["side"] == "BUY" else -1)
        * abs(decimal(entry["entry_price"]) - decimal(entry["initial_stop"]))
        * decimal(p["target_r_multiple"])
    )
    if decimal(entry["target"]) <= 0:
        raise ValueError("SHADOW_TARGET_INVALID")
    frames = [
        feature_frame(
            bars[max(0, i + 1 - warmup(p)) : i + 1], timeframe_minutes=5, params=p
        )
        if i + 1 >= warmup(p)
        else None
        for i in range(len(bars))
    ]
    with localcontext() as ctx:
        ctx.prec = 50
        variants = {
            v: _episode(
                bars,
                frames,
                entry,
                variant=v,
                policy=policy,
                exits=exits,
                params=p,
                width=WIDTH,
                opening_fee=opening_fee,
            )
            for v in VARIANTS
        }
    baseline = variants["baseline"]
    # Counterfactual trigger audit until baseline exit; exit bar excluded because
    # intrabar execution may predate its close. Timeout is at the bar open.
    baseline_end = baseline["closed_bar_at_ms"] or expected_end
    first_close = min(
        (f["t"] for f in facts["fills"] if f["leg"] == "CLOSE"), default=expected_end
    )
    for result in variants.values():
        both = result["status"] == baseline["status"] == "CLOSED"
        result["paired_net_delta"] = (
            encode(decimal(result["net_realized"]) - decimal(baseline["net_realized"]))
            if both
            else None
        )
        result["release_eligible"] = False
    return {
        "version": VERSION,
        "execution_authorized": False,
        "publication": "SHADOW_ONLY",
        "entry": entry,
        "opening_fee_usdt": opening_fee,
        "variants": variants,
        "baseline_funnel": funnel(bars, entry, exits=exits, end_ms=baseline_end),
        "actual_full_inventory_funnel": funnel(
            bars, entry, exits=exits, end_ms=min(first_close, expected_end)
        ),
        "safe_analysis_start_ms": safe_start,
        "opening_boundary_excluded_ms": safe_start - entry["opened_at_ms"],
        "history_end_ms": expected_end,
        "entry_digest": digest(canonical(entry)),
        "input_digest": digest(
            canonical({"facts": facts, "bars": [b.payload() for b in bars]})
        ),
        "facts": facts,
        "policy": asdict(policy),
        "exit_policy": asdict(exits),
        "parameters": p,
        "limitations": [
            "ACTUAL_FILL_ANCHOR_RESEARCH_EXITS_NOT_PRODUCTION_EXIT_REPLAY",
            "OPENING_BOUNDARY_UNOBSERVED_NO_ASSUMED_SURVIVAL_PROOF",
            "MULTIFILL_ENTRY_EXPOSURE_BEFORE_LAST_FILL_NOT_SIMULATED",
            "PLANNED_INITIAL_STOP_NOT_VENUE_PROTECTION_CONFIRMATION",
            "HISTORICAL_FILTERS_AND_FUNDING_UNAVAILABLE_NO_RELEASE_ELIGIBILITY",
            "ACTUAL_OPENING_FEES_EXIT_COSTS_ASSUMED_ACCOUNT_SETTLEMENT_NOT_DIRECTLY_COMPARABLE",
            "ACTUAL_HOLDING_FUNNEL_STOPS_AT_FIRST_PARTIAL_CLOSE_OR_FIXED_HORIZON",
            "ARCHIVE_ARRIVAL_TIME_UNKNOWN_NOT_PROOF_OF_REALTIME_ACTIONABILITY",
            "NO_PORTFOLIO_CAPITAL_OR_REENTRY_SIMULATION_NO_PROFITABILITY_APPROVAL",
        ],
    }
