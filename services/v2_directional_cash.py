"""Closed directional episode cash audit, not final account settlement.

Uses actual paginated income import, immutable fill economics and funding-time
ownership. Never sets completeness flags or marks an episode SETTLED. Matching
cash is necessary, not sufficient: wallet/exposure reconciliation remains separate.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal
from uuid import UUID, uuid4

from v2_core.binance_income import BinanceIncomeImporter
from v2_core.evidence import canonical, digest
from v2_core.income_ownership import income_owner
from v2_core.ledger import amount
from v2_core.liquidation_cash import insurance_clear_candidate
from v2_core.native_entry_precision import ORIGINS, protection_key
from v2_core.precision import money_context
from v2_core.runtime_policy import PolicyStore
from v2_core.scheduling import diagnostic_code
from v2_core.service import TradingData
from v2_core.session_lock import session_advisory_lock
from v2_core.settlement_precision import rounding_proof
from v2_core.state import BusinessState, StateKey


def _native_entry_observation(conn, scope, trace, order):
    """Read one exact immutable stop creation and adopted-order ownership.

    Never search neighboring snapshots for a price that explains the result.
    Current head cannot supply or replace creation-time entry economics.
    """
    request = order["request_evidence"]
    key = protection_key(asdict(scope), trace["intent_id"], trace["request"]["symbol"])
    client = "v2p-" + digest(key.identity)[:28]
    source_client = request.get(
        "parent_client_algo_id"
        if request["origin"] == "BINANCE_ALGO_CHILD"
        else "venue_client_order_id"
    )
    if source_client != client or (
        request["origin"] == "BINANCE_EXTERNAL_CLOSE"
        and request.get("kind") != "EXTERNAL_REDUCE_ONLY"
    ):
        return None
    row = conn.execute(
        """SELECT s.scope,s.version,s.payload,s.deleted,h.payload,h.deleted,
        floor(extract(epoch FROM h.created_at)*1000)::bigint,h.reason
        FROM v2_business_state s JOIN v2_state_history h USING(state_id)
        WHERE s.state_id=%s AND h.version=1""",
        (key.identity,),
    ).fetchone()
    if (
        row is None
        or row[0] != asdict(key)
        or row[3]
        or row[5]
        or row[7] != "TESTNET_PROTECTION_PROTOCOL"
    ):
        raise ValueError("PNL_ENTRY_OBSERVATION_INVALID")
    owner_key = StateKey(
        **asdict(scope),
        namespace="native-child-owner-v1",
        key=trace["request"]["symbol"] + ":" + request["exchange_order_id"],
    )
    owner = conn.execute(
        """SELECT s.scope,s.version,s.payload,s.deleted,h.payload,h.deleted,h.reason
        FROM v2_business_state s JOIN v2_state_history h USING(state_id)
        WHERE s.state_id=%s AND h.version=1""",
        (owner_key.identity,),
    ).fetchone()
    reason = (
        "CONFIRMED_PARENT_CHILD_IDENTITY"
        if request["origin"] == "BINANCE_ALGO_CHILD"
        else "CONFIRMED_EXTERNAL_CLOSE"
    )
    if (
        owner is None
        or owner[0] != asdict(owner_key)
        or owner[1] != 1
        or owner[3]
        or owner[2] != owner[4]
        or owner[5]
        or owner[6] != reason
    ):
        raise ValueError("PNL_ENTRY_OBSERVATION_INVALID")
    return {
        "inventory": row[4]["creation_proof"]["inventory"],
        "protection_creation": row[4],
        "protection_state": row[2],
        "protection_state_id": key.identity,
        "protection_creation_version": 1,
        "protection_state_version": row[1],
        "creation_recorded_at_ms": row[6],
        "protection_owner": owner[2],
    }


def _entry_observations(conn, scope, trace):
    """Read only exact immutable preflight references, never search old inventory."""
    result = {}
    closing = [o for o in trace["orders"] if o["leg"] == "CLOSE"]
    if len(closing) > 20:
        raise ValueError("PNL_ENTRY_OBSERVATION_LIMIT")
    for order in closing:
        request = order.get("request_evidence", {})
        if request.get("origin") in ORIGINS:
            witness = _native_entry_observation(conn, scope, trace, order)
            if witness is not None:
                result[order["order_id"]] = witness
            continue
        if request.get("source") not in {
            "directional-exit-v1",
            "unprotected-emergency-v1",
        }:
            continue
        identity = str(UUID(request["preflight_inventory"]))
        key = StateKey(**asdict(scope), namespace="account-inventory-v1", key=identity)
        row = conn.execute(
            "SELECT scope,version,payload,deleted FROM v2_business_state WHERE state_id=%s",
            (key.identity,),
        ).fetchone()
        if row is None or row[0] != asdict(key) or row[1] != 1 or row[3]:
            raise ValueError("PNL_ENTRY_OBSERVATION_INVALID")
        witness = {"inventory": row[2]}
        if request["source"] == "directional-exit-v1":
            key = StateKey(
                **asdict(scope), namespace="directional-exit-v1", key=trace["intent_id"]
            )
            if (
                request.get("state_id") != key.identity
                or type(request.get("state_version")) is not int
                or request["state_version"] < 1
            ):
                raise ValueError("PNL_ENTRY_OBSERVATION_INVALID")
            row = conn.execute(
                """SELECT s.scope,h.payload,h.deleted FROM v2_state_history h
                JOIN v2_business_state s USING(state_id)
                WHERE h.state_id=%s AND h.version=%s""",
                (key.identity, request["state_version"]),
            ).fetchone()
            if row is None or row[0] != asdict(key) or row[2]:
                raise ValueError("PNL_ENTRY_OBSERVATION_INVALID")
            witness["exit_state"] = row[1]
        result[order["order_id"]] = witness
    return result


def reconcile_cash(trace, report, rows, *, scope, entry_observations=None):
    """All inputs must come from the bound journal, never caller completeness flags."""
    if any(trace["scope"].get(k) != v for k, v in asdict(scope).items()):
        raise ValueError("CASH_SCOPE_MISMATCH")
    if trace["configuration"].get("mode") != "TESTNET_ADMISSION_ONLY" or trace["scope"][
        "producer"
    ] not in {"s6", "s8"}:
        raise ValueError("DIRECTIONAL_EVIDENCE_REQUIRED")
    orders = {o["order_id"]: o for o in trace["orders"]}
    if not orders or any(
        o["status"] not in {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}
        for o in orders.values()
    ):
        raise ValueError("ORDERS_NOT_FINAL")
    if (
        report["intent_id"] != trace["intent_id"]
        or report["settlement_currency"] != "USDT"
        or report["missing_valuations"]
    ):
        raise ValueError("UNSUPPORTED_CASH_CURRENCY")
    if not trace["request"]["symbol"].endswith("USDT") or trace["cash"]:
        raise ValueError("EXISTING_CASH_REQUIRES_RECONCILIATION")
    with money_context():
        expected, totals, trades, timeline = {}, {}, set(), []
        for fill in trace["fills"]:
            proof = fill["evidence"]
            tid = proof["trade_id"]
            if (
                proof["source"] != "binance-userTrades"
                or fill["fee_currency"] != "USDT"
                or tid in trades
            ):
                raise ValueError("INVALID_CASH_FILL_EVIDENCE")
            trades.add(tid)
            quantity = amount(fill["quantity"], positive=True)
            oid = fill["order_id"]
            totals[oid] = totals.get(oid, Decimal(0)) + quantity
            timeline.append(
                (
                    fill["occurred_at_ms"],
                    quantity * (1 if orders[oid]["leg"] == "OPEN" else -1),
                )
            )
            expected[("COMMISSION", tid)] = -amount(fill["fee"])
            expected[("REALIZED_PNL", tid)] = amount(proof["venue_realized_pnl"])
        if not timeline:
            raise ValueError("MISSING_FILLS")
        for oid, order in orders.items():
            filled, requested = (
                totals.get(oid, Decimal(0)),
                amount(order["quantity"], positive=True),
            )
            if (
                filled > requested
                or (order["status"] == "FILLED" and filled != requested)
                or (order["status"] == "REJECTED" and filled)
            ):
                raise ValueError("FILL_QUANTITY_MISMATCH")
        if amount(report["opened_quantity"], positive=True) != amount(
            report["closed_quantity"]
        ):
            raise ValueError("EXPOSURE_NOT_CLOSED")
        balance = Decimal(0)
        for timestamp in sorted({t for t, _ in timeline}):
            balance += sum((q for t, q in timeline if t == timestamp), Decimal(0))
            if balance < 0:
                raise ValueError("INVALID_EXPOSURE_TIMELINE")
        if balance != 0:
            raise ValueError("EXPOSURE_NOT_CLOSED")
        actual, funding, insurance, seen = {}, [], [], set()
        for row in rows:
            identity = (row["income_type"], row["source_id"])
            if (
                identity in seen
                or row["symbol"] != trace["request"]["symbol"]
                or row["currency"] != "USDT"
                or row["evidence"]["source"] != "binance-income"
            ):
                raise ValueError("UNATTRIBUTED_CASH")
            seen.add(identity)
            value = amount(row["amount"])
            if row["income_type"] == "INSURANCE_CLEAR":
                insurance.append(insurance_clear_candidate(trace, row))
            elif row["income_type"] == "FUNDING_FEE":
                when = row["occurred_at_ms"]
                if (
                    any(t == when for t, _ in timeline)
                    or sum((q for t, q in timeline if t < when), Decimal(0)) <= 0
                ):
                    raise ValueError("AMBIGUOUS_FUNDING_OWNERSHIP")
                funding.append(
                    {
                        "income_id": row["income_id"],
                        "amount": str(value),
                        "occurred_at_ms": when,
                    }
                )
            else:
                key = (row["income_type"], row["evidence"]["trade_id"])
                if key not in expected:
                    raise ValueError("UNATTRIBUTED_CASH")
                actual[key] = actual.get(key, Decimal(0)) + value
        if {k: v for k, v in actual.items() if v} != {
            k: v for k, v in expected.items() if v
        }:
            raise ValueError("INCOME_FILL_MISMATCH")
        gross = sum(
            (v for (kind, _), v in expected.items() if kind == "REALIZED_PNL"),
            Decimal(0),
        )
        fees = -sum(
            (v for (kind, _), v in expected.items() if kind == "COMMISSION"), Decimal(0)
        )
        if fees != Decimal(report["fees"]):
            raise ValueError("VENUE_LEDGER_ECONOMICS_MISMATCH")
        # Only after exact per-trade income/fee matching may the narrowly
        # witnessed venue-reported cash boundary be considered. This remains a
        # candidate: final fresh inventory and wallet equality are mandatory
        # in DirectionalSettlementStage before any correction is committed.
        precision = rounding_proof(
            trace,
            report,
            entry_observations=entry_observations,
            allow_reported_boundary_cash=True,
        )
        if gross != Decimal(report["gross_pnl"]) + amount(precision["amount"]):
            raise ValueError("VENUE_LEDGER_ECONOMICS_MISMATCH")
        funding_total = sum((Decimal(f["amount"]) for f in funding), Decimal(0))
        insurance_total = sum((Decimal(f["amount"]) for f in insurance), Decimal(0))
        return {
            "status": "CASH_MATCHED",
            "episode": trace["intent_id"],
            "ledger_revision": report["accounting_revision"],
            "gross_pnl": str(gross),
            "fees": str(fees),
            "funding": str(funding_total),
            "provisional_net_pnl": str(gross - fees + funding_total + insurance_total),
            "funding_candidates": funding,
            "insurance_clear": str(insurance_total),
            "insurance_candidates": insurance,
            "precision_reconciliation": precision,
            "settlement_authorized": False,
            "trade_ids": sorted(trades),
            "income_digest": digest(canonical({"rows": rows})),
        }


class DirectionalCashAudit:
    def __init__(
        self, connect, request, *, scope, clock_ms, excluded_position_symbols=()
    ):
        if (scope.exchange, scope.environment, scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ) or (
            getattr(request, "account_id", None),
            getattr(request, "environment", None),
        ) != (scope.account_id, scope.environment):
            raise ValueError("bound Testnet cash audit required")

        def read(method, path, params):
            if method != "GET" or path != "/fapi/v1/income":
                raise ValueError("CASH_AUDIT_READ_ONLY")
            return request(method, path, params)

        self.connect, self.scope, self.clock = connect, scope, clock_ms
        if (
            not isinstance(excluded_position_symbols, tuple)
            or len(set(excluded_position_symbols)) != len(excluded_position_symbols)
            or any(
                not isinstance(item, str) or not item.endswith("USDT")
                for item in excluded_position_symbols
            )
        ):
            raise ValueError("explicit external position exclusions required")
        self.excluded_position_symbols = excluded_position_symbols
        self.importer = BinanceIncomeImporter(
            connect,
            read,
            account_id=scope.account_id,
            environment=scope.environment,
            clock_ms=clock_ms,
        )

    def audit(self, episode):
        episode = str(UUID(episode))
        trace = TradingData(self.connect).trace(episode)
        if trace is None or any(
            trace["scope"].get(k) != v for k, v in asdict(self.scope).items()
        ):
            raise ValueError("CASH_SCOPE_MISMATCH")
        with session_advisory_lock(
            self.connect, "testnet-protocol-account:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BUSY", "settlement_authorized": False}
            # A crash during the new import must not leave a stale positive head.
            self._pending(episode, {"status": "AUDITING"})
            try:
                result = self._audit(episode)
            except Exception as exc:
                self._pending(
                    episode, {"status": "BLOCKED", "error_code": diagnostic_code(exc)}
                )
                raise
            if result["status"] != "CASH_MATCHED":
                self._pending(episode, result)
            return result

    def _pending(self, episode, result):
        key = StateKey(
            **asdict(self.scope), namespace="directional-cash-audit-v1", key=episode
        )
        store = BusinessState(self.connect)
        previous = store.read(key)
        result = store.change(
            key,
            expected_version=previous.version if previous else 0,
            request_key=str(uuid4()),
            payload={**result, "episode": episode, "settlement_authorized": False},
            reason="DIRECTIONAL_CASH_NOT_VERIFIED",
        )
        if result.code != "APPLIED":
            raise ValueError("CASH_AUDIT_CONFLICT")

    def run_once(self, *, limit=10):
        """Pipeline settlement port: closed-but-unsettled cash always blocks entry.

        CLEAR means no closed episode awaiting this stage, never settlement proof.
        A cash match still requires the future final account reconciler.
        """
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("bounded cash batch required")
        with session_advisory_lock(
            self.connect, "directional-cash-stage:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BLOCKED", "reason": "BUSY"}
            store = BusinessState(self.connect)
            key = StateKey(
                **asdict(self.scope),
                namespace="directional-cash-stage-v1",
                key="cursor",
            )
            cursor = store.read(key)
            after = json.loads(cursor.payload_json)["after"] if cursor else ""
            with self.connect() as conn:
                rows = conn.execute(
                    """SELECT i.intent_id::text FROM v2_trade_intents i
                    JOIN v2_episodes e ON e.episode_id=i.intent_id
                    JOIN v2_orders o ON o.episode_id=i.intent_id JOIN v2_fills f USING(order_id)
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1'
                    AND e.status<>'SETTLED' GROUP BY i.intent_id
                    HAVING sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)=0
                    AND sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE 0 END)>0
                    ORDER BY (i.intent_id::text<=%s),i.intent_id LIMIT %s""",
                    (*asdict(self.scope).values(), after, limit),
                ).fetchall()
            audits = {}
            for (episode,) in rows:
                try:
                    audits[episode] = self.audit(episode)
                except Exception as exc:  # noqa: BLE001 - isolate bad episodes, no signed details
                    audits[episode] = {
                        "status": "BLOCKED",
                        "error_code": diagnostic_code(exc),
                    }
                version = cursor.version if cursor else 0
                saved = store.change(
                    key,
                    expected_version=version,
                    request_key=f"scan:{version + 1}",
                    payload={"after": episode},
                    reason="CASH_STAGE_SCAN",
                )
                if saved.code != "APPLIED":
                    raise ValueError("CASH_STAGE_CURSOR_CONFLICT")
                cursor = store.read(key)
            return {
                "status": "BLOCKED" if rows else "CLEAR",
                "audits": audits,
                "reason": "ACCOUNT_SETTLEMENT_REQUIRED"
                if rows
                else "NO_CLOSED_PENDING_EPISODES",
                "settlement_authorized": False,
            }

    def _audit(self, episode):
        episode = str(UUID(episode))
        data = TradingData(self.connect)
        trace = data.trace(episode)
        if trace is None or any(
            trace["scope"].get(k) != v for k, v in asdict(self.scope).items()
        ):
            raise ValueError("CASH_SCOPE_MISMATCH")
        if trace["configuration"].get("mode") != "TESTNET_ADMISSION_ONLY":
            raise ValueError("DIRECTIONAL_EVIDENCE_REQUIRED")
        if not trace["fills"] or any(
            o["status"] not in {"FILLED", "CANCELLED", "REJECTED", "RECONCILED"}
            for o in trace["orders"]
        ):
            return {"status": "WAITING_CLOSE", "settlement_authorized": False}
        report = data.ledger.report(episode, settlement_currency="USDT")
        if amount(report["opened_quantity"]) != amount(report["closed_quantity"]):
            return {"status": "WAITING_CLOSE", "settlement_authorized": False}
        start = min(f["occurred_at_ms"] for f in trace["fills"])
        end = max(f["occurred_at_ms"] for f in trace["fills"])
        if self.clock() < end + 60000:
            return {"status": "WAITING_INCOME_LAG", "settlement_authorized": False}
        # Binance income timestamps may be truncated to the containing second
        # while userTrades retains milliseconds for the same trade identity.
        income_start, income_end = max(0, start - 999), end + 999
        run = self.importer.import_window(start_ms=income_start, end_ms=income_end)
        if run["status"] != "FETCHED":
            return {
                "status": "INCOME_INCOMPLETE",
                "run": run,
                "settlement_authorized": False,
            }
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute(
                "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (episode,),
            )
            current = data.trace(episode, connection=conn)
            if current != trace:
                raise ValueError("CASH_FACTS_CHANGED")
            rows = conn.execute(
                """SELECT income_id::text,income_type,source_id,symbol,amount::text,currency,occurred_at_ms,evidence
                FROM v2_exchange_income WHERE (exchange,account_id,environment,product)=(%s,%s,%s,%s)
                AND occurred_at_ms BETWEEN %s AND %s ORDER BY income_type,source_id LIMIT 20001""",
                (*asdict(self.scope).values(), income_start, income_end),
            ).fetchall()
            rows = [
                dict(
                    zip(
                        (
                            "income_id",
                            "income_type",
                            "source_id",
                            "symbol",
                            "amount",
                            "currency",
                            "occurred_at_ms",
                            "evidence",
                        ),
                        r,
                        strict=True,
                    )
                )
                for r in rows
            ]
            if len(rows) != run["rows"]:
                raise ValueError("INCOME_WINDOW_CHANGED")
            managed = (
                PolicyStore(lambda: nullcontext(conn), self.scope)
                .read()
                .values["capital.enabled"]
            )
            # Same-symbol overlapping exposure still cannot own funding uniquely.
            overlap = conn.execute(
                """SELECT 1 FROM v2_trade_intents i JOIN v2_orders o ON o.episode_id=i.intent_id
                JOIN v2_fills f USING(order_id) WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND i.intent_id<>%s AND (NOT %s OR i.payload->>'symbol'=%s) GROUP BY i.intent_id
                HAVING min(f.occurred_at_ms)<=%s AND (max(f.occurred_at_ms)>=%s OR
                sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)<>0) LIMIT 1""",
                (
                    *asdict(self.scope).values(),
                    episode,
                    managed,
                    trace["request"]["symbol"],
                    end,
                    start,
                ),
            ).fetchone()
            if overlap:
                raise ValueError("OVERLAPPING_ACCOUNT_EPISODE")
            target_rows = [
                row for row in rows if row["symbol"] == trace["request"]["symbol"]
            ]
            external_rows = [
                row for row in rows if row["symbol"] != trace["request"]["symbol"]
            ]
            for row in external_rows:
                if managed and row["symbol"] not in self.excluded_position_symbols:
                    if income_owner(conn, self.scope, row) == episode:
                        raise ValueError("INCONSISTENT_CASH_OWNER")
                    continue
                if (
                    row["symbol"] not in self.excluded_position_symbols
                    or row["income_type"] != "FUNDING_FEE"
                    or row["currency"] != "USDT"
                    or row["evidence"] != {"source": "binance-income", "trade_id": ""}
                ):
                    raise ValueError("UNATTRIBUTED_CASH")
            report = data.ledger.report(
                episode, settlement_currency="USDT", connection=conn
            )
            try:
                proof = reconcile_cash(current, report, target_rows, scope=self.scope)
            except ValueError as exc:
                if str(exc) != "UNEXPLAINED_FILL_PNL_PRECISION":
                    raise
                proof = reconcile_cash(
                    current,
                    report,
                    target_rows,
                    scope=self.scope,
                    entry_observations=_entry_observations(conn, self.scope, current),
                )
            proof.update(
                excluded_income_ids=[row["income_id"] for row in external_rows],
                income_run=run["run_id"],
                observed_at_ms=self.clock(),
                source="directional-cash-audit-v1",
            )
            key = StateKey(
                **asdict(self.scope), namespace="directional-cash-audit-v1", key=episode
            )
            store = BusinessState(lambda: nullcontext(conn))
            previous = store.read(key)
            version = previous.version if previous else 0
            result = store.change(
                key,
                expected_version=version,
                request_key=run["run_id"],
                payload=proof,
                reason="DIRECTIONAL_CASH_AUDIT",
            )
            if result.code != "APPLIED":
                raise ValueError("CASH_AUDIT_CONFLICT")
        return proof
