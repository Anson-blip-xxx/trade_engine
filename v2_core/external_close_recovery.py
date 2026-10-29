"""Adopt confirmed external Testnet closes without ever writing to Binance.

A missing position is not a fill. Adoption needs exclusive local ownership,
complete order/trade identities, exactly the remaining quantity and two fresh
full inventories with no live target-symbol orders. All local writes commit in
one transaction; no PREPARED adoption is visible to submission workers.
"""

import json
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

from v2_core.account_inventory import AccountInventory, positions
from v2_core.binance import BinanceFutures, identifier
from v2_core.evidence import canonical
from v2_core.ledger import amount
from v2_core.orders import Orders
from v2_core.runner import ExecutionRunner
from v2_core.session_lock import session_advisory_lock
from v2_core.state import BusinessState, StateKey

ORIGIN = "BINANCE_EXTERNAL_CLOSE"
RESOLUTION_NAMESPACE = "external-close-resolution-v1"
_FINAL = {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}
_WEEK_MS = 7 * 24 * 60 * 60 * 1000


class ExternalCloseRecovery:
    def __init__(
        self,
        connect,
        request,
        *,
        scope,
        clock_ms,
        max_pages=5,
        max_duration_ms=60000,
    ):
        if (scope.exchange, scope.environment, scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ) or (
            getattr(request, "account_id", None),
            getattr(request, "environment", None),
        ) != (scope.account_id, scope.environment):
            raise ValueError("EXTERNAL_CLOSE_SCOPE_MISMATCH")
        if type(max_pages) is not int or not 1 <= max_pages <= 20:
            raise ValueError("bounded external close history required")
        if type(max_duration_ms) is not int or not 1 <= max_duration_ms <= 120000:
            raise ValueError("bounded external close duration required")
        self.connect, self.scope, self.clock_ms = connect, scope, clock_ms
        self.max_pages, self.max_duration_ms = max_pages, max_duration_ms
        self._deadline = ContextVar("external_close_deadline", default=None)

        def read(method, path, params):
            if method != "GET":
                raise ValueError("EXTERNAL_CLOSE_RECOVERY_IS_READ_ONLY")
            deadline = self._deadline.get()
            if deadline is not None and self.clock_ms() > deadline:
                raise ValueError("EXTERNAL_CLOSE_DEADLINE")
            return request(method, path, params)

        read.account_id, read.environment = scope.account_id, scope.environment
        self.request = read
        self.inventory = AccountInventory(read, scope=scope, clock_ms=clock_ms)
        self.venue = BinanceFutures(
            read,
            account_id=scope.account_id,
            environment=scope.environment,
            max_pages=max_pages,
        )

    def _key(self, episode):
        return StateKey(
            **asdict(self.scope), namespace=RESOLUTION_NAMESPACE, key=episode
        )

    def _facts(self, episode, *, connection=None):
        with self.connect() if connection is None else nullcontext(connection) as conn:
            row = conn.execute(
                "SELECT exchange,account_id,environment,product,payload "
                "FROM v2_trade_intents WHERE intent_id=%s",
                (episode,),
            ).fetchone()
            if row is None or row[:4] != tuple(asdict(self.scope).values()):
                raise ValueError("EXTERNAL_CLOSE_SCOPE_MISMATCH")
            orders = conn.execute(
                """SELECT o.order_id::text,o.leg,o.status,o.exchange_order_id,o.version,
                COALESCE(sum(f.quantity),0)::text,min(f.occurred_at_ms),max(f.occurred_at_ms)
                FROM v2_orders o LEFT JOIN v2_fills f USING(order_id)
                WHERE o.episode_id=%s GROUP BY o.order_id ORDER BY o.order_id""",
                (episode,),
            ).fetchall()
            owners = conn.execute(
                """SELECT i.intent_id::text FROM v2_trade_intents i
                JOIN v2_orders o ON o.episode_id=i.intent_id LEFT JOIN v2_fills f USING(order_id)
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                  AND i.payload->>'symbol'=%s GROUP BY i.intent_id
                HAVING sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)>0
                ORDER BY i.intent_id""",
                (*asdict(self.scope).values(), row[4]["symbol"]),
            ).fetchall()
            native_children = conn.execute(
                """SELECT payload->'observation'->>'actualOrderId' FROM v2_business_state
                WHERE NOT deleted AND scope->>'namespace'='testnet-protection-v1'
                  AND scope @> %s::jsonb AND payload->'spec'->>'episode'=%s""",
                (canonical(asdict(self.scope)), episode),
            ).fetchall()
        with localcontext() as ctx:
            ctx.prec = 100
            remaining = sum(
                (amount(o[5]) * (1 if o[1] == "OPEN" else -1) for o in orders),
                Decimal(0),
            )
        return {
            "episode": episode,
            "symbol": row[4]["symbol"],
            "side": row[4]["side"],
            "orders": orders,
            "owners": owners,
            "native_children": sorted(
                {row[0] for row in native_children if row[0] not in (None, "", "0")}
            ),
            "remaining": str(remaining),
        }

    def _flat(self, observation, name):
        if observation["account_scope"] != asdict(self.scope):
            raise ValueError("EXTERNAL_CLOSE_INVENTORY_SCOPE")
        allowed = {
            "EXISTING_POSITION_REQUIRES_RECOVERY",
            "EXISTING_ORDINARY_ORDERS",
            "EXISTING_CONDITIONAL_ORDERS",
            "NO_AVAILABLE_BALANCE",
        }
        if observation["failures"] or set(observation["blockers"]) - allowed:
            raise ValueError("EXTERNAL_CLOSE_INCOMPLETE_INVENTORY")
        if (
            not observation["started_at_ms"]
            <= observation["finished_at_ms"]
            <= self.clock_ms()
        ):
            raise ValueError("EXTERNAL_CLOSE_INVALID_INVENTORY_TIME")
        if self.clock_ms() - observation["finished_at_ms"] > 30000:
            raise ValueError("EXTERNAL_CLOSE_STALE_INVENTORY")
        return not any(
            row["symbol"] == name
            for field in ("positions", "ordinary_orders", "conditional_orders")
            for row in observation["summary"][field]
        )

    def _discover(self, facts, end):
        opened = [o for o in facts["orders"] if o[1] == "OPEN" and amount(o[5]) > 0]
        if not opened or any(o[6] is None for o in opened):
            raise ValueError("EXTERNAL_CLOSE_NO_OPEN_FILLS")
        start = min(o[6] for o in opened)
        if not 0 <= start <= end or end - start >= _WEEK_MS:
            raise ValueError("EXTERNAL_CLOSE_HISTORY_WINDOW_EXCEEDED")
        known = {o[3] for o in facts["orders"] if o[3] is not None}
        discovered, seen, cursor, complete = {}, set(), None, False
        for _ in range(self.max_pages):
            params = {"symbol": facts["symbol"], "limit": 1000}
            if cursor is None:
                params.update(startTime=start, endTime=end)
            else:
                params["fromId"] = cursor
            page = self.request("GET", "/fapi/v1/userTrades", params)
            if not isinstance(page, list) or len(page) > 1000:
                raise ValueError("EXTERNAL_CLOSE_INVALID_TRADE_PAGE")
            for trade in page:
                trade_id = identifier(trade["id"])
                if trade_id in seen or (cursor is not None and int(trade_id) < cursor):
                    raise ValueError("EXTERNAL_CLOSE_DUPLICATE_TRADE")
                seen.add(trade_id)
                if (
                    trade.get("symbol") != facts["symbol"]
                    or trade.get("positionSide") != "BOTH"
                    or type(trade.get("time")) is not int
                    or not start <= trade["time"] <= end
                ):
                    raise ValueError("EXTERNAL_CLOSE_TRADE_SCOPE_MISMATCH")
                order_id = identifier(trade["orderId"])
                if order_id in known:
                    continue
                if order_id in facts["native_children"]:
                    raise ValueError("EXTERNAL_CLOSE_NATIVE_CHILD_RECOVERY_REQUIRED")
                if trade.get("side") != ("SELL" if facts["side"] == "BUY" else "BUY"):
                    raise ValueError("EXTERNAL_CLOSE_UNOWNED_OPEN_FILL")
                discovered.setdefault(order_id, []).append(trade)
            if len(page) < 1000:
                complete = True
                break
            cursor = max(int(t["id"]) for t in page) + 1
        if not complete:
            raise ValueError("EXTERNAL_CLOSE_HISTORY_INCOMPLETE")
        if not discovered or len(discovered) > 20:
            raise ValueError("EXTERNAL_CLOSE_NO_BOUNDED_CLOSE_PROOF")
        confirmed = []
        for identity, discovered_trades in sorted(
            discovered.items(), key=lambda x: int(x[0])
        ):
            raw = self.request(
                "GET",
                "/fapi/v1/order",
                {"symbol": facts["symbol"], "orderId": int(identity)},
            )
            if not isinstance(raw, dict) or (
                raw.get("reduceOnly") is not True
                or raw.get("status") != "FILLED"
                or raw.get("type") not in {"MARKET", "LIMIT"}
                or str(raw.get("orderId")) != identity
            ):
                raise ValueError("EXTERNAL_CLOSE_UNCONFIRMED_REDUCE_ONLY_ORDER")
            candidate = {
                **asdict(self.scope),
                "symbol": facts["symbol"],
                "side": "SELL" if facts["side"] == "BUY" else "BUY",
                "quantity": raw["origQty"],
                "client_order_id": raw["clientOrderId"],
                "exchange_order_id": identity,
                "order_type": raw["type"],
                "reduce_only": True,
            }
            if raw["type"] == "LIMIT":
                candidate.update(
                    limit_price=raw["price"], time_in_force=raw["timeInForce"]
                )
            self.venue._scope(candidate)
            self.venue._order(candidate, raw)
            observation = self.venue.query(candidate)
            if (
                observation is None
                or observation.status != "FILLED"
                or observation.evidence.get("fills_complete") is not True
                or {f["exchange_fill_id"] for f in observation.fills}
                != {identifier(t["id"]) for t in discovered_trades}
                or any(
                    not start <= f["occurred_at_ms"] <= end for f in observation.fills
                )
            ):
                raise ValueError("EXTERNAL_CLOSE_FILL_PROOF_INCOMPLETE")
            binding = {
                "origin": ORIGIN,
                "exchange_order_id": identity,
                "venue_client_order_id": raw["clientOrderId"],
                "kind": "LIQUIDATION"
                if raw["clientOrderId"].startswith("autoclose-")
                else "EXTERNAL_REDUCE_ONLY",
                "venue_order": raw,
            }
            confirmed.append((candidate, observation, binding))
        with localcontext() as ctx:
            ctx.prec = 100
            total = sum((amount(c[0]["quantity"]) for c in confirmed), Decimal(0))
        if total != amount(facts["remaining"], positive=True):
            raise ValueError("EXTERNAL_CLOSE_RESIDUAL_QUANTITY_MISMATCH")
        return confirmed

    def _adopt(self, facts, confirmed, final_inventory):
        episode = facts["episode"]
        with self.connect() as conn:
            conn.execute(
                "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (episode,),
            ).fetchone()
            if self._facts(episode, connection=conn) != facts:
                raise ValueError("EXTERNAL_CLOSE_LOCAL_FACTS_CHANGED")
            if not self._flat(final_inventory, facts["symbol"]):
                raise ValueError("EXTERNAL_CLOSE_NOT_FLAT")
            state = BusinessState(lambda: nullcontext(conn))
            orders = Orders(lambda: nullcontext(conn))
            runner = ExecutionRunner(
                lambda: nullcontext(conn),
                submit=lambda _: None,
                query=lambda _: None,
                risk_check=lambda _: False,
                scope=self.scope,
            )
            close_ids = []
            for candidate, observation, binding in confirmed:
                identity = binding["exchange_order_id"]
                owner_key = StateKey(
                    **asdict(self.scope),
                    namespace="native-child-owner-v1",
                    key=facts["symbol"] + ":" + identity,
                )
                owner = state.read(owner_key)
                expected = {"episode": episode, "binding": binding}
                if owner and (
                    owner.deleted or owner.payload_json != canonical(expected)
                ):
                    raise ValueError("EXTERNAL_CLOSE_ALREADY_OWNED")
                foreign = conn.execute(
                    """SELECT o.order_id FROM v2_orders o JOIN v2_trade_intents i
                    ON i.intent_id=o.episode_id
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND i.payload->>'symbol'=%s AND o.exchange_order_id=%s""",
                    (*asdict(self.scope).values(), facts["symbol"], identity),
                ).fetchone()
                if foreign:
                    raise ValueError("EXTERNAL_CLOSE_ORDER_ALREADY_RECORDED")
                if owner is None and (
                    state.change(
                        owner_key,
                        expected_version=0,
                        request_key="bind",
                        payload=expected,
                        reason="CONFIRMED_EXTERNAL_CLOSE",
                    ).code
                    != "APPLIED"
                ):
                    raise ValueError("EXTERNAL_CLOSE_OWNER_RACE")
                order_id, client_id = orders.prepare(
                    episode,
                    leg="CLOSE",
                    quantity=candidate["quantity"],
                    request_key="external-close:" + identity,
                    order_type=candidate["order_type"],
                    limit_price=candidate.get("limit_price"),
                    time_in_force=candidate.get("time_in_force"),
                    evidence=binding,
                )
                orders.transition(
                    order_id,
                    expected_version=1,
                    status="SUBMITTING",
                    exchange_order_id=identity,
                    evidence={"reason": "EXCHANGE_CREATED_NO_POST", "binding": binding},
                )
                fills = tuple(
                    {
                        **fill,
                        "evidence": {
                            **fill["evidence"],
                            "external_close_binding": binding,
                        },
                    }
                    for fill in observation.fills
                )
                if (
                    runner._apply_locked(
                        conn,
                        order_id,
                        replace(
                            observation,
                            client_order_id=client_id,
                            fills=fills,
                            evidence={
                                **observation.evidence,
                                "external_close_binding": binding,
                            },
                        ),
                    )
                    != "FILLED"
                ):
                    raise ValueError("EXTERNAL_CLOSE_NOT_FINAL")
                close_ids.append(order_id)
            if amount(self._facts(episode, connection=conn)["remaining"]) != 0:
                raise ValueError("EXTERNAL_CLOSE_LEDGER_NOT_FLAT")
            if not confirmed:
                close_ids = [
                    o[0]
                    for o in facts["orders"]
                    if o[1] == "CLOSE" and amount(o[5]) > 0
                ]
                if not close_ids or any(
                    o[2] != "FILLED" for o in facts["orders"] if o[0] in close_ids
                ):
                    raise ValueError("EXTERNAL_CLOSE_FINAL_CLOSE_REQUIRED")
            exchange_ids = (
                [c[2]["exchange_order_id"] for c in confirmed]
                if confirmed
                else [o[3] for o in facts["orders"] if o[0] in close_ids]
            )
            if any(
                not isinstance(identity, str) or not identity
                for identity in exchange_ids
            ):
                raise ValueError("EXTERNAL_CLOSE_EXCHANGE_ID_REQUIRED")
            resolution = {
                "status": "CONFIRMED",
                "account_scope": asdict(self.scope),
                "episode": episode,
                "symbol": facts["symbol"],
                "exchange_order_ids": exchange_ids,
                "close_order_ids": close_ids,
                "remaining_quantity": "0",
                "inventory": {
                    key: final_inventory[key]
                    for key in (
                        "observation_id",
                        "started_at_ms",
                        "finished_at_ms",
                        "response_digests",
                        "summary",
                    )
                },
                "observed_at_ms": self.clock_ms(),
            }
            resolution_key = self._key(episode)
            if (
                state.change(
                    resolution_key,
                    expected_version=0,
                    request_key="confirmed",
                    payload=resolution,
                    reason="CONFIRMED_EXTERNAL_CLOSE",
                ).code
                != "APPLIED"
            ):
                raise ValueError("EXTERNAL_CLOSE_RESOLUTION_CONFLICT")
            protections = conn.execute(
                """SELECT scope,payload,version FROM v2_business_state WHERE NOT deleted
                AND scope->>'namespace'='testnet-protection-v1' AND scope @> %s::jsonb
                AND payload->'spec'->>'episode'=%s FOR UPDATE""",
                (canonical(asdict(self.scope)), episode),
            ).fetchall()
            for key, payload, version in protections:
                if payload["spec"]["symbol"] != facts["symbol"]:
                    raise ValueError("EXTERNAL_CLOSE_PROTECTION_SCOPE_MISMATCH")
                if payload.get("status") in {
                    "FINISHED",
                    "CANCELED",
                    "EXPIRED",
                    "REJECTED",
                }:
                    continue
                updated = {
                    **payload,
                    "status": "EXTERNAL_FLAT_RECONCILED",
                    "external_reconciliation": {
                        "state_id": resolution_key.identity,
                        "episode": episode,
                        "symbol": facts["symbol"],
                        "account_scope": asdict(self.scope),
                    },
                }
                if (
                    state.change(
                        StateKey(**key),
                        expected_version=version,
                        request_key="external-flat:" + resolution_key.identity,
                        payload=updated,
                        reason="CONFIRMED_EXTERNAL_CLOSE",
                    ).code
                    != "APPLIED"
                ):
                    raise ValueError("EXTERNAL_CLOSE_PROTECTION_RACE")
        return {
            "status": "RECONCILED",
            "episode": episode,
            "symbol": facts["symbol"],
            "close_order_ids": close_ids,
            "resolution_state_id": resolution_key.identity,
        }

    def reconcile(self, episode):
        token = self._deadline.set(self.clock_ms() + self.max_duration_ms)
        try:
            return self._reconcile(episode)
        finally:
            self._deadline.reset(token)

    def _reconcile(self, episode):
        episode = str(UUID(episode))
        started = self.clock_ms()
        with session_advisory_lock(
            self.connect, "testnet-protocol-account:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BUSY", "episode": episode}
            facts = self._facts(episode)
            if amount(facts["remaining"]) == 0:
                previous = BusinessState(self.connect).read(self._key(episode))
                if (
                    previous
                    and not previous.deleted
                    and json.loads(previous.payload_json)["status"] == "CONFIRMED"
                ):
                    return {"status": "ALREADY_RECONCILED", "episode": episode}
                if any(o[2] not in _FINAL for o in facts["orders"]):
                    raise ValueError("EXTERNAL_CLOSE_LOCAL_ORDERS_NOT_FINAL")
                # Locally managed emergency closes also need to retire an
                # ambiguous old protection without inventing a cancel result.
                flat = self.inventory.collect(str(uuid4()))
                if not self._flat(flat, facts["symbol"]):
                    return {"status": "VENUE_EXPOSURE_OR_ORDERS", "episode": episode}
                return self._adopt(facts, [], flat)
            if facts["owners"] != [(episode,)]:
                raise ValueError("EXTERNAL_CLOSE_NONEXCLUSIVE_OWNERSHIP")
            if any(o[2] not in _FINAL for o in facts["orders"]):
                raise ValueError("EXTERNAL_CLOSE_LOCAL_ORDERS_NOT_FINAL")
            before = self.inventory.collect(str(uuid4()))
            if not self._flat(before, facts["symbol"]):
                return {"status": "VENUE_EXPOSURE_OR_ORDERS", "episode": episode}
            confirmed = self._discover(facts, self.clock_ms())
            after = self.inventory.collect(str(uuid4()))
            if not self._flat(after, facts["symbol"]):
                raise ValueError("EXTERNAL_CLOSE_VENUE_CHANGED")
            if not started <= self.clock_ms() <= started + self.max_duration_ms:
                raise ValueError("EXTERNAL_CLOSE_DEADLINE")
            return self._adopt(facts, confirmed, after)

    def run_once(self, limit=5):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("bounded external close batch required")
        # CLEAR only describes this flat-position reconciliation stage. It is
        # not proof that all exposure or native protection has been verified;
        # the lifecycle must still require its independent coverage audit.
        summary = {
            "scope": "EXTERNAL_FLAT_RECONCILIATION_ONLY",
            "account_safety_verified": False,
            "skipped": [],
            "skipped_count": 0,
            "skipped_with_nonfinal_orders_count": 0,
            "skipped_truncated": False,
        }
        with self.connect() as conn:
            candidates = conn.execute(
                """SELECT i.intent_id::text,i.payload->>'symbol',
                EXISTS (SELECT 1 FROM v2_orders pending WHERE pending.episode_id=i.intent_id
                  AND pending.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED'))
                FROM v2_trade_intents i
                JOIN v2_episodes e ON e.episode_id=i.intent_id
                JOIN v2_orders o ON o.episode_id=i.intent_id JOIN v2_fills f USING(order_id)
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                  AND e.status='ACTIVE'
                GROUP BY i.intent_id
                HAVING sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)>0
                  OR EXISTS (SELECT 1 FROM v2_business_state s WHERE NOT s.deleted
                    AND s.scope->>'namespace'='testnet-protection-v1'
                    AND s.scope @> %s::jsonb AND s.payload->'spec'->>'episode'=i.intent_id::text
                    AND s.payload->>'status' NOT IN ('EXTERNAL_FLAT_RECONCILED','FINISHED','CANCELED','EXPIRED','REJECTED'))
                ORDER BY min(f.occurred_at_ms),i.intent_id LIMIT %s""",
                (*asdict(self.scope).values(), canonical(asdict(self.scope)), 1001),
            ).fetchall()
        summary["candidate_count"] = len(candidates)
        if not candidates:
            return {"status": "CLEAR", "results": [], **summary}
        if len(candidates) > 1000:
            return {
                "status": "BLOCKED",
                "results": [],
                "reason": "EXTERNAL_CLOSE_CANDIDATE_BOUND",
                **summary,
            }
        # Fast negative screen: healthy open positions do not incur a full
        # inventory and history audit every cycle. Never adopt from this screen.
        active = {
            p["symbol"]
            for p in positions(self.request("GET", "/fapi/v3/positionRisk", {}))
        }
        flat_candidates = [
            (episode, name)
            for episode, name, _pending in candidates
            if name not in active
        ]
        skipped = [
            {
                "episode": episode,
                "symbol": name,
                "status": "SKIPPED",
                "reason": "VENUE_POSITION_NONZERO",
                "local_orders_final": not pending,
            }
            for episode, name, pending in candidates
            if name in active
        ]
        summary.update(
            skipped=skipped[:limit],
            skipped_count=len(skipped),
            skipped_with_nonfinal_orders_count=sum(
                not row["local_orders_final"] for row in skipped
            ),
            skipped_truncated=len(skipped) > limit,
        )
        results, blocked = [], len(flat_candidates) > limit
        for episode, _name in flat_candidates[:limit]:
            try:
                result = self.reconcile(episode)
            except Exception as exc:  # noqa: BLE001 - bounded sanitized per-episode failure
                from v2_core.transport import ExchangeTransportError

                result = {
                    "status": "BLOCKED",
                    "episode": episode,
                    "error_code": type(exc).__name__,
                }
                if isinstance(exc, ValueError) and str(exc).startswith(
                    "EXTERNAL_CLOSE_"
                ):
                    result["reason"] = str(exc)
                if isinstance(exc, ExchangeTransportError):
                    result["transport"] = exc.diagnostic_evidence()
            results.append(result)
            blocked = blocked or result["status"] in {"BLOCKED", "BUSY"}
        return {
            "status": "BLOCKED" if blocked else "CLEAR",
            "results": results,
            "remaining_candidates": max(0, len(flat_candidates) - limit),
            **summary,
        }
