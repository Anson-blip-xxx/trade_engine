"""Pure prospective exit-research adapter; no I/O or order/policy authority.

Callers supply already acquired facts and their actual receipt times. Old bar
closes, order updateTime and this function's invocation time are never promoted
to historical availability. Partial evidence remains visible, not fabricated.
"""

from dataclasses import asdict
from uuid import UUID

from v2_core.account_risk import AccountScope
from v2_core.directional_exit import ExitDecision, ExitFacts, evaluate_directional_exit
from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar, decimal
from v2_core.runtime_policy import SCHEMA, resolve
from v2_core.state import StateKey
from v2_core.symbols import is_supported_symbol

VERSION = "prospective-exit-research-snapshot-v1"
HOUR = 3_600_000
_SOURCES = {"binance-public-exit-v1", "binance-public-exit-v2"}
_MARKET = "BINANCE_USDM_SANDBOX"


class _Unknown(ValueError):
    """Only fixed codes, never input values or provider exception text."""


def _stamp(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise _Unknown("INVALID_EVIDENCE_TIME")
    return value


def _money(value, *, positive=True):
    if type(value) is not str or len(value) > 128:
        raise _Unknown("INVALID_EVIDENCE_NUMBER")
    parsed = decimal(value)
    if positive and parsed <= 0:
        raise _Unknown("INVALID_EVIDENCE_NUMBER")
    return parsed


def _timely(record, at, max_age, code):
    seen, known = record.get("observed_at_ms"), record.get("available_at_ms")
    if seen is None or known is None:
        raise _Unknown(code + "_AVAILABILITY_NOT_PROVEN")
    seen, known = _stamp(seen), _stamp(known)
    if not seen <= known <= at or at - seen >= max_age:
        raise _Unknown(code + "_NOT_TIMELY")
    return seen, known


def _component(operation):
    try:
        evidence = operation()
        return {"status": "AVAILABLE", "reason": None, "evidence": evidence}
    except _Unknown as exc:
        return {"status": "UNKNOWN", "reason": str(exc), "evidence": None}
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
        return {"status": "UNKNOWN", "reason": "EVIDENCE_INVALID", "evidence": None}


def _core(scope, episode, target, at, opened, facts, baseline, baseline_policy):
    if (
        not isinstance(scope, AccountScope)
        or (scope.exchange, scope.environment, scope.product)
        != ("BINANCE", "SANDBOX", "FUTURES")
        or not is_supported_symbol(target, quote_assets=("USDT",))
        or not isinstance(episode, str)
        or len(episode) != 36
        or str(UUID(episode)) != episode
        or _stamp(opened) > _stamp(at)
    ):
        raise _Unknown("EXIT_RESEARCH_SCOPE_OR_TIME_INVALID")
    if (
        not isinstance(facts, dict)
        or len(facts) != len(ExitFacts.__dataclass_fields__)
        or set(facts) != set(ExitFacts.__dataclass_fields__)
    ):
        raise _Unknown("FROZEN_EXIT_FACTS_INVALID")
    encoded = {}
    for name, value in facts.items():
        if name == "momentum_closes_15m":
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                raise _Unknown("FROZEN_EXIT_FACTS_INVALID")
            for close in value:
                _money(close)
            encoded[name] = list(value)
        elif name in {"partial_done", "held_ms"}:
            if type(value) is not (bool if name == "partial_done" else int):
                raise _Unknown("FROZEN_EXIT_FACTS_INVALID")
            encoded[name] = value
        else:
            if type(value) is not str or len(value) > 128:
                raise _Unknown("FROZEN_EXIT_FACTS_INVALID")
            encoded[name] = value
    if encoded["held_ms"] != at - opened:
        raise _Unknown("ORIGINAL_HOLDING_CLOCK_MISMATCH")
    if (
        not isinstance(baseline_policy, dict)
        or len(baseline_policy) != len(SCHEMA)
        or set(baseline_policy) != set(SCHEMA)
    ):
        raise _Unknown("FROZEN_BASELINE_POLICY_INCOMPLETE")
    values = resolve(baseline_policy)
    if (
        not isinstance(baseline, dict)
        or len(baseline) != len(ExitDecision.__dataclass_fields__)
        or set(baseline) != set(ExitDecision.__dataclass_fields__)
    ):
        raise _Unknown("FROZEN_BASELINE_INVALID")
    # Reproduction is diagnostic only: the returned decision is never used for
    # execution and cannot replace the caller's already made baseline decision.
    reproduced = evaluate_directional_exit(
        ExitFacts(**encoded), policy=values
    ).evidence()
    if baseline != reproduced:
        raise _Unknown("FROZEN_BASELINE_REPLAY_MISMATCH")
    return {
        "at_ms": at,
        "facts": encoded,
        "baseline": reproduced,
        "baseline_policy": values,
    }


def _observation(raw, *, target, facts, at, max_age):
    if not isinstance(raw, dict):
        raise _Unknown("OBSERVATION_NOT_SUPPLIED")
    if (raw.get("symbol"), raw.get("environment")) != (target, "SANDBOX"):
        raise _Unknown("OBSERVATION_MARKET_SCOPE_MISMATCH")
    if (
        raw.get("source") not in _SOURCES
        or raw.get("market_source", _MARKET) != _MARKET
    ):
        raise _Unknown("OBSERVATION_MARKET_SOURCE_UNPROVEN")
    seen, known = _timely(raw, at, max_age, "OBSERVATION")
    for name in ("mark_price", "funding_rate", "ema9_1h", "ema20_1h", "exit_fee_rate"):
        if _money(raw.get(name), positive=False) != _money(facts[name], positive=False):
            raise _Unknown("OBSERVATION_EXIT_FACTS_MISMATCH")
    closes = raw.get("momentum_closes_15m")
    if (
        not isinstance(closes, (list, tuple))
        or len(closes) != 4
        or any(
            _money(left) != _money(right)
            for left, right in zip(closes, facts["momentum_closes_15m"], strict=True)
        )
    ):
        raise _Unknown("OBSERVATION_EXIT_FACTS_MISMATCH")
    for width, field in ((900000, "closed_15m_at_ms"), (HOUR, "closed_1h_at_ms")):
        if _stamp(raw.get(field)) != seen // width * width:
            raise _Unknown("CLOSED_EXIT_CANDLES_STALE_OR_FUTURE")
    return {
        "observed_at_ms": seen,
        "available_at_ms": known,
        "closed_15m_at_ms": raw["closed_15m_at_ms"],
        "closed_1h_at_ms": raw["closed_1h_at_ms"],
        "environment": "SANDBOX",
        "market_source": _MARKET,
    }


def _hourly(raw, *, target, at, closed_at):
    if not isinstance(raw, dict):
        raise _Unknown("HOURLY_HISTORY_NOT_SUPPLIED")
    if (
        raw.get("symbol"),
        raw.get("environment"),
        raw.get("interval"),
        raw.get("market_source"),
    ) != (target, "SANDBOX", "1h", _MARKET):
        raise _Unknown("HOURLY_HISTORY_MARKET_SCOPE_MISMATCH")
    rows = raw.get("rows")
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 72:
        raise _Unknown("HOURLY_HISTORY_BOUND")
    receipt = raw.get("available_at_ms")
    if receipt is not None and _stamp(receipt) > at:
        raise _Unknown("HOURLY_BATCH_NOT_AVAILABLE_AS_OF")
    result, last_known = [], None
    for row in rows:
        if isinstance(row, (list, tuple)):
            if len(row) != 12 or _stamp(row[6]) != _stamp(row[0]) + HOUR - 1:
                raise _Unknown("RAW_HOURLY_ROW_INVALID")
            values = dict(zip(("t", "o", "h", "l", "c", "v"), row[:6], strict=True))
            known = receipt
        elif isinstance(row, dict):
            values = {name: row[name] for name in ("t", "o", "h", "l", "c", "v")}
            # An explicitly unknown per-row receipt must not be repaired from
            # the batch timestamp; absent per-row provenance may use an actual
            # recorded receipt for this complete batch, never at_ms by default.
            known = row.get("available_at_ms", receipt)
        else:
            raise _Unknown("HOURLY_ROW_INVALID")
        for name in ("o", "h", "l", "c", "v"):
            _money(values[name], positive=name != "v")
        bar = Bar(**values)
        if bar.t % HOUR or bar.t + HOUR > at:
            raise _Unknown("HOURLY_BAR_UNCLOSED_OR_UNALIGNED")
        if result and bar.t != result[-1]["t"] + HOUR:
            raise _Unknown("HOURLY_HISTORY_GAP_OR_REORDERED")
        if known is None:
            raise _Unknown("HOURLY_AVAILABILITY_NOT_PROVEN")
        known = _stamp(known)
        if not bar.t + HOUR <= known <= at or receipt is not None and known > receipt:
            raise _Unknown("HOURLY_AVAILABILITY_INVALID")
        if last_known is not None and known < last_known:
            raise _Unknown("HOURLY_AVAILABILITY_REORDERED")
        last_known = known
        result.append({**bar.payload(), "available_at_ms": known})
    if result[-1]["t"] + HOUR != closed_at:
        raise _Unknown("HOURLY_HISTORY_STALE_OR_FUTURE")
    return result


def _native_terms(raw, *, scope, episode, target, facts, at, max_age):
    if not isinstance(raw, dict) or raw.get("account_scope") != asdict(scope):
        raise _Unknown("NATIVE_ACCOUNT_SCOPE_UNPROVEN")
    spec, response = raw.get("spec"), raw.get("response")
    expected_side = "SELL" if facts["side"] == "BUY" else "BUY"
    if (
        not isinstance(spec, dict)
        or len(spec) != 5
        or set(spec) != {"episode", "symbol", "side", "kind", "trigger_price"}
        or (spec["episode"], spec["symbol"], spec["side"], spec["kind"])
        != (episode, target, expected_side, "STOP_MARKET")
        or _money(spec["trigger_price"]) != _money(facts["stop_price"])
        or not isinstance(response, dict)
    ):
        raise _Unknown("NATIVE_STOP_BINDING_UNPROVEN")
    seen, known = _timely(raw, at, max_age, "NATIVE_QUERY")
    key = StateKey(
        **asdict(scope),
        namespace="testnet-protection-v1",
        key=canonical({"episode": episode, "symbol": target, "kind": "STOP_MARKET"}),
    )
    if (
        (
            response.get("symbol"),
            response.get("side"),
            response.get("positionSide"),
            response.get("orderType"),
            response.get("algoType"),
            response.get("algoStatus"),
            response.get("workingType"),
        )
        != (
            target,
            expected_side,
            "BOTH",
            "STOP_MARKET",
            "CONDITIONAL",
            "NEW",
            "MARK_PRICE",
        )
        or response.get("closePosition") is not True
        or response.get("priceProtect") is not False
        or _money(response.get("triggerPrice")) != _money(facts["stop_price"])
        or type(response.get("algoId")) is not int
        or response["algoId"] <= 0
        or response.get("clientAlgoId") != "v2p-" + digest(key.identity)[:28]
    ):
        raise _Unknown("NATIVE_STOP_TERMS_UNPROVEN")
    return {
        "status": "NEW",
        "price": spec["trigger_price"],
        "observed_at_ms": seen,
        "available_at_ms": known,
        "time_basis": "LOCAL_QUERY_INTERVAL_NOT_VENUE_ORDER_UPDATE_TIME",
        "side": expected_side,
        "close_all": True,
        "quantity_coverage_proven": False,
    }


def _native_quantity(raw, *, scope, target, facts, at, max_age):
    proof = raw.get("quantity_evidence") if isinstance(raw, dict) else None
    if not isinstance(proof, dict) or proof.get("account_scope") != asdict(scope):
        raise _Unknown("NATIVE_POSITION_QUANTITY_NOT_SUPPLIED")
    seen, known = _timely(proof, at, max_age, "NATIVE_POSITION")
    row = proof.get("response")
    if not isinstance(row, dict) or (row.get("symbol"), row.get("positionSide")) != (
        target,
        "BOTH",
    ):
        raise _Unknown("NATIVE_POSITION_SCOPE_MISMATCH")
    quantity = _money(row.get("positionAmt"), positive=False)
    wrong_side = quantity <= 0 if facts["side"] == "BUY" else quantity >= 0
    absolute = quantity.copy_abs()
    if wrong_side or absolute < _money(facts["remaining_quantity"]):
        raise _Unknown("NATIVE_POSITION_DIRECTION_OR_QUANTITY_MISMATCH")
    return {
        "protected_quantity": format(absolute, "f"),
        "observed_at_ms": seen,
        "available_at_ms": known,
        "basis": "CALLER_SCOPED_POSITION_RISK_OBSERVATION",
    }


def build_exit_research_snapshot(
    *,
    scope,
    episode,
    symbol,
    at_ms,
    opened_at_ms,
    facts,
    baseline,
    baseline_policy,
    observation,
    hourly=None,
    native=None,
    max_age_ms=10000,
    minimum_hourly_bars=24,
):
    """Detach existing evidence; never fetch, mutate, infer timestamps or trade.

    Hourly input is an explicit scoped 1h batch with <=72 raw Binance rows or
    normalized OHLCV rows. Native input is a scoped ProtectionSpec + response;
    quantity_evidence is a separately scoped/timed existing positionRisk row.
    A close-all flag or venue quantity=0 alone is not position quantity proof.
    """
    result = {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": None,
        "execution_authorized": False,
        "promotion_authorized": False,
        "actual_pnl": False,
        "historical_code_identity_proven": False,
        "binding": None,
        "snapshot": None,
        "components": {},
        "qualification_gaps": [],
        "limitations": [
            "CALLER_SUPPLIED_ACQUISITION_PROVENANCE_NOT_INDEPENDENT_VENUE_QUERY",
            "NATIVE_ORDER_AND_POSITION_OBSERVATIONS_NOT_ATOMIC",
            "PARTIAL_EVIDENCE_CANNOT_AUTHORIZE_EXIT_EXTENSION",
            "NO_HISTORICAL_RECEIPT_INFERRED_FROM_BAR_CLOSE_OR_ORDER_UPDATE_TIME",
        ],
    }
    if (
        type(max_age_ms) is not int
        or not 1 <= max_age_ms <= 30000
        or type(minimum_hourly_bars) is not int
        or not 1 <= minimum_hourly_bars <= 72
    ):
        result["reason"] = "EXIT_RESEARCH_BOUNDS_INVALID"
        return result
    core = _component(
        lambda: _core(
            scope,
            episode,
            symbol,
            at_ms,
            opened_at_ms,
            facts,
            baseline,
            baseline_policy,
        )
    )
    if core["status"] != "AVAILABLE":
        result["reason"] = core["reason"]
        result["qualification_gaps"] = [core["reason"]]
        return result
    values = core["evidence"]
    result["binding"] = {
        "episode": episode,
        "symbol": symbol,
        "environment": scope.environment,
        "account_scope_digest": digest(canonical(asdict(scope))),
        "opened_at_ms": opened_at_ms,
        "at_ms": at_ms,
    }
    observed = _component(
        lambda: _observation(
            observation,
            target=symbol,
            facts=values["facts"],
            at=at_ms,
            max_age=max_age_ms,
        )
    )
    if observed["status"] == "AVAILABLE":
        observed["indicator_version"] = observation["source"]
    expected_hour = (
        observed["evidence"]["closed_1h_at_ms"]
        if observed["status"] == "AVAILABLE"
        else at_ms // HOUR * HOUR
    )
    hours = _component(
        lambda: _hourly(hourly, target=symbol, at=at_ms, closed_at=expected_hour)
    )
    if hours["status"] == "AVAILABLE":
        hours["closed_bar_count"] = len(hours["evidence"])
        hours["minimum_required_bars"] = minimum_hourly_bars
        if len(hours["evidence"]) < minimum_hourly_bars:
            hours.update(status="PARTIAL", reason="HOURLY_TREND_WARMUP_INSUFFICIENT")
    terms = _component(
        lambda: _native_terms(
            native,
            scope=scope,
            episode=episode,
            target=symbol,
            facts=values["facts"],
            at=at_ms,
            max_age=max_age_ms,
        )
    )
    quantity = _component(
        lambda: _native_quantity(
            native,
            scope=scope,
            target=symbol,
            facts=values["facts"],
            at=at_ms,
            max_age=max_age_ms,
        )
    )
    protection = None
    if terms["status"] == quantity["status"] == "AVAILABLE":
        # Conservatively retain the oldest observation and latest receipt from
        # the non-atomic pair; never manufacture a fresher combined observation.
        before, position = terms["evidence"], quantity["evidence"]
        protection = {
            "status": "NEW",
            "price": before["price"],
            "protected_quantity": position["protected_quantity"],
            "observed_at_ms": min(before["observed_at_ms"], position["observed_at_ms"]),
            "available_at_ms": max(
                before["available_at_ms"], position["available_at_ms"]
            ),
        }
    components = {
        "observation": observed,
        "hourly_history": hours,
        "native_terms": terms,
        "native_quantity": quantity,
    }
    gaps = [
        part["reason"] for part in components.values() if part["status"] != "AVAILABLE"
    ]
    result.update(
        status="AVAILABLE"
        if not gaps
        else "PARTIAL"
        if any(part["evidence"] is not None for part in components.values())
        else "UNKNOWN",
        reason="EXIT_RESEARCH_EVIDENCE_INCOMPLETE" if gaps else None,
        components=components,
        qualification_gaps=gaps,
        snapshot={
            **values,
            "observation": observed["evidence"],
            "hourly_history": hours["evidence"] or [],
            "native_protection": protection,
        },
    )
    return result
