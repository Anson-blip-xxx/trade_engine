"""Testnet exposure/stop coverage audit. Diagnostic only, never a trade permit.

Venue REST is not atomic. Bracket position reads and local ledger snapshots;
any ambiguity blocks the diagnostic. Does not repair, close or cancel anything.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from v2_core.account_inventory import AccountInventory
from v2_core.coverage_evidence import pack_coverage
from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol
from v2_core.ledger import amount
from v2_core.protection import ProtectionSpec
from v2_core.session_lock import session_advisory_lock
from v2_core.state import BusinessState, StateKey


def _excluded_positions(values):
    if not isinstance(values, (list, tuple)) or len(values) > 20:
        raise ValueError("bounded external position exclusions required")
    normalized = tuple(sorted(symbol(item) for item in values))
    if len(set(normalized)) != len(normalized):
        raise ValueError("unique external position exclusions required")
    return normalized


def _complete_inventory(observation):
    """Verify real source responses; derived modes are not extra REST reads."""
    raw, digests = observation["responses"], observation["response_digests"]
    common = {
        "positions_before",
        "positions_after",
        "account",
        "ordinary_orders",
        "conditional_orders",
    }
    combined = "account_config" in raw
    expected = common | ({"account_config"} if combined else {"mode", "margin_mode"})
    if set(raw) != expected or digests != {
        key: digest(canonical({"response": value})) for key, value in raw.items()
    }:
        return False
    if not combined:
        return (
            raw["mode"].get("dualSidePosition") is False
            and raw["margin_mode"].get("multiAssetsMargin") is False
        )
    from v2_core.account_inventory import account_config_derivations

    return (
        raw["account_config"].get("dualSidePosition") is False
        and raw["account_config"].get("multiAssetsMargin") is False
        and observation.get("derived_responses")
        == account_config_derivations(raw["account_config"])
    )


def coverage_from_facts(scope, facts, inventory, *, excluded_position_symbols=()):
    exclusions = set(_excluded_positions(excluded_position_symbols))
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
                if p["symbol"] not in exclusions
            }
            if set(owners) & exclusions:
                blockers.add("EXCLUDED_POSITION_HAS_LOCAL_OWNERSHIP")
            if {k: v for k, v in expected.items() if v} != actual:
                blockers.add("VENUE_LEDGER_POSITION_MISMATCH")
            if any(len(items) != 1 for items in owners.values()):
                blockers.add("NONEXCLUSIVE_SYMBOL_OWNERSHIP")
            if any(
                o["status"] not in {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}
                and not (
                    o["status"] == "PREPARED"
                    and o.get("leg") == "OPEN"
                    and o.get("exchange_order_id") is None
                    and o.get("has_fills") is False
                )
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
    def __init__(
        self,
        connect,
        request,
        *,
        scope,
        clock_ms,
        excluded_position_symbols=(),
    ):
        if (scope.exchange, scope.environment, scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ):
            raise ValueError("testnet futures coverage only")
        self.connect, self.scope = connect, scope
        self.excluded_position_symbols = _excluded_positions(excluded_position_symbols)
        self.inventory = AccountInventory(
            request,
            scope=scope,
            clock_ms=clock_ms,
            excluded_position_symbols=self.excluded_position_symbols,
            read_workers=self._read_workers,
            combined_account_config=self._combined_account_config,
        )

    def _combined_account_config(self):
        selected = BusinessState(self.connect).read(
            StateKey(
                **asdict(self.scope),
                namespace="account-inventory-policy-v1",
                key="current",
            )
        )
        if selected is None:
            return False
        payload = json.loads(selected.payload_json)
        if (
            selected.deleted
            or set(payload) != {"combined_account_config"}
            or type(payload["combined_account_config"]) is not bool
        ):
            raise ValueError("INVALID_ACCOUNT_INVENTORY_POLICY")
        return payload["combined_account_config"]

    def _read_workers(self):
        from v2_core.runtime_policy import PolicyStore

        return (
            PolicyStore(self.connect, self.scope).read().values["context.read_workers"]
        )

    def facts(self, *, connection=None):
        with (
            nullcontext(connection) if connection is not None else self.connect()
        ) as conn:
            if connection is None:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

            def read_pages(sql, params):
                # A historical trade count is not an exposure limit. Read all
                # rows in the same snapshot, using a bounded server-side fetch.
                result = []
                with conn.cursor(name="coverage_" + uuid4().hex) as cursor:
                    cursor.execute(sql, params)
                    while batch := cursor.fetchmany(1000):
                        result.extend(batch)
                return result

            episodes = read_pages(
                """SELECT i.intent_id::text,i.payload->>'symbol',i.payload->>'side',
                COALESCE(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0)::text,i.data_revision
                FROM v2_trade_intents i LEFT JOIN v2_orders o ON o.episode_id=i.intent_id
                LEFT JOIN v2_fills f USING(order_id)
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                GROUP BY i.intent_id ORDER BY i.intent_id""",
                tuple(asdict(self.scope).values()),
            )
            orders = read_pages(
                """SELECT o.order_id::text,i.payload->>'symbol',o.exchange_order_id,o.status,o.version,
                o.leg,EXISTS(SELECT 1 FROM v2_fills f WHERE f.order_id=o.order_id)
                FROM v2_orders o JOIN v2_trade_intents i ON o.episode_id=i.intent_id
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                ORDER BY o.order_id""",
                tuple(asdict(self.scope).values()),
            )
            protections = read_pages(
                """SELECT state_id::text,payload,version FROM v2_business_state
                WHERE NOT deleted AND scope->>'namespace'='testnet-protection-v1' AND scope @> %s::jsonb
                ORDER BY state_id""",
                (canonical(asdict(self.scope)),),
            )
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
                            "leg",
                            "has_fills",
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

    def reuse_after_protection(self, proof):
        """Reuse only this cycle's persisted diagnostic, never a POST permit.

        No latest-row lookup, timestamp refresh or cache. A miss takes the
        caller's original full audit path; actual reductions still collect
        their own fresh inventory under the original execution guards.
        """
        checked = self._protection_witness(proof)
        return checked[0] if checked is not None and checked[1] is False else None

    def expired_after_protection(self, proof):
        """Only a complete, unchanged persisted witness may request a refresh.

        This boolean is not coverage clearance. A stale, once-valid observation
        must be collected again and pass the ordinary five-second reuse fence.
        Tampering, changed facts, slow acquisition and unknown evidence do not
        receive this disposition.
        """
        checked = self._protection_witness(proof)
        return checked is not None and checked[1] is True

    def _protection_witness(self, proof):
        try:
            if not isinstance(proof, dict):
                return None
            identity = str(UUID(proof["observation_id"]))
            key = StateKey(
                **asdict(self.scope),
                namespace="account-coverage-evidence-v1",
                key=identity,
            )
            with session_advisory_lock(
                self.connect, "testnet-protocol-account:" + self.scope.key
            ) as acquired:
                if not acquired:
                    return None
                with self.connect() as conn:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    saved = BusinessState(lambda: nullcontext(conn)).read(key)
                    if saved is None or saved.deleted or saved.version != 1:
                        return None
                    source = conn.execute(
                        "SELECT reason FROM v2_state_history WHERE state_id=%s AND version=1",
                        (key.identity,),
                    ).fetchone()
                    if source != ("ACCOUNT_COVERAGE_AUDIT",):
                        return None
                    payload = json.loads(saved.payload_json)
                    observation, result = payload["inventory"], payload["result"]
                    exclusions = list(self.excluded_position_symbols)
                    if (
                        proof != {**result, "observation_id": identity}
                        or result["status"] != "ACCOUNT_COVERAGE_CLEAR"
                        or result["blockers"] != []
                        or result["execution_authorized"] is not False
                        or result["excluded_position_symbols"] != exclusions
                        or observation["observation_id"] != identity
                        or observation["account_scope"] != asdict(self.scope)
                        or observation["excluded_position_symbols"] != exclusions
                        or observation["execution_authorized"] is not False
                        or observation["failures"]
                        or not _complete_inventory(observation)
                        or payload["before"] != payload["after"]
                        or coverage_from_facts(
                            self.scope,
                            payload["after"],
                            observation,
                            excluded_position_symbols=self.excluded_position_symbols,
                        )
                        or self.facts(connection=conn) != payload["after"]
                    ):
                        return None
                    started, finished = (
                        observation["started_at_ms"],
                        observation["finished_at_ms"],
                    )
                    now = self.inventory.clock_ms()
                    if (
                        any(
                            type(value) is not int for value in (started, finished, now)
                        )
                        or not 0 <= started <= finished <= now
                        or finished > started + 5000
                    ):
                        return None
                    return (
                        {**result, "observation_id": identity},
                        now > started + 5000,
                    )
        except Exception:  # noqa: BLE001 - a diagnostic reuse miss must re-audit
            return None

    def run_once(self):
        with session_advisory_lock(
            self.connect, "testnet-protocol-account:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BUSY", "execution_authorized": False}
            before = self.facts()
            observation = self.inventory.collect(str(uuid4()))
            after = self.facts()
            blockers = coverage_from_facts(
                self.scope,
                after,
                observation,
                excluded_position_symbols=self.excluded_position_symbols,
            )
            if before != after:
                blockers = sorted(
                    set(blockers) | {"LOCAL_FACTS_CHANGED_DURING_INVENTORY"}
                )
            result = {
                "status": "ACCOUNT_COVERAGE_BLOCKED"
                if blockers
                else "ACCOUNT_COVERAGE_CLEAR",
                "blockers": blockers,
                "excluded_position_symbols": list(self.excluded_position_symbols),
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
                    payload=pack_coverage(
                        conn,
                        self.scope,
                        {
                            "inventory": observation,
                            "before": before,
                            "after": after,
                            "result": result,
                        },
                    ),
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
