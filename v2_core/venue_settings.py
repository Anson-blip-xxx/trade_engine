"""Fail-closed Testnet symbol-setting coordination before an opening order.

Only an account without V2-managed exposure or any orders may be changed.
PostgreSQL records the desired settings before exchange I/O; every ambiguous
write is resolved by a fresh GET. This component cannot submit, cancel, protect
or close an order.
"""

import json
from dataclasses import asdict
from uuid import UUID, uuid4

from v2_core.account_inventory import AccountInventory
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.state import BusinessState, StateKey


class TestnetSymbolSettings:
    __test__ = False

    def __init__(
        self,
        connect,
        request,
        *,
        scope,
        clock_ms,
        allow_writes=False,
        excluded_position_symbols=(),
    ):
        if (
            not callable(connect)
            or not callable(request)
            or not callable(clock_ms)
            or not isinstance(scope, AccountScope)
            or (scope.exchange, scope.environment, scope.product)
            != ("BINANCE", "SANDBOX", "FUTURES")
            or (getattr(request, "account_id", None), getattr(request, "environment", None))
            != (scope.account_id, scope.environment)
            or type(allow_writes) is not bool
        ):
            raise ValueError("explicit Testnet symbol settings coordinator required")
        self.connect, self.request, self.scope = connect, request, scope
        self.clock, self.allow_writes = clock_ms, allow_writes
        self.inventory = AccountInventory(
            request,
            scope=scope,
            clock_ms=clock_ms,
            max_duration_ms=10000,
            excluded_position_symbols=excluded_position_symbols,
        )
        self.store = BusinessState(connect)

    @staticmethod
    def _desired(terms):
        if (
            not isinstance(terms, dict)
            or set(terms) != {"leverage", "margin_type"}
            or type(terms["leverage"]) is not int
            or not 1 <= terms["leverage"] <= 5
            or terms["margin_type"] not in {"ISOLATED", "CROSSED"}
        ):
            raise ValueError("bounded desired symbol settings required")
        return {"leverage": terms["leverage"], "margin_type": terms["margin_type"]}

    def _read(self, symbol):
        rows = self.request("GET", "/fapi/v1/symbolConfig", {"symbol": symbol})
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
            or rows[0].get("symbol") != symbol
            or type(rows[0].get("leverage")) is not int
            or rows[0].get("marginType") not in {"ISOLATED", "CROSSED"}
            or type(rows[0].get("isAutoAddMargin")) is not bool
        ):
            raise ValueError("invalid symbol settings response")
        return rows[0]

    @staticmethod
    def _matches(settings, desired):
        return (
            settings["leverage"] == desired["leverage"]
            and settings["marginType"] == desired["margin_type"]
            and settings["isAutoAddMargin"] is False
        )

    def ensure(self, order, terms):
        desired = self._desired(terms)
        if (
            not isinstance(order, dict)
            or order.get("leg") != "OPEN"
            or order.get("reduce_only") is not False
            or not isinstance(order.get("symbol"), str)
            or not order["symbol"].endswith("USDT")
        ):
            raise ValueError("opening order required for symbol settings")
        order_id = str(UUID(order["order_id"]))
        if order["symbol"] in self.inventory.excluded_position_symbols:
            raise ValueError("cannot configure an externally excluded position symbol")
        key = StateKey(
            **asdict(self.scope), namespace="venue-symbol-settings-v1", key=order_id
        )
        current = self._read(order["symbol"])
        if self._matches(current, desired):
            return {"status": "VERIFIED", "changed": [], "state_id": None}
        if not self.allow_writes:
            return {"status": "WRITE_DISABLED", "changed": [], "state_id": None}

        inventory = self.inventory.collect(str(uuid4()))
        summary = inventory.get("summary", {})
        if (
            inventory.get("failures")
            or inventory.get("blockers")
            or summary.get("positions")
            or summary.get("ordinary_orders")
            or summary.get("conditional_orders")
        ):
            return {"status": "ACCOUNT_NOT_FLAT", "changed": [], "state_id": None}
        payload = {
            "status": "CONFIGURING",
            "account_scope": asdict(self.scope),
            "order_id": order_id,
            "symbol": order["symbol"],
            "desired": desired,
            "inventory_id": inventory["observation_id"],
            "inventory_digest": digest(canonical(inventory)),
            "before_digest": digest(canonical(current)),
            "execution_authorized": False,
        }
        saved = self.store.read(key)
        if saved is None:
            result = self.store.change(
                key,
                expected_version=0,
                request_key="configure",
                payload=payload,
                reason="VENUE_SYMBOL_SETTINGS",
            )
            if result.code != "APPLIED":
                raise ValueError("symbol settings registration conflict")
        else:
            previous = json.loads(saved.payload_json)
            if previous.get("desired") != desired or previous.get("symbol") != order[
                "symbol"
            ]:
                raise ValueError("symbol settings identity conflict")

        changed = []
        operations = (
            (
                "margin_type",
                "/fapi/v1/marginType",
                {
                    "symbol": order["symbol"],
                    "marginType": desired["margin_type"],
                },
            ),
            (
                "leverage",
                "/fapi/v1/leverage",
                {"symbol": order["symbol"], "leverage": desired["leverage"]},
            ),
        )
        for field, path, params in operations:
            current = self._read(order["symbol"])
            matches = (
                current["marginType"] == desired["margin_type"]
                if field == "margin_type"
                else current["leverage"] == desired["leverage"]
            )
            if matches:
                continue
            try:
                self.request("POST", path, params)
            except Exception:  # noqa: BLE001, S110 - resolve only by fresh GET
                pass
            current = self._read(order["symbol"])
            matches = (
                current["marginType"] == desired["margin_type"]
                if field == "margin_type"
                else current["leverage"] == desired["leverage"]
            )
            if not matches:
                return {"status": "UNCONFIRMED", "changed": changed, "state_id": key.identity}
            changed.append(field)
        current = self._read(order["symbol"])
        if not self._matches(current, desired):
            return {"status": "UNCONFIRMED", "changed": changed, "state_id": key.identity}
        snapshot = self.store.read(key)
        result = self.store.change(
            key,
            expected_version=snapshot.version,
            request_key="verified",
            payload={
                **payload,
                "status": "VERIFIED",
                "changed": changed,
                "after_digest": digest(canonical(current)),
            },
            reason="VENUE_SYMBOL_SETTINGS_VERIFIED",
        )
        if result.code not in {"APPLIED", "ALREADY_APPLIED"}:
            raise ValueError("symbol settings verification conflict")
        return {"status": "VERIFIED", "changed": changed, "state_id": key.identity}
