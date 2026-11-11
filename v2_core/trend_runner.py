"""Causal residual-position comparison, with no execution or I/O authority.

The same actually filled residual is replayed through independent short-wave
and hourly-trend exits. This is a counterfactual model, NOT actual realized PnL.
Unknown historical availability is usable only in explicitly retrospective
research and can never become evidence of a timely trading opportunity.
"""

import re
from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar, decimal, encode, smoothed, standard_series

HOUR = 3_600_000
VERSION = "residual-trend-shadow-v1"
MAX_BARS = 2048


@dataclass(frozen=True)
class TrendRunnerPolicy:
    mode: str = "OFF"
    atr_period: int = 14
    fast_period: int = 8
    slow_period: int = 24
    structure_period: int = 6
    atr_multiple: str = "3"
    baseline_trigger_fraction: str = "0.03"
    baseline_trail_fraction: str = "0.02"
    max_hold_hours: int = 168
    max_stale_hours: int = 2
    entry_evidence_max_age_hours: int = 6
    fee_rate: str = "0.0005"
    exit_slippage_fraction: str = "0.0005"

    def __post_init__(self):
        if self.mode not in {"OFF", "SHADOW"}:
            raise ValueError("TREND_RUNNER_NO_LIVE_AUTHORITY")
        for key in (
            "atr_period",
            "fast_period",
            "slow_period",
            "structure_period",
            "max_hold_hours",
            "max_stale_hours",
            "entry_evidence_max_age_hours",
        ):
            if type(getattr(self, key)) is not int:
                raise ValueError("TREND_RUNNER_POLICY_INTEGER")
        if not (
            2 <= self.atr_period <= 200
            and 2 <= self.fast_period < self.slow_period <= 500
            and 1 <= self.structure_period <= 200
            and 1 <= self.max_hold_hours <= 720
            and 1 <= self.max_stale_hours <= 6
            and 1 <= self.entry_evidence_max_age_hours <= 24
        ):
            raise ValueError("TREND_RUNNER_POLICY_BOUNDS")
        for key in (
            "atr_multiple",
            "baseline_trigger_fraction",
            "baseline_trail_fraction",
            "fee_rate",
            "exit_slippage_fraction",
        ):
            if type(getattr(self, key)) is not str:
                raise ValueError("TREND_RUNNER_POLICY_DECIMAL_STRING")
        if not (
            0 < decimal(self.atr_multiple) <= 20
            and 0 < decimal(self.baseline_trigger_fraction) <= 1
            and 0 < decimal(self.baseline_trail_fraction) <= 1
            and 0 <= decimal(self.fee_rate) <= Decimal("0.01")
            and 0 <= decimal(self.exit_slippage_fraction) <= Decimal("0.1")
        ):
            raise ValueError("TREND_RUNNER_POLICY_DECIMAL_BOUNDS")


class _Unavailable(ValueError):
    pass


def _time(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("TREND_RUNNER_TIME_INVALID")
    return value


def _money(value):
    if type(value) is not str:
        raise ValueError("TREND_RUNNER_DECIMAL_STRING_REQUIRED")
    result = decimal(value)
    if result <= 0:
        raise ValueError("TREND_RUNNER_POSITIVE_REQUIRED")
    return result


def _bounded_json_input(payload):
    """Check shallow shapes before canonicalization; never traverse arbitrary JSON."""
    list_bounds = {
        "history": MAX_BARS,
        "forward": MAX_BARS,
        "funding": MAX_BARS,
        "entry_fills": 100,
        "reduction_fills": 100,
    }

    def scalar(value):
        if value is not None and type(value) not in (str, int, bool):
            raise ValueError("TREND_RUNNER_SCALAR_REQUIRED")
        if isinstance(value, str) and len(value) > 128:
            raise ValueError("TREND_RUNNER_SCALAR_BOUND")

    def record(value):
        if not isinstance(value, dict) or len(value) > 8:
            raise ValueError("TREND_RUNNER_RECORD_BOUND")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 64:
                raise ValueError("TREND_RUNNER_RECORD_KEY")
            scalar(item)

    for key, value in payload.items():
        if key in list_bounds:
            if not isinstance(value, list) or len(value) > list_bounds[key]:
                raise ValueError("TREND_RUNNER_COLLECTION_BOUND")
            for item in value:
                record(item)
        elif key in {"native_stop", "entry_evidence"}:
            if value is not None:
                record(value)
        else:
            scalar(value)


def _bars(rows, *, boundary, timely):
    if not isinstance(rows, list) or len(rows) > MAX_BARS:
        raise ValueError("TREND_RUNNER_HISTORY_BOUND")
    parsed, previous_available = [], None
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "t",
            "o",
            "h",
            "l",
            "c",
            "v",
            "available_at_ms",
        }:
            raise ValueError("TREND_RUNNER_BAR_FIELDS")
        bar = Bar(**{key: row[key] for key in ("t", "o", "h", "l", "c", "v")})
        if bar.t % HOUR or bar.t + HOUR > boundary:
            raise _Unavailable("UNALIGNED_OR_UNCLOSED_HOURLY_BAR")
        if parsed and bar.t != parsed[-1][0].t + HOUR:
            raise _Unavailable("HOURLY_HISTORY_GAP")
        available = row["available_at_ms"]
        if available is None:
            if timely:
                raise _Unavailable("BAR_AVAILABILITY_NOT_PROVEN")
            # Explicit retrospective convention, never a stored observation.
            available = bar.t + HOUR
        else:
            _time(available)
            if available < bar.t + HOUR:
                raise _Unavailable("BAR_AVAILABLE_BEFORE_CLOSE")
            if available > boundary:
                raise _Unavailable("BAR_NOT_AVAILABLE_AS_OF")
        if previous_available is not None and available < previous_available:
            raise _Unavailable("BAR_AVAILABILITY_OUT_OF_ORDER")
        previous_available = available
        parsed.append((bar, available))
    return parsed


def _fills(rows, *, activation):
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("TREND_RUNNER_FILL_BOUND")
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"at_ms", "quantity", "price"}:
            raise ValueError("TREND_RUNNER_FILL_FIELDS")
        at = _time(row["at_ms"])
        if at > activation or parsed and at < parsed[-1][0]:
            raise _Unavailable("FILL_NOT_AVAILABLE_AT_ACTIVATION")
        parsed.append((at, _money(row["quantity"]), _money(row["price"])))
    return parsed


def _tighten(side, old, candidate):
    return max(old, candidate) if side == "BUY" else min(old, candidate)


def _trend_stop(side, bars, atr, policy):
    close = bars[-1].c
    recent = bars[-policy.structure_period :]
    distance = atr * decimal(policy.atr_multiple)
    # The closer protective boundary wins; never widen the original stop.
    return (
        max(close - distance, min(b.l for b in recent))
        if side == "BUY"
        else min(close + distance, max(b.h for b in recent))
    )


def _wave_stop(side, entry, peak, old, policy):
    favorable = (peak / entry - 1) if side == "BUY" else (1 - peak / entry)
    if favorable < decimal(policy.baseline_trigger_fraction):
        return old
    # Percentage points relative to original fill, not a leveraged ROI.
    distance = entry * decimal(policy.baseline_trail_fraction)
    return _tighten(side, old, peak - distance if side == "BUY" else peak + distance)


def run_shadow(payload, *, policy=None):
    """Replay a bounded JSON payload; all prices/quantities are decimal strings.

    Required: side, evidence_mode, activation_at_ms, as_of_ms, entry_fills,
    reduction_fills, native_stop, entry_evidence, history, forward, funding,
    funding_complete. Activation is an aligned closed-hour decision boundary.
    A historical fill after that boundary is not silently moved backwards.

    TIMELY requires frozen pre-entry directional evidence and timestamped native
    protection. RETROSPECTIVE allows missing availability/proof but is permanently
    NOT_PROVEN. Both arms are simulations from activation onward, not the actual
    baseline strategy. Fees are assumptions; entry slippage is already in fills.
    """
    policy = TrendRunnerPolicy() if policy is None else policy
    if not isinstance(policy, TrendRunnerPolicy):
        raise TypeError("TREND_RUNNER_POLICY_TYPE")
    result = {
        "version": VERSION,
        "policy": asdict(policy),
        "policy_digest": digest(canonical(asdict(policy))),
        "execution_authorized": False,
        "profitability_valid": False,
        "actual_pnl": False,
        "status": "OFF" if policy.mode == "OFF" else "UNAVAILABLE",
    }
    if policy.mode == "OFF":
        return result
    if not isinstance(payload, dict):
        raise TypeError("TREND_RUNNER_PAYLOAD")
    required = {
        "side",
        "evidence_mode",
        "activation_at_ms",
        "as_of_ms",
        "entry_fills",
        "reduction_fills",
        "native_stop",
        "entry_evidence",
        "history",
        "forward",
        "funding",
        "funding_complete",
    }
    if set(payload) != required:
        raise ValueError("TREND_RUNNER_PAYLOAD_FIELDS")
    _bounded_json_input(payload)
    if payload["evidence_mode"] not in {"TIMELY", "RETROSPECTIVE"}:
        raise ValueError("TREND_RUNNER_EVIDENCE_MODE")
    result.update(
        {
            "evidence_mode": payload["evidence_mode"],
            "timeliness": "NOT_PROVEN",
            "input_digest": digest(canonical(payload)),
            "costs": {
                "fee_rate_assumed": policy.fee_rate,
                "exit_slippage_fraction_assumed": policy.exit_slippage_fraction,
                "actual_fee_schedule_verified": False,
                "funding_complete": payload["funding_complete"],
                "funding_convention": "ambiguous intrahour debits included; credits omitted",
            },
            "comparison_scope": "same residual only; prior reductions and portfolio effects excluded",
        }
    )
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            return _run(payload, policy, result)
    except _Unavailable as exc:
        result.update(status="UNAVAILABLE", reason=str(exc))
        return result


def _run(payload, policy, result):
    side = payload["side"]
    if side not in {"BUY", "SELL"} or type(payload["funding_complete"]) is not bool:
        raise ValueError("TREND_RUNNER_SIDE_OR_FUNDING")
    sign = Decimal(1) if side == "BUY" else Decimal(-1)
    activation, boundary = map(
        _time, (payload["activation_at_ms"], payload["as_of_ms"])
    )
    if activation % HOUR or boundary < activation:
        raise _Unavailable("ACTIVATION_NOT_CLOSED_HOUR_BOUNDARY")
    timely = payload["evidence_mode"] == "TIMELY"
    entries = _fills(payload["entry_fills"], activation=activation)
    reductions = _fills(payload["reduction_fills"], activation=activation)
    if not entries:
        raise _Unavailable("NO_ACTUAL_ENTRY_FILLS")
    if reductions and reductions[0][0] < entries[-1][0]:
        raise _Unavailable("INTERLEAVED_SCALE_IN_ALLOCATION_UNSUPPORTED")
    filled = sum((row[1] for row in entries), Decimal(0))
    reduced = sum((row[1] for row in reductions), Decimal(0))
    if not 0 <= reduced < filled:
        raise _Unavailable("NO_POSITIVE_FILLED_RESIDUAL")
    qty = filled - reduced
    entry = sum((row[1] * row[2] for row in entries), Decimal(0)) / filled
    evidence = payload["entry_evidence"]
    proof = evidence is not None
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) != {
            "direction",
            "observed_at_ms",
            "available_at_ms",
            "digest",
        }:
            raise ValueError("TREND_RUNNER_ENTRY_EVIDENCE_FIELDS")
        observed, available = map(
            _time, (evidence["observed_at_ms"], evidence["available_at_ms"])
        )
        if not 0 <= observed <= available <= entries[0][0]:
            raise _Unavailable("ENTRY_EVIDENCE_NOT_PRE_ENTRY")
        if evidence["direction"] != side:
            raise _Unavailable("ENTRY_EVIDENCE_DIRECTION_CONFLICT")
        if not isinstance(evidence["digest"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", evidence["digest"]
        ):
            raise ValueError("TREND_RUNNER_ENTRY_EVIDENCE_DIGEST")
        if entries[0][0] - observed > policy.entry_evidence_max_age_hours * HOUR:
            raise _Unavailable("ENTRY_EVIDENCE_STALE")
    if timely and not proof:
        raise _Unavailable("FROZEN_PRE_ENTRY_EVIDENCE_MISSING")
    native = payload["native_stop"]
    if (
        not isinstance(native, dict)
        or not {"price", "confirmed_at_ms", "available_at_ms"} <= set(native)
        or set(native)
        - {"price", "confirmed_at_ms", "available_at_ms", "protected_quantity"}
    ):
        raise ValueError("TREND_RUNNER_NATIVE_STOP_FIELDS")
    hard_stop = _money(native["price"])
    if sign * (entry - hard_stop) <= 0:
        raise _Unavailable("ORIGINAL_STOP_NOT_LOSS_BOUNDARY")
    native_timestamp_validated = (
        native["confirmed_at_ms"] is not None and native["available_at_ms"] is not None
    )
    if native_timestamp_validated:
        confirmed, available = map(
            _time, (native["confirmed_at_ms"], native["available_at_ms"])
        )
        if not entries[0][0] <= confirmed <= available <= activation:
            raise _Unavailable("NATIVE_STOP_NOT_CONFIRMED_AT_ACTIVATION")
    elif timely:
        raise _Unavailable("NATIVE_PROTECTION_NOT_PROVEN")
    protected_qty = native.get("protected_quantity")
    if protected_qty is not None and _money(protected_qty) < qty:
        raise _Unavailable("NATIVE_PROTECTION_RESIDUAL_NOT_COVERED")
    if timely and protected_qty is None:
        raise _Unavailable("NATIVE_PROTECTION_QUANTITY_NOT_PROVEN")
    native_proven = native_timestamp_validated and protected_qty is not None
    history = _bars(payload["history"], boundary=activation, timely=timely)
    needed = max(policy.slow_period, policy.atr_period + 1, policy.structure_period)
    if len(history) < needed:
        raise _Unavailable("HOURLY_TREND_WARMUP")
    if history[-1][0].t + HOUR != activation:
        raise _Unavailable("ACTIVATION_HISTORY_STALE")
    bars = [row[0] for row in history]
    forward = _bars(payload["forward"], boundary=boundary, timely=timely)
    if forward and forward[0][0].t != activation:
        raise _Unavailable("FORWARD_HISTORY_GAP")
    if activation - entries[0][0] >= policy.max_hold_hours * HOUR:
        raise _Unavailable("POSITION_ALREADY_PAST_MAX_HOLD")
    fast = smoothed([b.c for b in bars], policy.fast_period)[-1]
    slow = smoothed([b.c for b in bars], policy.slow_period)[-1]
    atr = standard_series(bars, policy.atr_period)["atr"][-1]
    last = bars[-1].c
    if sign * (last - hard_stop) <= 0:
        raise _Unavailable("ORIGINAL_STOP_ALREADY_BREACHED")
    if not (sign * (fast - slow) > 0 and sign * (last - fast) > 0 and atr > 0):
        result.update(status="INELIGIBLE", reason="ACTIVATION_TREND_NOT_CONFIRMED")
        return result
    funding = payload["funding"]
    if not isinstance(funding, list) or len(funding) > MAX_BARS:
        raise ValueError("TREND_RUNNER_FUNDING_BOUND")
    funding_by_hour, seen = {}, set()
    for event in funding:
        if not isinstance(event, dict) or set(event) != {"at_ms", "rate", "mark_price"}:
            raise ValueError("TREND_RUNNER_FUNDING_FIELDS")
        at = _time(event["at_ms"])
        if at in seen or not activation <= at < boundary:
            raise ValueError("TREND_RUNNER_FUNDING_TIME")
        if type(event["rate"]) is not str or abs(decimal(event["rate"])) > 1:
            raise ValueError("TREND_RUNNER_FUNDING_RATE")
        mark = _money(event["mark_price"])
        seen.add(at)
        funding_by_hour.setdefault(at // HOUR * HOUR, []).append(
            (at, -sign * qty * mark * decimal(event["rate"]))
        )
    peak = max(entry, last) if side == "BUY" else min(entry, last)
    runner_stop = _tighten(side, hard_stop, _trend_stop(side, bars, atr, policy))
    wave_stop = _wave_stop(side, entry, peak, hard_stop, policy)
    if sign * (last - runner_stop) <= 0 or sign * (last - wave_stop) <= 0:
        raise _Unavailable("ACTIVATION_STOP_ALREADY_BREACHED")
    states = {
        "baseline_wave": _state(wave_stop, peak, activation),
        "proposed_runner": _state(runner_stop, peak, activation),
    }
    result.update(
        {
            "status": "SHADOW",
            "timeliness": "TIMESTAMP_VALIDATED" if timely else "NOT_PROVEN",
            "native_protection_proven": native_proven,
            "native_protection_proof_scope": "caller-supplied timestamps and quantity; no venue verification performed",
            "side": side,
            "activation_at_ms": activation,
            "filled_quantity": encode(filled),
            "reduced_quantity": encode(reduced),
            "residual_quantity": encode(qty),
            "entry_price": encode(entry),
            "original_hard_stop": encode(hard_stop),
            "activation_evidence": {
                "last_closed_bar_ms": bars[-1].t + HOUR,
                "fast_ema": encode(fast),
                "slow_ema": encode(slow),
                "atr": encode(atr),
                "frozen_input_digest": digest(
                    canonical(
                        {
                            "history": payload["history"],
                            "entry_evidence": evidence,
                            "native_stop": native,
                            "activation_at_ms": activation,
                        }
                    )
                ),
            },
        }
    )
    deadline = entries[0][0] + policy.max_hold_hours * HOUR
    # Indicator series are causal recurrences. Compute once for bounded linear
    # work; select only the current prefix value during the event replay.
    atr_values = standard_series(bars + [b for b, _ in forward], policy.atr_period)[
        "atr"
    ]
    history_count = len(bars)
    for forward_index, (bar, available) in enumerate(forward):
        for name, state in states.items():
            if state["status"] != "OPEN":
                continue
            # Every closed candle retains its own availability event. A later
            # observation must not overwrite one that is still in flight.
            # _bars validates nondecreasing availability and MAX_BARS bounds
            # the entire queue; each event is activated at most once.
            while state["pending"] and state["pending"][0]["available"] <= bar.t:
                pending = state["pending"].popleft()
                state["stop"] = _tighten(side, state["stop"], pending["stop"])
                state["evidence_close_ms"] = pending["closed"]
            before = state["stop"]
            gap = sign * (bar.o - before) <= 0
            touch = bar.l <= before if side == "BUY" else bar.h >= before
            reason, reference = None, None
            if gap:
                reason, reference = "STOP_GAP", bar.o
            elif bar.t >= deadline:
                reason, reference = "MAX_HOLD", bar.o
            elif bar.t - state["evidence_close_ms"] >= policy.max_stale_hours * HOUR:
                reason, reference = "STALE_DATA", bar.o
            elif bar.t < deadline < bar.t + HOUR:
                # Hourly OHLC cannot price an exact intrahour time exit or order
                # that exit against an intrahour stop. Require an exit without
                # inventing either price or realized PnL.
                state["status"] = "EXIT_REQUIRED_MAX_HOLD"
                state["trace"].append(
                    {
                        "bar_open_ms": bar.t,
                        "stop_effective": encode(before),
                        "status": state["status"],
                        "reason": "INTRAHOUR_DEADLINE_PRICE_UNKNOWN",
                    }
                )
                continue
            elif touch:
                reason, reference = "TRAIL_STOP", before
            for at, cash in funding_by_hour.get(bar.t, ()):
                # OHLC cannot establish ordering of intrahour funding vs stop.
                if reason in {"STOP_GAP", "MAX_HOLD", "STALE_DATA"} and at > bar.t:
                    continue
                if reason is None or cash < 0 or at == bar.t and reason == "TRAIL_STOP":
                    state["funding"] += cash
                else:
                    state["ambiguous_funding_credits_omitted"] += 1
            if reason:
                _close(state, reference, reason, bar, side, entry, qty, policy)
            state["trace"].append(
                {
                    "bar_open_ms": bar.t,
                    "stop_effective": encode(before),
                    "status": state["status"],
                    "reason": reason,
                }
            )
        bars.append(bar)
        # A closed candle may improve only NEXT bar's stop. Its high/low can
        # never improve the stop which was tested inside that same candle.
        atr = atr_values[history_count + forward_index]
        for name, state in states.items():
            if state["status"] != "OPEN":
                continue
            state["peak"] = (
                max(state["peak"], bar.h)
                if side == "BUY"
                else min(state["peak"], bar.l)
            )
            candidate = (
                _trend_stop(side, bars, atr, policy)
                if name == "proposed_runner"
                else _wave_stop(side, entry, state["peak"], state["stop"], policy)
            )
            state["pending"].append(
                {
                    "stop": candidate,
                    "available": available,
                    "closed": bar.t + HOUR,
                }
            )
            state["last_bar_close_ms"] = bar.t + HOUR
    latest_close = forward[-1][0].t + HOUR if forward else activation
    for state in states.values():
        if state["status"] == "OPEN":
            if boundary >= deadline:
                state["status"] = "EXIT_REQUIRED_MAX_HOLD"
            elif boundary - latest_close >= policy.max_stale_hours * HOUR:
                state["status"] = "EXIT_REQUIRED_DATA_STALE"
            # No fresh executable price: do not fabricate a successful exit.
    result["arms"] = {
        name: _public_state(state, funding_complete=payload["funding_complete"])
        for name, state in states.items()
    }
    baseline, proposed = states.values()
    result["modeled_net_delta_with_reported_funding"] = (
        encode(proposed["net"] - baseline["net"])
        if baseline["status"] == proposed["status"] == "CLOSED"
        and payload["funding_complete"]
        else None
    )
    result["exit_comparison_complete"] = (
        baseline["status"] == proposed["status"] == "CLOSED"
    )
    result["cost_comparison_complete"] = (
        result["exit_comparison_complete"] and payload["funding_complete"]
    )
    result["comparison_complete"] = result["cost_comparison_complete"]
    result["actual_cost_comparison_verified"] = False
    result["processed_forward_bars"] = len(forward)
    return result


def _state(stop, peak, activation):
    return {
        "status": "OPEN",
        "stop": stop,
        "peak": peak,
        "funding": Decimal(0),
        "pending": deque(),
        "trace": [],
        "evidence_close_ms": activation,
        "last_bar_close_ms": activation,
        "ambiguous_funding_credits_omitted": 0,
    }


def _close(state, reference, reason, bar, side, entry, qty, policy):
    sign = Decimal(1) if side == "BUY" else Decimal(-1)
    price = reference * (1 - sign * decimal(policy.exit_slippage_fraction))
    fees = (entry + price) * qty * decimal(policy.fee_rate)
    gross = sign * (price - entry) * qty
    state.update(
        {
            "status": "CLOSED",
            "exit_reason": reason,
            "exit_bar_open_ms": bar.t,
            "exit_time_precision": "hour_open"
            if reason != "TRAIL_STOP"
            else "within_hour_unknown",
            "exit_price": price,
            "gross": gross,
            "modeled_fees": fees,
            "net": gross - fees + state["funding"],
            "pending": deque(),
        }
    )


def _public_state(state, *, funding_complete):
    mapping = {
        "net": "modeled_net_with_reported_funding",
        "gross": "modeled_gross",
        "funding": "reported_funding_cashflow",
        "stop": "last_effective_stop",
    }
    result = {
        mapping.get(key, key): encode(value) if isinstance(value, Decimal) else value
        for key, value in state.items()
        if key not in {"pending", "peak", "evidence_close_ms", "last_bar_close_ms"}
    }
    result["funding_completeness"] = (
        "COMPLETE" if funding_complete else "PARTIAL_NOT_TOTAL"
    )
    if state["status"] == "CLOSED":
        result["modeled_after_fees_before_funding"] = encode(
            state["gross"] - state["modeled_fees"]
        )
        if not funding_complete:
            result["modeled_net_with_reported_funding"] = None
    return result


@dataclass(frozen=True)
class StagnationShadowPolicy:
    """Research-only bounds, not new defaults for the executing exit policy."""

    mode: str = "OFF"
    max_extension_minutes: int = 60
    max_total_hold_minutes: int = 240
    max_observation_age_ms: int = 10000
    max_snapshot_gap_ms: int = 120000
    fast_period: int = 8
    slow_period: int = 24
    structure_period: int = 6

    def __post_init__(self):
        if self.mode not in {"OFF", "SHADOW"}:
            raise ValueError("STAGNATION_SHADOW_NO_LIVE_AUTHORITY")
        for name, lower, upper in (
            ("max_extension_minutes", 1, 240),
            ("max_total_hold_minutes", 1, 1440),
            ("max_observation_age_ms", 1, 30000),
            ("max_snapshot_gap_ms", 1000, 300000),
            ("fast_period", 2, 71),
            ("slow_period", 3, 72),
            ("structure_period", 1, 72),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("STAGNATION_SHADOW_POLICY_BOUNDS")
        if self.fast_period >= self.slow_period:
            raise ValueError("STAGNATION_SHADOW_PERIOD_ORDER")


def stagnation_shadow(payload, *, policy=None):
    """Qualify a bounded extension from the first supplied frozen stagnation row.

    This is an eligibility trace, NOT a fill/PnL simulation or an exit instruction.
    Each snapshot must have a complete frozen baseline policy, actual ExitFacts,
    the saved baseline decision, timely observation/protection and closed hourly
    bars. Missing proof cannot be repaired by retrospective historical downloads.
    The deadline never moves when another LOW_YIELD_STAGNATION row arrives.
    The collector must separately establish whether the first supplied row was
    the first historical stagnation event; this pure function cannot prove it.
    """
    policy = StagnationShadowPolicy() if policy is None else policy
    if not isinstance(policy, StagnationShadowPolicy):
        raise TypeError("STAGNATION_SHADOW_POLICY_TYPE")
    result = {
        "version": "stagnation-continuation-shadow-v1",
        "status": "OFF" if policy.mode == "OFF" else "UNAVAILABLE",
        "policy": asdict(policy),
        "policy_digest": digest(canonical(asdict(policy))),
        "execution_authorized": False,
        "promotion_authorized": False,
        "profitability_valid": False,
        "actual_pnl": False,
        "baseline_code_identity_proven": False,
        "first_historical_stagnation_proven": False,
        "baseline_reproduced_count": 0,
        "continuation_eligibility": "UNKNOWN",
        "comparison_scope": "frozen-input current-code qualification; no counterfactual fills or PnL",
        "trace": [],
    }
    if policy.mode == "OFF":
        return result
    if not isinstance(payload, dict) or set(payload) != {"opened_at_ms", "snapshots"}:
        raise ValueError("STAGNATION_SHADOW_FIELDS")
    opened = _time(payload["opened_at_ms"])
    snapshots = payload["snapshots"]
    if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 256:
        raise ValueError("STAGNATION_SHADOW_SNAPSHOT_BOUND")
    try:
        return _stagnation_trace(payload, opened, snapshots, policy, result)
    except _Unavailable as exc:
        result.update(status="UNAVAILABLE", reason=str(exc))
        if result["trace"]:
            result["trace"][-1].setdefault("reason", str(exc))
        return result


def _stagnation_snapshot(raw, opened, policy):
    from v2_core.directional_exit import ExitFacts, evaluate_directional_exit
    from v2_core.runtime_policy import SCHEMA, resolve

    if not isinstance(raw, dict) or set(raw) != {
        "at_ms",
        "facts",
        "baseline",
        "baseline_policy",
        "observation",
        "native_protection",
        "hourly_history",
    }:
        raise ValueError("STAGNATION_SHADOW_SNAPSHOT_FIELDS")
    at = _time(raw["at_ms"])
    values = raw["baseline_policy"]
    # Never fill today's defaults into a historical policy snapshot.
    if not isinstance(values, dict) or set(values) != set(SCHEMA):
        raise _Unavailable("FROZEN_BASELINE_POLICY_INCOMPLETE")
    values = resolve(values)
    encoded = raw["facts"]
    if not isinstance(encoded, dict) or set(encoded) != set(
        ExitFacts.__dataclass_fields__
    ):
        raise ValueError("STAGNATION_SHADOW_FACTS_FIELDS")
    closes = encoded["momentum_closes_15m"]
    if not isinstance(closes, list) or len(closes) != 4:
        raise ValueError("STAGNATION_SHADOW_MOMENTUM_FIELDS")
    if any(type(v) is not str or len(v) > 128 for v in closes):
        raise ValueError("STAGNATION_SHADOW_DECIMAL_BOUND")
    for name, value in encoded.items():
        if name not in {"momentum_closes_15m", "partial_done", "held_ms"} and (
            type(value) is not str or len(value) > 128
        ):
            raise ValueError("STAGNATION_SHADOW_SCALAR_BOUND")
    facts = ExitFacts(**{**encoded, "momentum_closes_15m": tuple(closes)})
    decision = evaluate_directional_exit(facts, policy=values)
    if raw["baseline"] != decision.evidence():
        raise _Unavailable("FROZEN_BASELINE_REPLAY_MISMATCH")
    if at < opened or facts.held_ms != at - opened:
        raise _Unavailable("ORIGINAL_HOLDING_CLOCK_MISMATCH")
    return at, facts, decision, values


def _stagnation_evidence(raw, at, facts, policy):
    observation = raw["observation"]
    if not isinstance(observation, dict) or set(observation) != {
        "observed_at_ms",
        "available_at_ms",
        "closed_15m_at_ms",
        "closed_1h_at_ms",
        "environment",
        "market_source",
    }:
        raise _Unavailable("FROZEN_OBSERVATION_PROOF_MISSING")
    if (observation["environment"], observation["market_source"]) != (
        "SANDBOX",
        "BINANCE_USDM_SANDBOX",
    ):
        raise _Unavailable("EXECUTION_MARKET_SOURCE_MISMATCH")
    if observation["observed_at_ms"] is None or observation["available_at_ms"] is None:
        raise _Unavailable("OBSERVATION_AVAILABILITY_NOT_PROVEN")
    observed, available = map(
        _time, (observation["observed_at_ms"], observation["available_at_ms"])
    )
    if (
        not observed <= available <= at
        or at - observed >= policy.max_observation_age_ms
    ):
        raise _Unavailable("EXIT_OBSERVATION_NOT_TIMELY")
    for width, field in ((900000, "closed_15m_at_ms"), (HOUR, "closed_1h_at_ms")):
        closed = _time(observation[field])
        if closed != observed // width * width:
            raise _Unavailable("CLOSED_EXIT_CANDLES_STALE_OR_FUTURE")
    native = raw["native_protection"]
    if not isinstance(native, dict) or set(native) != {
        "status",
        "price",
        "protected_quantity",
        "observed_at_ms",
        "available_at_ms",
    }:
        raise _Unavailable("FROZEN_NATIVE_PROTECTION_MISSING")
    if native["observed_at_ms"] is None or native["available_at_ms"] is None:
        raise _Unavailable("NATIVE_PROTECTION_AVAILABILITY_NOT_PROVEN")
    seen, known = map(_time, (native["observed_at_ms"], native["available_at_ms"]))
    if (
        native["status"] != "NEW"
        or not seen <= known <= at
        or at - seen >= policy.max_observation_age_ms
        or _money(native["price"]) != _money(facts.stop_price)
        or _money(native["protected_quantity"]) < _money(facts.remaining_quantity)
    ):
        raise _Unavailable("NATIVE_PROTECTION_NOT_CURRENT_AND_COVERED")
    if not isinstance(raw["hourly_history"], list) or len(raw["hourly_history"]) > 72:
        raise ValueError("STAGNATION_SHADOW_HISTORY_BOUND")
    history = _bars(raw["hourly_history"], boundary=at, timely=True)
    if (
        len(history) < max(policy.slow_period, policy.structure_period)
        or history[-1][0].t + HOUR != observation["closed_1h_at_ms"]
    ):
        raise _Unavailable("HOURLY_TREND_WARMUP_OR_STALE")
    # Same causal 8/24 first-close-seeded EMA hypothesis as residual runner;
    # intentionally NOT a claim that legacy exit EMA9/20 has this seed/contract.
    prices = [b.c for b, _ in history]
    fast = smoothed(prices, policy.fast_period)[-1]
    slow = smoothed(prices, policy.slow_period)[-1]
    sign = Decimal(1) if facts.side == "BUY" else Decimal(-1)
    closes = [decimal(c) for c in facts.momentum_closes_15m]
    mark = _money(facts.mark_price)
    aligned = (
        sign * (fast - slow) > 0
        and sign * (prices[-1] - fast) > 0
        and sign * (mark - fast) > 0
    )
    recent = [b for b, _ in history[-policy.structure_period :]]
    boundary = (
        min(b.l for b in recent) if facts.side == "BUY" else max(b.h for b in recent)
    )
    structure_intact = sign * (mark - boundary) > 0
    momentum = (
        sign * (closes[-1] - closes[-2]) >= 0
        and sign * (closes[-1] - sum(closes[:3], Decimal(0)) / 3) > 0
    )
    return aligned and momentum and structure_intact, {
        "fast_ema": encode(fast),
        "slow_ema": encode(slow),
        "hourly_aligned": aligned,
        "closed_hour_structure_boundary": encode(boundary),
        "closed_hour_structure_intact": structure_intact,
        "closed_15m_momentum_intact": momentum,
        "protection_proof_scope": "caller-supplied frozen query; no venue verification",
    }


def _stagnation_trace(payload, opened, snapshots, policy, result):
    from v2_core.directional_exit import evaluate_directional_exit
    from v2_core.runtime_policy import SCHEMA

    previous, deadline, anchor_identity = None, None, None
    for raw in snapshots:
        at, facts, baseline, values = _stagnation_snapshot(raw, opened, policy)
        result["baseline_reproduced_count"] += 1
        identity = (
            facts.system_tag,
            facts.side,
            facts.entry_price,
            facts.stop_price,
            facts.remaining_quantity,
            facts.planned_loss,
            facts.partial_done,
            digest(canonical(values)),
        )
        if previous is not None and at <= previous:
            raise _Unavailable("SNAPSHOT_ORDER_INVALID")
        if previous is None:
            if (baseline.action, baseline.reason) != ("CLOSE", "LOW_YIELD_STAGNATION"):
                result.update(status="NOT_APPLICABLE", reason="NOT_STAGNATION_EXIT")
                result["continuation_eligibility"] = "NOT_APPLICABLE"
                return result
            deadline = min(
                at + policy.max_extension_minutes * 60000,
                opened + policy.max_total_hold_minutes * 60000,
            )
            anchor_identity = identity
            result.update(anchor_at_ms=at, extension_deadline_ms=deadline)
        elif identity != anchor_identity:
            raise _Unavailable("POSITION_OR_BASELINE_POLICY_CHANGED")
        step = {
            "at_ms": at,
            "baseline_action": baseline.action,
            "baseline_reason": baseline.reason,
            "continuation_eligible": False,
            "baseline_reproduced": True,
            "baseline_policy_digest": digest(canonical(values)),
            "frozen_facts_digest": digest(canonical(raw["facts"])),
        }
        result["trace"].append(step)
        if baseline.reason != "LOW_YIELD_STAGNATION" and baseline.action != "WAIT":
            step["reason"] = "PRESERVE_BASELINE_PROTECTION_OR_EXIT"
        elif at >= deadline:
            step["reason"] = "FIXED_EXTENSION_DEADLINE"
        elif previous is not None and at - previous > policy.max_snapshot_gap_ms:
            raise _Unavailable("SNAPSHOT_COVERAGE_GAP")
        else:
            # LOW_YIELD_STAGNATION precedes peak/reversal/time exits in the
            # baseline. Removing that single gate must not hide a later exit.
            remainder = evaluate_directional_exit(
                facts,
                policy={
                    **values,
                    "exit.stagnation_minutes": SCHEMA["exit.stagnation_minutes"][2],
                },
            )
            if remainder.action != "WAIT":
                step.update(
                    reason="PRESERVE_OTHER_BASELINE_EXIT",
                    protected_baseline_reason=remainder.reason,
                )
                result.update(status="NO_CONTINUATION", reason=step["reason"])
                result["continuation_eligibility"] = "NOT_ELIGIBLE"
                return result
            intact, evidence = _stagnation_evidence(raw, at, facts, policy)
            step["evidence"] = evidence
            step["continuation_eligible"] = intact
            step["reason"] = (
                "TREND_CONTINUATION_CANDIDATE" if intact else "TREND_NOT_INTACT"
            )
        previous = at
        if not step["continuation_eligible"]:
            result.update(status="NO_CONTINUATION", reason=step["reason"])
            result["continuation_eligibility"] = "NOT_ELIGIBLE"
            return result
    # Prefix-only observations do not establish what would happen later.
    result.update(
        status="SHADOW_ELIGIBLE_PREFIX",
        reason="FORWARD_OUTCOME_NOT_PROVEN",
        input_digest=digest(canonical(payload)),
        continuation_eligibility="ELIGIBLE_OBSERVED_PREFIX_ONLY",
    )
    return result
