"""Versioned, exact per-fill proof for observed Binance USDT cash truncation.

This is not an epsilon allowance or a claim about every Binance product. Only
single-opening, fully closed linear episodes with no later increases qualify.
The caller must ALSO prove income/fill and account-wallet equality. Unknown
rounding conventions, reordered exposure and unrepresentable corrections fail.

An explicit caller opt-in additionally permits one narrowly observed reported-
cash discrepancy. It is a settlement candidate, not a verified venue algorithm:
matched income and final exact wallet reconciliation remain required.
"""

from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from v2_core.evidence import canonical, digest
from v2_core.native_entry_precision import ORIGINS, native_entry_anchor
from v2_core.precision import amount, decimal_text, money_context
from v2_core.state import StateKey

MODEL = "binance-usdt-per-fill-truncate-8-v1"
OBSERVED_ENTRY_MODEL = "binance-usdt-observed-entry-truncate-8-v1"
REPORTED_BOUNDARY_MODEL = "venue-reported-boundary-cash-adjustment-v1"


def _observed_entry(trace, order, witness, *, entry, qty, opening, closing):
    """One full close, pinned to an immutable, pre-close venue observation.

    A neighboring cash quantum is never evidence. The recorded entry must be
    the unique nearest decimal to the rational fill average at its own scale;
    the whole-position effect must be smaller than one cash quantum.
    """
    invalid = "PNL_ENTRY_OBSERVATION_INVALID"
    scope = {
        name: trace["scope"][name]
        for name in ("exchange", "account_id", "environment", "product")
    }
    if (
        (scope["exchange"], scope["environment"], scope["product"])
        != ("BINANCE", "SANDBOX", "FUTURES")
        or not trace["request"]["symbol"].endswith("USDT")
        or len({f["order_id"] for f in closing}) != 1
        or Fraction(amount(order["quantity"], positive=True)) != qty
        or not isinstance(witness, dict)
    ):
        raise ValueError(invalid)
    request = order.get("request_evidence", {})
    source = request.get("source")
    if request.get("origin") not in ORIGINS and source not in {
        "directional-exit-v1",
        "unprotected-emergency-v1",
    }:
        raise ValueError(invalid)
    observation = witness["inventory"]
    identity = (
        None
        if request.get("origin") in ORIGINS
        else str(UUID(request["preflight_inventory"]))
    )
    anchor = {"source": source}
    opening_id = opening[0]["order_id"]
    if request.get("origin") in ORIGINS:
        observation, identity, expected_digest, anchor = native_entry_anchor(
            trace, order, witness, qty=qty, closing=closing
        )
    elif source == "directional-exit-v1":
        state_key = StateKey(
            **scope, namespace="directional-exit-v1", key=trace["intent_id"]
        )
        saved = witness["exit_state"]
        if (
            request.get("state_id") != state_key.identity
            or type(request.get("state_version")) is not int
            or request["state_version"] < 1
            or saved.get("episode") != trace["intent_id"]
            or saved.get("account_scope") != scope
            or saved.get("opening_order_id") != opening_id
            or saved.get("execution_authorized") is not True
            or saved.get("preflight_inventory") != identity
        ):
            raise ValueError(invalid)
        expected_digest = saved.get("preflight_digest")
        anchor.update(
            state_id=state_key.identity, state_version=request["state_version"]
        )
    elif source == "unprotected-emergency-v1":
        if request.get("opening_order_id") != opening_id:
            raise ValueError(invalid)
        expected_digest = request.get("preflight_digest")
    else:
        raise ValueError(invalid)
    if (
        observation.get("account_scope") != scope
        or observation.get("observation_id") != identity
        or digest(canonical(observation)) != expected_digest
        or observation.get("failures") != {}
        or not isinstance(observation.get("blockers"), list)
        or not set(observation["blockers"])
        <= {"EXISTING_POSITION_REQUIRES_RECOVERY", "EXISTING_CONDITIONAL_ORDERS"}
    ):
        raise ValueError(invalid)
    raw = observation["responses"]
    if observation.get("response_digests") != {
        name: digest(canonical({"response": value})) for name, value in raw.items()
    }:
        raise ValueError(invalid)
    mode = raw.get("account_config", raw.get("mode", {}))
    margin = raw.get("account_config", raw.get("margin_mode", {}))
    if (
        mode.get("dualSidePosition") is not False
        or margin.get("multiAssetsMargin") is not False
    ):
        raise ValueError(invalid)
    positions = []
    for name in ("positions_before", "positions_after"):
        matches = [
            r for r in raw[name] if r.get("symbol") == trace["request"]["symbol"]
        ]
        if len(matches) != 1 or matches[0].get("positionSide") != "BOTH":
            raise ValueError(invalid)
        positions.append(
            {
                key: matches[0][key]
                for key in ("positionAmt", "entryPrice", "updateTime")
            }
        )
    before, after = positions
    sign = 1 if trace["request"]["side"] == "BUY" else -1
    started, finished = observation["started_at_ms"], observation["finished_at_ms"]
    updated = before["updateTime"]
    if (
        before != after
        or Fraction(amount(before["positionAmt"])) != qty * sign
        or any(type(t) is not int for t in (started, finished, updated))
        or not max(f["occurred_at_ms"] for f in opening)
        <= updated
        <= started
        <= finished
        < min(f["occurred_at_ms"] for f in closing)
    ):
        raise ValueError(invalid)
    recorded = amount(before["entryPrice"], positive=True)
    places = -recorded.as_tuple().exponent
    if not 0 <= places <= 18:
        raise ValueError(invalid)
    scale = 10**places
    scaled = entry * scale
    whole, remainder = divmod(scaled.numerator, scaled.denominator)
    # Do not invent a venue tie-breaking convention from one observation.
    if remainder * 2 == scaled.denominator:
        raise ValueError(invalid)
    nearest = Fraction(whole + (remainder * 2 > scaled.denominator), scale)
    if Fraction(recorded) != nearest or abs(
        (Fraction(recorded) - entry) * qty
    ) >= Fraction(1, 100_000_000):
        raise ValueError(invalid)
    return Fraction(recorded), {
        **anchor,
        "observation_id": identity,
        "inventory_digest": expected_digest,
        "entry_price": before["entryPrice"],
        "entry_decimal_places": places,
        "position_quantity": before["positionAmt"],
        "position_updated_at_ms": updated,
        "started_at_ms": started,
        "finished_at_ms": finished,
    }


def _reported_boundary_match(
    trace, report, *, opening, closing, qty, entry, recorded, exact, observed
):
    """Recognize the bounded cash shape, never infer how a venue computed it.

    The caller has already validated the exact pre-close witness. Only a
    positive, single-fill close one cash quantum below an exact grid boundary
    qualifies; no neighboring-loss, multi-fill or total-error cancellation rule.
    """
    quantum = Fraction(1, 100_000_000)
    scope = trace["scope"]
    return (
        (scope["exchange"], scope["environment"], scope["product"])
        == ("BINANCE", "SANDBOX", "FUTURES")
        and scope.get("producer") in {"s6", "s8"}
        and trace["configuration"].get("mode") == "TESTNET_ADMISSION_ONLY"
        and trace["request"]["symbol"].endswith("USDT")
        and report["settlement_currency"] == "USDT"
        and len(opening) == len(closing) == 1
        and len(trace["orders"]) == 2
        and {order["leg"] for order in trace["orders"]} == {"OPEN", "CLOSE"}
        and all(
            order["status"] == "FILLED"
            and Fraction(amount(order["quantity"], positive=True)) == qty
            for order in trace["orders"]
        )
        and recorded == entry
        and exact > quantum
        and (exact / quantum).denominator == 1
        and Fraction(observed) == exact - quantum
        and observed > 0
    )


def rounding_proof(
    trace, report, *, entry_observations=None, allow_reported_boundary_cash=False
):
    if type(allow_reported_boundary_cash) is not bool:
        raise ValueError("EXPLICIT_BOUNDARY_CASH_POLICY_REQUIRED")
    orders = {o["order_id"]: o for o in trace["orders"]}
    opening = [f for f in trace["fills"] if orders[f["order_id"]]["leg"] == "OPEN"]
    closing = [f for f in trace["fills"] if orders[f["order_id"]]["leg"] == "CLOSE"]
    if (
        not opening
        or not closing
        or len({f["order_id"] for f in opening}) != 1
        or len(opening) + len(closing) != len(trace["fills"])
        or max(f["occurred_at_ms"] for f in opening)
        >= min(f["occurred_at_ms"] for f in closing)
        or trace["request"]["side"] not in {"BUY", "SELL"}
    ):
        raise ValueError("UNSUPPORTED_PNL_PRECISION_TIMELINE")

    def quantity(fill):
        return Fraction(amount(fill["quantity"], positive=True))

    def price(fill):
        return Fraction(amount(fill["price"], positive=True))

    qty = sum(quantity(f) for f in opening)
    if qty != sum(quantity(f) for f in closing):
        raise ValueError("EXPOSURE_NOT_CLOSED")
    if any(amount(f["evidence"]["venue_realized_pnl"]) != 0 for f in opening):
        raise ValueError("OPEN_FILL_REALIZED_PNL_NONZERO")
    entry = sum(quantity(f) * price(f) for f in opening) / qty
    sign = 1 if trace["request"]["side"] == "BUY" else -1
    theoretical, venue, proofs = Fraction(0), Fraction(0), []
    model = MODEL
    for fill in sorted(
        closing, key=lambda f: (f["occurred_at_ms"], f["evidence"]["trade_id"])
    ):
        exact = quantity(fill) * (price(fill) - entry) * sign
        observed = amount(fill["evidence"]["venue_realized_pnl"])
        # int(Fraction) truncates toward zero, including losses. No Decimal
        # division may move a value across a rounding boundary here.
        truncated = Fraction(int(exact * 100_000_000), 100_000_000)
        method = "EXACT" if Fraction(observed) == exact else "TRUNCATE_TOWARD_ZERO_8"
        witness_proof = {}
        if Fraction(observed) not in {exact, truncated}:
            witness = (entry_observations or {}).get(fill["order_id"])
            if witness is None:
                raise ValueError("UNEXPLAINED_FILL_PNL_PRECISION")
            recorded, observation = _observed_entry(
                trace,
                orders[fill["order_id"]],
                witness,
                entry=entry,
                qty=qty,
                opening=opening,
                closing=closing,
            )
            recorded_pnl = quantity(fill) * (price(fill) - recorded) * sign
            if Fraction(observed) == Fraction(
                int(recorded_pnl * 100_000_000), 100_000_000
            ):
                model = OBSERVED_ENTRY_MODEL
                method = "OBSERVED_ENTRY_TRUNCATE_TOWARD_ZERO_8"
            elif allow_reported_boundary_cash and _reported_boundary_match(
                trace,
                report,
                opening=opening,
                closing=closing,
                qty=qty,
                entry=entry,
                recorded=recorded,
                exact=exact,
                observed=observed,
            ):
                model = REPORTED_BOUNDARY_MODEL
                method = "VENUE_REPORTED_BOUNDARY_CASH_ADJUSTMENT"
                witness_proof.update(
                    cash_difference="-0.00000001",
                    cash_quantum="0.00000001",
                    algorithm_verified=False,
                    requires_final_wallet_match=True,
                    settlement_authorized=False,
                )
            else:
                raise ValueError("UNEXPLAINED_FILL_PNL_PRECISION")
            witness_proof["entry_observation"] = observation
        theoretical += exact
        venue += Fraction(observed)
        proofs.append(
            {
                "trade_id": fill["evidence"]["trade_id"],
                "theoretical_numerator": str(exact.numerator),
                "theoretical_denominator": str(exact.denominator),
                "venue_pnl": decimal_text(observed),
                "method": method,
                **witness_proof,
            }
        )
    if theoretical != Fraction(Decimal(report["gross_pnl"])):
        raise ValueError("VENUE_LEDGER_ECONOMICS_MISMATCH")
    delta = venue - theoretical
    with money_context():
        correction = Decimal(delta.numerator) / Decimal(delta.denominator)
    encoded = decimal_text(correction)
    if Fraction(amount(encoded)) != delta:
        raise ValueError("PNL_CORRECTION_NOT_EXACTLY_REPRESENTABLE")
    result = {
        "model": model,
        "amount": encoded,
        "theoretical_gross_pnl": report["gross_pnl"],
        "fills": proofs,
    }
    if model == REPORTED_BOUNDARY_MODEL:
        # This describes observed cash only. The caller must still prove the
        # final wallet equality before applying this correction atomically.
        with money_context():
            reported = Decimal(venue.numerator) / Decimal(venue.denominator)
        result.update(
            venue_reported_gross_pnl=decimal_text(reported),
            algorithm_verified=False,
            requires_final_wallet_match=True,
            settlement_authorized=False,
        )
    return result
