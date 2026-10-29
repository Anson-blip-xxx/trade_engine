"""Read-only startup inventory; a clear inventory is NOT a trading permit.

Sequential REST responses are not atomic venue snapshots. Bracket positions to
detect quantity changes; retain response digests and the acquisition interval.
Persist each observation and its notification atomically in PG, with no files.
This intentionally blocks on ALL existing exposure until recovery owns it.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from uuid import UUID

from psycopg.types.json import Jsonb

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds, symbol
from v2_core.ledger import amount
from v2_core.operational import OperationalProjector
from v2_core.recovery_diagnostics import transport_evidence
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
ACCOUNT_CONFIG_PATH = "/fapi/v1/accountConfig"
COMBINED_ROUTES = (
    ("positions_before", "/fapi/v3/positionRisk"),
    ("account_config", ACCOUNT_CONFIG_PATH),
    *ROUTES[3:],
)


def account_config_derivations(response):
    """Explicit field projections, never fabricated endpoint responses.

    Only the two required mode booleans are interpreted. All other venue fields
    remain in the raw evidence and digest, including future API additions.
    updateTime is reserved and cannot establish freshness.
    """
    if not isinstance(response, dict) or any(
        type(response.get(field)) is not bool
        for field in ("dualSidePosition", "multiAssetsMargin")
    ):
        raise ValueError("INVALID_ACCOUNT_CONFIG_RESPONSE")
    source_digest = digest(canonical({"response": response}))
    return {
        name: {
            "response": {field: response[field]},
            "source_response": "account_config",
            "source_path": ACCOUNT_CONFIG_PATH,
            "source_digest": source_digest,
        }
        for name, field in (
            ("mode", "dualSidePosition"),
            ("margin_mode", "multiAssetsMargin"),
        )
    }


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
        read_workers=1,
        combined_account_config=False,
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
        if not callable(read_workers) and (
            type(read_workers) is not int or not 1 <= read_workers <= 4
        ):
            raise ValueError("bounded inventory read workers required")
        self.read_workers = read_workers
        if (
            not callable(combined_account_config)
            and type(combined_account_config) is not bool
        ):
            raise ValueError("explicit combined account configuration required")
        self.combined_account_config = combined_account_config

    def collect(self, observation_id):
        observation_id = str(UUID(observation_id))
        workers = (
            self.read_workers() if callable(self.read_workers) else self.read_workers
        )
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("bounded inventory read workers required")
        combined = (
            self.combined_account_config()
            if callable(self.combined_account_config)
            else self.combined_account_config
        )
        if type(combined) is not bool:
            raise ValueError("explicit combined account configuration required")
        routes = COMBINED_ROUTES if combined else ROUTES
        started = milliseconds(self.clock_ms())
        raw, failures, failure_details = {}, {}, {}

        def fetch(name, path):
            now = milliseconds(self.clock_ms())
            if not started <= now <= started + self.max_duration_ms:
                return name, None, "ACQUISITION_DEADLINE", None
            try:
                value = self.request("GET", path, {})
                canonical({"response": value})
                return name, value, None, None
            except Exception as exc:  # noqa: BLE001 - only fixed transport categories and numeric codes
                return name, None, "ACCOUNT_READ_UNAVAILABLE", transport_evidence(exc)

        def record(result):
            name, value, error, details = result
            if error:
                failures[name] = error
                if details:
                    failure_details[name] = details
            else:
                raw[name] = value

        if workers == 1:
            for name, path in routes:
                record(fetch(name, path))
                if failures:
                    break
        else:
            # Only independent GETs overlap; position observations bracket the
            # whole set, never reuse snapshots between protection and exit.
            record(fetch("positions_before", "/fapi/v3/positionRisk"))
            if not failures:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [
                        pool.submit(fetch, name, path)
                        for name, path in routes
                        if name not in {"positions_before", "positions_after"}
                    ]
                    for future in futures:
                        record(future.result())
                if not failures:
                    record(fetch("positions_after", "/fapi/v3/positionRisk"))
        finished = milliseconds(self.clock_ms())
        blockers = []
        derived = {}
        summary = {"positions": [], "ordinary_orders": [], "conditional_orders": []}
        if failures:
            blockers.append("INCOMPLETE_ACCOUNT_READ")
        if not started <= finished <= started + self.max_duration_ms:
            blockers.append("ACQUISITION_DEADLINE")
        if not failures:
            try:
                if combined:
                    derived = account_config_derivations(raw["account_config"])
                    mode = derived["mode"]["response"]
                    margin_mode = derived["margin_mode"]["response"]
                else:
                    mode, margin_mode = raw["mode"], raw["margin_mode"]
                if mode.get("dualSidePosition") is not False:
                    blockers.append("ONE_WAY_MODE_REQUIRED")
                if margin_mode.get("multiAssetsMargin") is not False:
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
            "failure_details": failure_details,
            "response_digests": {
                key: digest(canonical({"response": value}))
                for key, value in raw.items()
            },
            "responses": raw,
            **({"derived_responses": derived} if combined else {}),
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
