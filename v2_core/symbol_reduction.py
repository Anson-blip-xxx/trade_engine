"""Pure, fail-closed proof for independently owned symbol reductions.

This is not an execution permit. The caller must collect these facts while
holding the account lock, bracket inventory with equal local snapshots, recheck
the target stop and decision, and retain the normal reduce-only dispatch fence.
Full-account coverage remains blocked for OPEN even when this proof is ready.
"""

from dataclasses import asdict
from uuid import UUID

from v2_core.account_coverage import _excluded_positions, coverage_from_facts
from v2_core.account_inventory import (
    COMBINED_ROUTES,
    ROUTES,
    account_config_derivations,
    orders,
    positions,
)
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds
from v2_core.ingress import symbol as normalized_symbol
from v2_core.ledger import amount
from v2_core.protection import ProtectionSpec

_TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}
_PENDING = {"PREPARED", "SUBMITTING", "UNKNOWN", "ACKNOWLEDGED"}
_ISOLATABLE = {"LOCAL_ORDERS_NOT_FINAL", "MISSING_CONFIRMED_STOP"}
_ROUTES = {name for name, _ in ROUTES}
_COMBINED_ROUTES = {name for name, _ in COMBINED_ROUTES}


class _InvalidProof(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise _InvalidProof(code)


def _uuid(value):
    _require(
        isinstance(value, str) and str(UUID(value)) == value, "INVALID_REDUCTION_FACTS"
    )
    return value


def _exchange_id(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 32
        and value.isascii()
        and value.isdecimal()
        and int(value) > 0
        and str(int(value)) == value
    )


def _inventory(scope, inventory, *, now_ms, max_age_ms, exclusions):
    _require(isinstance(inventory, dict), "INVALID_REDUCTION_INVENTORY")
    _require(
        inventory.get("account_scope") == asdict(scope), "REDUCTION_SCOPE_MISMATCH"
    )
    _uuid(inventory["observation_id"])
    started, finished = (
        milliseconds(inventory["started_at_ms"]),
        milliseconds(inventory["finished_at_ms"]),
    )
    _require(
        started <= finished <= now_ms and now_ms - started < max_age_ms,
        "STALE_REDUCTION_INVENTORY",
    )
    _require(inventory.get("failures") == {}, "INCOMPLETE_ACCOUNT_READ")
    _require(
        inventory.get("excluded_position_symbols") == list(exclusions),
        "REDUCTION_EXCLUSIONS_MISMATCH",
    )
    raw = inventory["responses"]
    _require(
        isinstance(raw, dict) and set(raw) in (_ROUTES, _COMBINED_ROUTES),
        "INCOMPLETE_ACCOUNT_READ",
    )
    _require(
        inventory.get("response_digests")
        == {key: digest(canonical({"response": value})) for key, value in raw.items()},
        "INVENTORY_RESPONSE_DIGEST_MISMATCH",
    )
    before, after = (
        positions(raw["positions_before"]),
        positions(raw["positions_after"]),
    )
    _require(before == after, "POSITION_CHANGED_DURING_INVENTORY")
    if set(raw) == _COMBINED_ROUTES:
        derived = account_config_derivations(raw["account_config"])
        _require(
            inventory.get("derived_responses") == derived,
            "INVENTORY_DERIVATION_MISMATCH",
        )
        mode = derived["mode"]["response"]
        margin_mode = derived["margin_mode"]["response"]
    else:
        _require("derived_responses" not in inventory, "INVENTORY_DERIVATION_MISMATCH")
        mode, margin_mode = raw["mode"], raw["margin_mode"]
    _require(mode.get("dualSidePosition") is False, "ONE_WAY_MODE_REQUIRED")
    _require(
        margin_mode.get("multiAssetsMargin") is False, "SINGLE_ASSET_MODE_REQUIRED"
    )
    _require(
        all(
            row.get("positionSide") == "BOTH"
            for name in ("positions_before", "positions_after")
            for row in raw[name]
        ),
        "ONE_WAY_POSITIONS_REQUIRED",
    )
    summary = {
        "positions": [row for row in after if row["symbol"] not in exclusions],
        "ordinary_orders": orders(raw["ordinary_orders"], conditional=False),
        "conditional_orders": orders(raw["conditional_orders"], conditional=True),
        "balance": {
            key: str(amount(raw["account"][key]))
            for key in ("totalWalletBalance", "availableBalance", "totalMarginBalance")
        },
        "trade_permission_verified": False,
    }
    if exclusions:
        summary["excluded_positions"] = [
            row for row in after if row["symbol"] in exclusions
        ]
    _require(inventory.get("summary") == summary, "INVENTORY_SUMMARY_MISMATCH")
    expected_blockers = set()
    if summary["positions"]:
        expected_blockers.add("EXISTING_POSITION_REQUIRES_RECOVERY")
    if summary["ordinary_orders"]:
        expected_blockers.add("EXISTING_ORDINARY_ORDERS")
    if summary["conditional_orders"]:
        expected_blockers.add("EXISTING_CONDITIONAL_ORDERS")
    if amount(summary["balance"]["availableBalance"]) <= 0:
        expected_blockers.add("NO_AVAILABLE_BALANCE")
    actual = inventory.get("blockers")
    _require(
        isinstance(actual, list)
        and all(isinstance(item, str) for item in actual)
        and len(actual) == len(set(actual))
        and set(actual) == expected_blockers,
        "INVENTORY_BLOCKER_MISMATCH",
    )
    _require(
        inventory.get("status")
        == ("BLOCKED" if actual else "CLEAR_FOR_RECOVERY_CHECKS")
        and inventory.get("execution_authorized") is False,
        "INVALID_REDUCTION_INVENTORY",
    )


def _facts(facts):
    _require(
        isinstance(facts, dict) and set(facts) == {"episodes", "orders", "protections"},
        "INVALID_REDUCTION_FACTS",
    )
    for name in facts:
        _require(
            isinstance(facts[name], list)
            and all(isinstance(row, dict) for row in facts[name]),
            "INVALID_REDUCTION_FACTS",
        )
    owners, seen = {}, set()
    for row in facts["episodes"]:
        episode = _uuid(row["episode"])
        _require(episode not in seen, "DUPLICATE_EPISODE_FACTS")
        seen.add(episode)
        target = normalized_symbol(row["symbol"])
        _require(
            row["side"] in {"BUY", "SELL"}
            and type(row["revision"]) is int
            and row["revision"] >= 0,
            "INVALID_REDUCTION_FACTS",
        )
        remaining = amount(row["remaining"])
        _require(remaining >= 0, "LEDGER_OVERCLOSED")
        if remaining:
            owners.setdefault(target, []).append(row)
    seen = set()
    for row in facts["orders"]:
        identity = _uuid(row["order_id"])
        _require(identity not in seen, "DUPLICATE_ORDER_FACTS")
        seen.add(identity)
        normalized_symbol(row["symbol"])
        _require(
            row["status"] in _TERMINAL | _PENDING
            and row["leg"] in {"OPEN", "CLOSE"}
            and type(row["version"]) is int
            and row["version"] >= 1
            and type(row["has_fills"]) is bool
            and (
                row["exchange_order_id"] is None
                or _exchange_id(row["exchange_order_id"])
            ),
            "INVALID_REDUCTION_FACTS",
        )
    seen = set()
    for row in facts["protections"]:
        _require(row["state_id"] not in seen, "DUPLICATE_PROTECTION_FACTS")
        seen.add(row["state_id"])
        _require(
            type(row["version"]) is int and row["version"] >= 1,
            "INVALID_REDUCTION_FACTS",
        )
        ProtectionSpec(**row["payload"]["spec"])
    return owners


def _target_projection(facts, inventory, target):
    projected_facts = {
        "episodes": [row for row in facts["episodes"] if row["symbol"] == target],
        "orders": [row for row in facts["orders"] if row["symbol"] == target],
        "protections": [
            row
            for row in facts["protections"]
            if row["payload"]["spec"]["symbol"] == target
        ],
    }
    projected_inventory = {
        **inventory,
        "excluded_position_symbols": [],
        "responses": {
            **inventory["responses"],
            **{
                name: [
                    row
                    for row in inventory["responses"][name]
                    if row["symbol"] == target
                ]
                for name in (
                    "positions_before",
                    "positions_after",
                    "ordinary_orders",
                    "conditional_orders",
                )
            },
        },
        "summary": {
            **inventory["summary"],
            "positions": [
                row
                for row in inventory["summary"]["positions"]
                if row["symbol"] == target
            ],
        },
    }
    return projected_facts, projected_inventory


def symbol_reduction_proof(
    scope,
    facts,
    inventory,
    *,
    episode,
    symbol,
    side,
    remaining,
    now_ms,
    max_age_ms=10000,
    excluded_position_symbols=(),
):
    """Prove isolation; never clear account blockers or authorize a POST.

    Digests bind this proof to the complete original account evidence, not a
    filtered view. Hash consistency is not data-source authentication: scope-
    bound collectors and the caller's lock/recheck discipline remain required.
    """
    result = {
        "status": "SYMBOL_REDUCTION_BLOCKED",
        "blockers": [],
        "account_blockers": [],
        "quarantined_symbols": [],
        "account_scope": None,
        "episode": None,
        "symbol": None,
        "side": None,
        "remaining": None,
        "facts_digest": None,
        "inventory_digest": None,
        "execution_authorized": False,
    }
    try:
        _require(
            isinstance(scope, AccountScope) and scope.environment == "SANDBOX",
            "REDUCTION_SCOPE_MISMATCH",
        )
        result["account_scope"] = asdict(scope)
        episode, symbol = _uuid(episode), normalized_symbol(symbol)
        _require(side in {"BUY", "SELL"}, "INVALID_REDUCTION_TARGET")
        remaining = amount(remaining, positive=True)
        milliseconds(now_ms)
        _require(
            type(max_age_ms) is int and 1 <= max_age_ms <= 60000,
            "INVALID_REDUCTION_FRESHNESS",
        )
        exclusions = _excluded_positions(excluded_position_symbols)
        _require(symbol not in exclusions, "TARGET_SYMBOL_EXCLUDED")
        result.update(
            episode=episode, symbol=symbol, side=side, remaining=str(remaining)
        )
        _inventory(
            scope,
            inventory,
            now_ms=now_ms,
            max_age_ms=max_age_ms,
            exclusions=exclusions,
        )
        owners = _facts(facts)
        result["facts_digest"] = digest(canonical(facts))
        result["inventory_digest"] = digest(canonical(inventory))
        account_blockers = coverage_from_facts(
            scope, facts, inventory, excluded_position_symbols=exclusions
        )
        result["account_blockers"] = account_blockers
        _require(not set(account_blockers) - _ISOLATABLE, "ACCOUNT_REDUCTION_UNSAFE")
        target_owners = owners.get(symbol, [])
        _require(
            len(target_owners) == 1
            and target_owners[0]["episode"] == episode
            and target_owners[0]["side"] == side
            and amount(target_owners[0]["remaining"]) == remaining,
            "TARGET_OWNER_OR_QUANTITY_MISMATCH",
        )
        _require(
            any(
                row["symbol"] == symbol and row["leg"] == "OPEN" and row["has_fills"]
                for row in facts["orders"]
            ),
            "TARGET_OPENING_FILLS_REQUIRED",
        )
        quarantined = set()
        for row in facts["orders"]:
            if row["status"] in _TERMINAL:
                continue
            _require(row["symbol"] != symbol, "TARGET_ORDERS_NOT_FINAL")
            _require(
                row["leg"] == "CLOSE"
                and row["status"] in {"UNKNOWN", "ACKNOWLEDGED", "SUBMITTING"}
                and _exchange_id(row["exchange_order_id"])
                and row["symbol"] not in exclusions
                and len(owners.get(row["symbol"], [])) == 1,
                "UNLOCALIZED_PENDING_ORDER",
            )
            quarantined.add(row["symbol"])
        target_facts, target_inventory = _target_projection(facts, inventory, symbol)
        target_blockers = coverage_from_facts(scope, target_facts, target_inventory)
        _require(not target_blockers, "TARGET_COVERAGE_UNSAFE")
        remote = {
            (row["symbol"], row["algoId"])
            for row in inventory["responses"]["conditional_orders"]
        }
        protected = {
            row["payload"]["spec"]["symbol"]
            for row in facts["protections"]
            if row["payload"]["spec"]["kind"] == "STOP_MARKET"
            and (
                row["payload"]["spec"]["symbol"],
                row["payload"].get("observation", {}).get("algoId"),
            )
            in remote
        }
        quarantined.update(set(owners) - protected)
        _require(symbol not in quarantined, "TARGET_COVERAGE_UNSAFE")
        result.update(
            status="SYMBOL_REDUCTION_READY", quarantined_symbols=sorted(quarantined)
        )
    except _InvalidProof as exc:
        result["blockers"] = [str(exc)]
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        result["blockers"] = ["INVALID_REDUCTION_EVIDENCE"]
    return result
