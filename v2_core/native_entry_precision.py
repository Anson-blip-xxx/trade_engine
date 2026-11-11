"""Pinned stop-creation entry evidence for fully closed Testnet episodes.

No exchange calls, order authority, inferred child IDs or monetary tolerance.
A stale parent status is not a fill: immutable adopted fill/owner evidence and
the deterministic parent client identity must independently agree.
"""

import re
from fractions import Fraction
from uuid import UUID

from v2_core.evidence import canonical, digest
from v2_core.precision import amount
from v2_core.state import StateKey

INVALID = "PNL_ENTRY_OBSERVATION_INVALID"
ORIGINS = frozenset({"BINANCE_EXTERNAL_CLOSE", "BINANCE_ALGO_CHILD"})


def protection_key(scope, episode, symbol):
    return StateKey(
        **scope,
        namespace="testnet-protection-v1",
        key=canonical({"episode": episode, "symbol": symbol, "kind": "STOP_MARKET"}),
    )


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value)


def native_entry_anchor(trace, order, witness, *, qty, closing):
    try:
        return _checked(trace, order, witness, qty=qty, closing=closing)
    except (KeyError, TypeError, AttributeError, IndexError, OverflowError):
        raise ValueError(INVALID) from None


def _checked(trace, order, witness, *, qty, closing):
    scope = {
        k: trace["scope"][k]
        for k in ("exchange", "account_id", "environment", "product")
    }
    symbol, episode = trace["request"]["symbol"], trace["intent_id"]
    key = protection_key(scope, episode, symbol)
    client = "v2p-" + digest(key.identity)[:28]
    binding = order["request_evidence"]
    origin = binding.get("origin")
    if (
        (scope["exchange"], scope["environment"], scope["product"])
        != ("BINANCE", "SANDBOX", "FUTURES")
        or str(UUID(episode)) != episode
        or origin not in ORIGINS
        or not _identifier(binding.get("exchange_order_id"))
        or (
            "exchange_order_id" in order
            and str(order["exchange_order_id"]) != binding["exchange_order_id"]
        )
        or order["leg"] != "CLOSE"
        or order["status"] != "FILLED"
        or order["order_type"] != "MARKET"
        or Fraction(amount(order["quantity"], positive=True)) != qty
        or witness["protection_state_id"] != key.identity
        or type(witness["protection_creation_version"]) is not int
        or witness["protection_creation_version"] != 1
        or type(witness["protection_state_version"]) is not int
        or witness["protection_state_version"] < 2
        or witness["protection_owner"] != {"episode": episode, "binding": binding}
    ):
        raise ValueError(INVALID)
    creation, current = witness["protection_creation"], witness["protection_state"]
    proof, spec = creation["creation_proof"], creation["spec"]
    side = "SELL" if trace["request"]["side"] == "BUY" else "BUY"
    if (
        set(creation) != {"status", "spec", "creation_proof"}
        or creation["status"] != "SENDING"
        or spec != current["spec"]
        or proof != current["creation_proof"]
        or proof["source"] != "exclusive-testnet-stop-install-v1"
        or proof["account_scope"] != scope
        or proof["spec"] != spec
        or set(spec) != {"episode", "symbol", "side", "kind", "trigger_price"}
        or (spec["episode"], spec["symbol"], spec["side"], spec["kind"])
        != (episode, symbol, side, "STOP_MARKET")
        or not isinstance(proof["facts_digest"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", proof["facts_digest"])
        or proof["reference"]["symbol"] != symbol
        or proof["reference"]["environment"] != "SANDBOX"
        or witness["inventory"] != proof["inventory"]
    ):
        raise ValueError(INVALID)
    amount(spec["trigger_price"], positive=True)
    # Submission and observation belong to this exact deterministic stop.
    # Neither their algoStatus nor the absence of actualOrderId grants finality.
    for parent in (current["submission"], current["observation"]):
        if (
            parent.get("clientAlgoId") != client
            or parent.get("symbol") != symbol
            or parent.get("side") != side
            or parent.get("positionSide") != "BOTH"
            or parent.get("orderType") != "STOP_MARKET"
            or parent.get("algoType") != "CONDITIONAL"
            or parent.get("workingType") != "MARK_PRICE"
            or parent.get("priceProtect") is not False
            or type(parent.get("algoId")) is not int
            or parent["algoId"] <= 0
            or parent.get("closePosition") is not True
            or amount(parent["triggerPrice"], positive=True)
            != amount(spec["trigger_price"], positive=True)
        ):
            raise ValueError(INVALID)
    if current["submission"]["algoId"] != current["observation"]["algoId"]:
        raise ValueError(INVALID)
    if origin == "BINANCE_EXTERNAL_CLOSE":
        raw = binding["venue_order"]
        if (
            binding.get("kind") != "EXTERNAL_REDUCE_ONLY"
            or binding.get("venue_client_order_id") != client
            or raw.get("clientOrderId") != client
            or str(raw.get("orderId")) != binding["exchange_order_id"]
            or raw.get("symbol") != symbol
            or raw.get("side") != side
            or raw.get("positionSide") != "BOTH"
            or raw.get("reduceOnly") is not True
            or raw.get("closePosition") is not True
            or raw.get("status") != "FILLED"
            or raw.get("type") != "MARKET"
            or Fraction(amount(raw["origQty"], positive=True)) != qty
            or Fraction(amount(raw["executedQty"], positive=True)) != qty
        ):
            raise ValueError(INVALID)
        fill_binding = "external_close_binding"
    else:
        if (
            binding.get("parent_client_algo_id") != client
            or binding.get("parent_algo_id") != current["submission"]["algoId"]
            or str(current["observation"].get("actualOrderId"))
            != binding["exchange_order_id"]
            or not isinstance(binding.get("venue_client_order_id"), str)
            or not binding["venue_client_order_id"]
        ):
            raise ValueError(INVALID)
        fill_binding = "native_child_binding"
    if any(
        fill["order_id"] != order["order_id"]
        or fill["evidence"].get("order_id") != binding["exchange_order_id"]
        or fill["evidence"].get("symbol") != symbol
        or (
            (origin == "BINANCE_EXTERNAL_CLOSE" or fill_binding in fill["evidence"])
            and fill["evidence"].get(fill_binding) != binding
        )
        for fill in closing
    ):
        raise ValueError(INVALID)
    observation = proof["inventory"]
    registered = witness["creation_recorded_at_ms"]
    finished, reference = (
        observation["finished_at_ms"],
        proof["reference"]["observed_at_ms"],
    )
    if (
        any(type(v) is not int or v < 0 for v in (registered, finished, reference))
        or not max(finished, reference)
        <= registered
        < min(f["occurred_at_ms"] for f in closing)
        or type(proof["deadline_ms"]) is not int
        or proof["deadline_ms"] != min(finished, reference) + 10000
        or registered >= proof["deadline_ms"]
    ):
        raise ValueError(INVALID)
    raw = observation["responses"]
    common = {
        "positions_before",
        "positions_after",
        "account",
        "ordinary_orders",
        "conditional_orders",
    }
    if set(raw) != common | (
        {"account_config"} if "account_config" in raw else {"mode", "margin_mode"}
    ):
        raise ValueError(INVALID)
    identity = str(UUID(observation["observation_id"]))
    expected_digest = digest(canonical(observation))
    return (
        observation,
        identity,
        expected_digest,
        {
            "source": "native-protection-creation-v1",
            "protection_state_id": key.identity,
            "protection_creation_version": 1,
            "creation_recorded_at_ms": registered,
            "creation_digest": digest(canonical(creation)),
            "close_binding_digest": digest(canonical(binding)),
            "close_origin": origin,
        },
    )
