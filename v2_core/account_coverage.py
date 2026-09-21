"""Testnet exposure/stop coverage audit. Diagnostic only, never a trade permit.

Venue REST is not atomic. Bracket position reads and local ledger snapshots;
any ambiguity blocks the diagnostic. Does not repair, close or cancel anything.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal, localcontext
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_inventory import AccountInventory
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.protection import ProtectionSpec
from v2_core.state import BusinessState, StateKey


def coverage_from_facts(scope, facts, inventory):
    if inventory["account_scope"] != asdict(scope):
        raise ValueError("COVERAGE_SCOPE_MISMATCH")
    blockers = set(inventory["blockers"]) - {
        "EXISTING_POSITION_REQUIRES_RECOVERY",
        "EXISTING_ORDINARY_ORDERS",
        "EXISTING_CONDITIONAL_ORDERS",
    }
    if inventory["failures"]:
        blockers.add("INCOMPLETE_ACCOUNT_READ")
    if blockers:
        return sorted(blockers)
    raw = inventory["responses"]
    try:
        with localcontext() as ctx:
            ctx.prec = 100
            expected, owners = {}, {}
            for episode in facts["episodes"]:
                quantity = amount(episode["remaining"])
                if quantity < 0:
                    blockers.add("LEDGER_OVERCLOSED")
                if not quantity:
                    continue
                symbol = episode["symbol"]
                owners.setdefault(symbol, []).append(episode)
                expected[symbol] = expected.get(symbol, Decimal(0)) + quantity * (
                    1 if episode["side"] == "BUY" else -1
                )
            actual = {
                p["symbol"]: amount(p["quantity"])
                for p in inventory["summary"]["positions"]
            }
            if {k: v for k, v in expected.items() if v} != actual:
                blockers.add("VENUE_LEDGER_POSITION_MISMATCH")
            if any(len(items) != 1 for items in owners.values()):
                blockers.add("NONEXCLUSIVE_SYMBOL_OWNERSHIP")
            if any(
                o["status"] not in {"FILLED", "CANCELLED", "REJECTED"}
                for o in facts["orders"]
            ):
                blockers.add("LOCAL_ORDERS_NOT_FINAL")
            known_orders = {
                (o["symbol"], o["exchange_order_id"])
                for o in facts["orders"]
                if o["exchange_order_id"]
            }
            if raw["ordinary_orders"]:
                blockers.add("OPEN_ORDINARY_ORDERS")
            for row in raw["ordinary_orders"]:
                if (row["symbol"], str(row["orderId"])) not in known_orders:
                    blockers.add("UNOWNED_ORDINARY_ORDER")
            known_algos, covered = set(), set()
            remote = {(r["symbol"], r["algoId"]): r for r in raw["conditional_orders"]}
            for record in facts["protections"]:
                payload = record["payload"]
                spec = ProtectionSpec(**payload["spec"])
                key = StateKey(
                    **asdict(scope),
                    namespace="testnet-protection-v1",
                    key=canonical(
                        {
                            "episode": spec.episode,
                            "symbol": spec.symbol,
                            "kind": spec.kind,
                        }
                    ),
                )
                if key.identity != record["state_id"]:
                    raise ValueError("protection identity mismatch")
                observation = payload.get("observation", {})
                algo_id = observation.get("algoId")
                if algo_id is None:
                    continue
                identity = (spec.symbol, algo_id)
                if identity in known_algos:
                    blockers.add("DUPLICATE_ALGO_OWNERSHIP")
                known_algos.add(identity)
                live = remote.get(identity)
                if live is None:
                    continue
                owner = next(
                    (
                        e
                        for e in owners.get(spec.symbol, [])
                        if e["episode"] == spec.episode
                    ),
                    None,
                )
                if owner is None:
                    blockers.add("CONDITIONAL_WITHOUT_LEDGER_EXPOSURE")
                    continue
                if (
                    payload.get("status") != "NEW"
                    or live.get("algoStatus") != "NEW"
                    or live.get("clientAlgoId") != "v2p-" + digest(key.identity)[:28]
                    or live.get("side") != ("SELL" if owner["side"] == "BUY" else "BUY")
                    or live.get("side") != spec.side
                    or live.get("positionSide") != "BOTH"
                    or live.get("orderType") != spec.kind
                    or live.get("algoType") != "CONDITIONAL"
                    or live.get("closePosition") is not True
                    or live.get("workingType") != "MARK_PRICE"
                    or live.get("priceProtect") is not False
                    or amount(live["triggerPrice"], positive=True)
                    != amount(spec.trigger_price, positive=True)
                ):
                    blockers.add("PROTECTION_TERMS_MISMATCH")
                    continue
                if spec.kind == "STOP_MARKET":
                    covered.add(spec.episode)
            if set(remote) - known_algos:
                blockers.add("UNOWNED_CONDITIONAL_ORDER")
            for items in owners.values():
                for episode in items:
                    if episode["episode"] not in covered:
                        blockers.add("MISSING_CONFIRMED_STOP")
    except (ValueError, TypeError, KeyError, AttributeError):
        blockers.add("INVALID_COVERAGE_FACTS")
    return sorted(blockers)


class AccountCoverageAudit:
    def __init__(self, connect, request, *, scope, clock_ms):
        if (scope.exchange, scope.environment, scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ):
            raise ValueError("testnet futures coverage only")
        self.connect, self.scope = connect, scope
        self.inventory = AccountInventory(request, scope=scope, clock_ms=clock_ms)

    def facts(self):
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            episodes = conn.execute(
                """SELECT i.intent_id::text,i.payload->>'symbol',i.payload->>'side',
                COALESCE(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0)::text,i.data_revision
                FROM v2_trade_intents i LEFT JOIN v2_orders o ON o.episode_id=i.intent_id
                LEFT JOIN v2_fills f USING(order_id)
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                GROUP BY i.intent_id ORDER BY i.intent_id LIMIT 1001""",
                tuple(asdict(self.scope).values()),
            ).fetchall()
            orders = conn.execute(
                """SELECT o.order_id::text,i.payload->>'symbol',o.exchange_order_id,o.status,o.version
                FROM v2_orders o JOIN v2_trade_intents i ON o.episode_id=i.intent_id
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                ORDER BY o.order_id LIMIT 1001""",
                tuple(asdict(self.scope).values()),
            ).fetchall()
            protections = conn.execute(
                """SELECT state_id::text,payload,version FROM v2_business_state
                WHERE NOT deleted AND scope->>'namespace'='testnet-protection-v1' AND scope @> %s::jsonb
                ORDER BY state_id LIMIT 1001""",
                (canonical(asdict(self.scope)),),
            ).fetchall()
        if any(len(rows) > 1000 for rows in (episodes, orders, protections)):
            raise ValueError("COVERAGE_SCAN_CAPACITY_EXCEEDED")
        return {
            "episodes": [
                dict(
                    zip(
                        ("episode", "symbol", "side", "remaining", "revision"),
                        r,
                        strict=True,
                    )
                )
                for r in episodes
            ],
            "orders": [
                dict(
                    zip(
                        (
                            "order_id",
                            "symbol",
                            "exchange_order_id",
                            "status",
                            "version",
                        ),
                        r,
                        strict=True,
                    )
                )
                for r in orders
            ],
            "protections": [
                dict(zip(("state_id", "payload", "version"), r, strict=True))
                for r in protections
            ],
        }

    def run_once(self):
        with self.connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + self.scope.key,),
            ).fetchone()[0]:
                return {"status": "BUSY", "execution_authorized": False}
            guard.commit()
            before = self.facts()
            observation = self.inventory.collect(str(uuid4()))
            after = self.facts()
            blockers = coverage_from_facts(self.scope, after, observation)
            if before != after:
                blockers = sorted(
                    set(blockers) | {"LOCAL_FACTS_CHANGED_DURING_INVENTORY"}
                )
            result = {
                "status": "ACCOUNT_COVERAGE_BLOCKED"
                if blockers
                else "ACCOUNT_COVERAGE_CLEAR",
                "blockers": blockers,
                "execution_authorized": False,
            }
            identity = observation["observation_id"]
            latest_key = StateKey(
                **asdict(self.scope), namespace="account-coverage-v1", key="latest"
            )
            evidence_key = StateKey(
                **asdict(self.scope),
                namespace="account-coverage-evidence-v1",
                key=identity,
            )
            with self.connect() as conn:
                store = BusinessState(lambda: nullcontext(conn))
                previous = store.read(latest_key)
                store.change(
                    evidence_key,
                    expected_version=0,
                    request_key=identity,
                    payload={
                        "inventory": observation,
                        "before": before,
                        "after": after,
                        "result": result,
                    },
                    reason="ACCOUNT_COVERAGE_AUDIT",
                )
                version = previous.version if previous else 0
                saved = store.change(
                    latest_key,
                    expected_version=version,
                    request_key=identity,
                    payload={**result, "observation_id": identity},
                    reason="ACCOUNT_COVERAGE_AUDIT",
                )
                if saved.code != "APPLIED":
                    raise ValueError("COVERAGE_CHECKPOINT_CONFLICT")
                was_blocked = (
                    previous
                    and json.loads(previous.payload_json)["status"]
                    == "ACCOUNT_COVERAGE_BLOCKED"
                )
                if blockers or was_blocked:
                    bucket = conn.execute(
                        "SELECT floor(extract(epoch FROM clock_timestamp())/300)::bigint"
                    ).fetchone()[0]
                    conn.execute(
                        "INSERT INTO v2_operational_outbox(event_id,scope_id,dedup_key,event_type,payload) VALUES (%s,%s,%s,'PROTECTION_RECOVERY',%s) ON CONFLICT(scope_id,dedup_key) DO NOTHING",
                        (
                            str(uuid4()),
                            self.scope.environment,
                            f"coverage:{digest(self.scope.key)}:{digest(canonical(result))}:{bucket}",
                            Jsonb(
                                {
                                    **result,
                                    "account_scope": asdict(self.scope),
                                    "state_id": evidence_key.identity,
                                }
                            ),
                        ),
                    )
            return {**result, "observation_id": identity}
