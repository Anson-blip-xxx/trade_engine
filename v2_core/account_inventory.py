"""Read-only startup inventory; a clear inventory is NOT a trading permit.

Sequential REST responses are not atomic venue snapshots. Bracket positions to
detect quantity changes; retain response digests and the acquisition interval.
Persist each observation and its notification atomically in PG, with no files.
This intentionally blocks on ALL existing exposure until recovery owns it.
"""

from dataclasses import asdict
from uuid import UUID

from psycopg.types.json import Jsonb

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds, symbol
from v2_core.ledger import amount
from v2_core.operational import OperationalProjector
from v2_core.state import StateKey

ROUTES = (
    ("mode", "/fapi/v1/positionSide/dual"),
    ("margin_mode", "/fapi/v1/multiAssetsMargin"),
    ("positions_before", "/fapi/v3/positionRisk"),
    ("account", "/fapi/v3/account"),
    ("ordinary_orders", "/fapi/v1/openOrders"),
    ("conditional_orders", "/fapi/v1/openAlgoOrders"),
    ("positions_after", "/fapi/v3/positionRisk"),
)


class InventoryProjector(OperationalProjector):
    def __init__(self, connect, *, scope, sink):
        if not isinstance(scope, AccountScope):
            raise TypeError("explicit inventory account required")
        self.account = scope
        super().__init__(connect, "inventory-tg:" + digest(scope.key), sink)

    def _scope_filter(self):
        return (
            (
                "e.event_type='ACCOUNT_INVENTORY' AND e.scope_id=%s "
                "AND e.payload->'account_scope'=%s::jsonb"
            ),
            (self.account.environment, self.account.key),
        )


def positions(rows):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("INVALID_POSITIONS")
    result, seen = [], set()
    for row in rows:
        name = symbol(row["symbol"])
        side = row["positionSide"]
        if side not in {"BOTH", "LONG", "SHORT"} or (name, side) in seen:
            raise ValueError("INVALID_POSITION_IDENTITY")
        seen.add((name, side))
        quantity = amount(row["positionAmt"])
        if quantity:
            result.append({"symbol": name, "side": side, "quantity": str(quantity)})
    return sorted(result, key=lambda row: (row["symbol"], row["side"]))


def orders(rows, *, conditional):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("INVALID_ORDERS")
    result, seen = [], set()
    for row in rows:
        name = symbol(row["symbol"])
        identity = row["algoId" if conditional else "orderId"]
        if type(identity) is not int or identity < 0 or (name, identity) in seen:
            raise ValueError("INVALID_ORDER_IDENTITY")
        seen.add((name, identity))
        result.append({"symbol": name, "id": str(identity)})
    return sorted(result, key=lambda row: (row["symbol"], row["id"]))


class AccountInventory:
    def __init__(
        self,
        request,
        *,
        scope,
        clock_ms,
        max_duration_ms=30000,
        excluded_position_symbols=(),
    ):
        if not isinstance(scope, AccountScope) or not callable(clock_ms):
            raise TypeError("explicit scope and clock required")
        if not callable(request) or (
            getattr(request, "account_id", None),
            getattr(request, "environment", None),
        ) != (scope.account_id, scope.environment):
            raise ValueError("INVENTORY_TRANSPORT_SCOPE")
        if type(max_duration_ms) is not int or not 1 <= max_duration_ms <= 60000:
            raise ValueError("bounded inventory duration required")
        if (
            not isinstance(excluded_position_symbols, (list, tuple))
            or len(excluded_position_symbols) > 20
        ):
            raise ValueError("bounded external position exclusions required")
        exclusions = tuple(sorted(symbol(item) for item in excluded_position_symbols))
        if len(set(exclusions)) != len(exclusions):
            raise ValueError("unique external position exclusions required")
        self.request, self.scope, self.clock_ms = request, scope, clock_ms
        self.max_duration_ms = max_duration_ms
        self.excluded_position_symbols = exclusions

    def collect(self, observation_id):
        observation_id = str(UUID(observation_id))
        started = milliseconds(self.clock_ms())
        raw, failures = {}, {}
        for name, path in ROUTES:
            now = milliseconds(self.clock_ms())
            if not started <= now <= started + self.max_duration_ms:
                failures[name] = "ACQUISITION_DEADLINE"
                break
            try:
                value = self.request("GET", path, {})
                canonical({"response": value})
                raw[name] = value
            except Exception:  # noqa: BLE001 - do not retain URLs, keys or raw exception text
                failures[name] = "ACCOUNT_READ_UNAVAILABLE"
                break
        finished = milliseconds(self.clock_ms())
        blockers = []
        summary = {"positions": [], "ordinary_orders": [], "conditional_orders": []}
        if failures:
            blockers.append("INCOMPLETE_ACCOUNT_READ")
        if not started <= finished <= started + self.max_duration_ms:
            blockers.append("ACQUISITION_DEADLINE")
        if not failures:
            try:
                if raw["mode"].get("dualSidePosition") is not False:
                    blockers.append("ONE_WAY_MODE_REQUIRED")
                if raw["margin_mode"].get("multiAssetsMargin") is not False:
                    blockers.append("SINGLE_ASSET_MODE_REQUIRED")
                before_all = positions(raw["positions_before"])
                after_all = positions(raw["positions_after"])
                if before_all != after_all:
                    blockers.append("POSITION_CHANGED_DURING_INVENTORY")
                excluded = set(self.excluded_position_symbols)
                summary["positions"] = [
                    item for item in after_all if item["symbol"] not in excluded
                ]
                if self.excluded_position_symbols:
                    summary["excluded_positions"] = [
                        item for item in after_all if item["symbol"] in excluded
                    ]
                before = [item for item in before_all if item["symbol"] not in excluded]
                if before or summary["positions"]:
                    blockers.append("EXISTING_POSITION_REQUIRES_RECOVERY")
                for field, conditional in (
                    ("ordinary_orders", False),
                    ("conditional_orders", True),
                ):
                    summary[field] = orders(raw[field], conditional=conditional)
                    if summary[field]:
                        blockers.append("EXISTING_" + field.upper())
                account = raw["account"]
                summary["balance"] = {
                    key: str(amount(account[key]))
                    for key in (
                        "totalWalletBalance",
                        "availableBalance",
                        "totalMarginBalance",
                    )
                }
                if amount(summary["balance"]["availableBalance"]) <= 0:
                    blockers.append("NO_AVAILABLE_BALANCE")
                # V3 does not promise canTrade; absence is not permission.
                summary["trade_permission_verified"] = False
            except (ValueError, TypeError, KeyError, AttributeError):
                blockers.append("INVALID_ACCOUNT_RESPONSE")
        return {
            "observation_id": observation_id,
            "account_scope": asdict(self.scope),
            "started_at_ms": started,
            "finished_at_ms": finished,
            "status": "BLOCKED" if blockers else "CLEAR_FOR_RECOVERY_CHECKS",
            "execution_authorized": False,
            "excluded_position_symbols": list(self.excluded_position_symbols),
            "blockers": blockers,
            "summary": summary,
            "failures": failures,
            "response_digests": {
                key: digest(canonical({"response": value}))
                for key, value in raw.items()
            },
            "responses": raw,
        }


def persist_inventory(connect, *, scope, observation):
    """Immutable observation + notification in one transaction; retry by ID."""
    if not isinstance(scope, AccountScope) or observation["account_scope"] != asdict(
        scope
    ):
        raise ValueError("INVENTORY_PERSIST_SCOPE")
    identity = str(UUID(observation["observation_id"]))
    key = StateKey(**asdict(scope), namespace="account-inventory-v1", key=identity)
    encoded = canonical(observation)
    alert = {
        "account_id": scope.account_id,
        "account_scope": asdict(scope),
        "observation_id": identity,
        "status": observation["status"],
        "blockers": observation["blockers"],
        "position_count": len(observation["summary"]["positions"]),
        "ordinary_order_count": len(observation["summary"]["ordinary_orders"]),
        "conditional_order_count": len(observation["summary"]["conditional_orders"]),
    }
    with connect() as conn:
        conn.execute(
            """INSERT INTO v2_business_state(state_id,scope,version,payload,deleted)
            VALUES (%s,%s,1,%s,FALSE) ON CONFLICT DO NOTHING""",
            (key.identity, Jsonb(asdict(key)), Jsonb(observation)),
        )
        row = conn.execute(
            "SELECT scope,payload,version,deleted FROM v2_business_state WHERE state_id=%s FOR UPDATE",
            (key.identity,),
        ).fetchone()
        if row != (asdict(key), observation, 1, False):
            raise ValueError("INVENTORY_ID_CONFLICT")
        conn.execute(
            """INSERT INTO v2_state_history(event_id,state_id,request_key,expected_version,
            version,payload,deleted,reason) VALUES (%s,%s,%s,0,1,%s,FALSE,'ACCOUNT_INVENTORY')
            ON CONFLICT(state_id,request_key) DO NOTHING""",
            (identity, key.identity, identity, Jsonb(observation)),
        )
        conn.execute(
            """INSERT INTO v2_operational_outbox(event_id,scope_id,dedup_key,event_type,payload)
            VALUES (%s,%s,%s,'ACCOUNT_INVENTORY',%s) ON CONFLICT(scope_id,dedup_key) DO NOTHING""",
            (identity, scope.environment, "inventory:" + key.identity, Jsonb(alert)),
        )
    return {
        "observation_id": identity,
        "state_id": key.identity,
        "digest": digest(encoded),
        **alert,
    }
