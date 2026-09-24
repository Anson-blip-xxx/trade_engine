"""Final Testnet settlement from opening baseline, flat inventory and cash proof."""

import json
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal
from uuid import UUID, uuid4

from services.v2_directional_cash import DirectionalCashAudit
from v2_core.account_coverage import AccountCoverageAudit, coverage_from_facts
from v2_core.account_inventory import persist_inventory
from v2_core.directional_outcomes import DirectionalOutcomeJournal
from v2_core.evidence import canonical, digest
from v2_core.income import IncomeJournal
from v2_core.ledger import Ledger, amount
from v2_core.state import BusinessState, StateKey


class DirectionalSettlementStage:
    def __init__(
        self,
        connect,
        request,
        *,
        scope,
        clock_ms,
        limit=10,
        excluded_position_symbols=(),
    ):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("bounded settlement batch required")
        self.connect, self.scope, self.clock, self.limit = (
            connect,
            scope,
            clock_ms,
            limit,
        )
        self.cash = DirectionalCashAudit(
            connect,
            request,
            scope=scope,
            clock_ms=clock_ms,
            excluded_position_symbols=excluded_position_symbols,
        )
        self.coverage = AccountCoverageAudit(
            connect,
            request,
            scope=scope,
            clock_ms=clock_ms,
            excluded_position_symbols=excluded_position_symbols,
        )
        self.outcomes = DirectionalOutcomeJournal(connect, scope=scope)

    def _state(self, namespace, key):
        value = BusinessState(self.connect).read(
            StateKey(**asdict(self.scope), namespace=namespace, key=key)
        )
        if value is None or value.deleted:
            raise ValueError("REQUIRED_SETTLEMENT_EVIDENCE_MISSING")
        return json.loads(value.payload_json), value.version

    def _baseline(self, episode):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT o.order_id::text FROM v2_orders o JOIN v2_trade_intents i
                ON i.intent_id=o.episode_id WHERE i.intent_id=%s
                AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND o.leg='OPEN' ORDER BY o.order_id LIMIT 2""",
                (episode, *asdict(self.scope).values()),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("SINGLE_OPENING_BASELINE_REQUIRED")
        proof, version = self._state("opening-readiness-v1", rows[0][0])
        inventory = proof.get("inventory")
        if (
            proof.get("status") != "READY_FOR_GUARDED_SUBMISSION"
            or proof.get("blockers")
            or not isinstance(inventory, dict)
            or inventory.get("account_scope") != asdict(self.scope)
            or inventory.get("blockers")
            or inventory.get("failures")
        ):
            raise ValueError("VALID_OPENING_BASELINE_REQUIRED")
        return rows[0][0], proof, inventory, version

    def settle(self, episode):
        episode = str(UUID(episode))
        cash = self.cash.audit(episode)
        if cash.get("status") != "CASH_MATCHED":
            return {
                "status": "BLOCKED",
                "reason": cash.get("status"),
                "settlement_authorized": False,
            }
        with self.connect() as account:
            if not account.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + self.scope.key,),
            ).fetchone()[0]:
                return {
                    "status": "BLOCKED",
                    "reason": "ACCOUNT_BUSY",
                    "settlement_authorized": False,
                }
            account.commit()
            result = self._settle_locked(episode, cash)
        if result.get("status") == "SETTLED":
            result["outcome"] = self.outcomes.record(episode)
        return result

    def _external_cash(
        self, *, symbol, baseline_ms, final_ms, fill_start, fill_end, cash
    ):
        # Re-fetch the entire baseline-to-final wallet interval. A narrow
        # episode import cannot justify subtracting unrelated account cash.
        run = self.cash.importer.import_window(
            start_ms=max(0, baseline_ms - 999), end_ms=final_ms
        )
        if run["status"] != "FETCHED":
            raise ValueError("FINAL_INCOME_INCOMPLETE")
        with self.connect() as conn:
            values = conn.execute(
                """SELECT income_id::text,income_type,source_id,symbol,amount::text,
                currency,occurred_at_ms,evidence FROM v2_exchange_income
                WHERE (exchange,account_id,environment,product)=(%s,%s,%s,%s)
                AND occurred_at_ms BETWEEN %s AND %s
                ORDER BY income_type,source_id LIMIT 20001""",
                (*asdict(self.scope).values(), max(0, baseline_ms - 999), final_ms),
            ).fetchall()
        fields = (
            "income_id",
            "income_type",
            "source_id",
            "symbol",
            "amount",
            "currency",
            "occurred_at_ms",
            "evidence",
        )
        rows = [dict(zip(fields, value, strict=True)) for value in values]
        if len(rows) != run["rows"]:
            raise ValueError("FINAL_INCOME_WINDOW_CHANGED")
        target, external = [], []
        for row in rows:
            if row["symbol"] == symbol:
                if not fill_start - 999 <= row["occurred_at_ms"] <= fill_end + 999:
                    raise ValueError("UNATTRIBUTED_TARGET_CASH")
                target.append(row)
            elif (
                row["symbol"] in self.coverage.excluded_position_symbols
                and row["income_type"] == "FUNDING_FEE"
                and row["currency"] == "USDT"
                and row["evidence"] == {"source": "binance-income", "trade_id": ""}
                and row["occurred_at_ms"] > baseline_ms
            ):
                external.append(row)
            else:
                raise ValueError("UNATTRIBUTED_ACCOUNT_CASH")
        if digest(canonical({"rows": target})) != cash["income_digest"]:
            raise ValueError("CASH_OBSERVATION_SUPERSEDED")
        episode_external_ids = sorted(
            row["income_id"]
            for row in external
            if fill_start - 999 <= row["occurred_at_ms"] <= fill_end + 999
        )
        if episode_external_ids != sorted(cash["excluded_income_ids"]):
            raise ValueError("EXTERNAL_CASH_OBSERVATION_SUPERSEDED")
        return external, run["run_id"]

    def _settle_locked(self, episode, cash):
        baseline_order, baseline_proof, baseline, baseline_version = self._baseline(
            episode
        )
        before = self.coverage.facts()
        final = self.coverage.inventory.collect(str(uuid4()))
        after = self.coverage.facts()
        persist_inventory(self.connect, scope=self.scope, observation=final)
        blockers = coverage_from_facts(
            self.scope,
            after,
            final,
            excluded_position_symbols=self.coverage.excluded_position_symbols,
        )
        if before != after:
            blockers = sorted(set(blockers) | {"LOCAL_FACTS_CHANGED_DURING_INVENTORY"})
        if (
            blockers
            or final["failures"]
            or final["finished_at_ms"] < cash["observed_at_ms"]
        ):
            raise ValueError("FINAL_ACCOUNT_NOT_RECONCILED")
        with self.connect() as conn:
            window = conn.execute(
                """SELECT min(f.occurred_at_ms),max(f.occurred_at_ms),max(i.payload->>'symbol')
                FROM v2_fills f JOIN v2_orders o USING(order_id)
                JOIN v2_trade_intents i ON i.intent_id=o.episode_id WHERE o.episode_id=%s""",
                (episode,),
            ).fetchone()
        if (
            window[0] is None
            or window[2] is None
            or baseline["finished_at_ms"] > window[0]
            or final["started_at_ms"] < window[1]
        ):
            raise ValueError("INVALID_SETTLEMENT_OBSERVATION_ORDER")
        start_wallet = amount(baseline["responses"]["account"]["totalWalletBalance"])
        final_wallet = amount(final["responses"]["account"]["totalWalletBalance"])
        provisional = Decimal(cash["provisional_net_pnl"])
        external, income_run = self._external_cash(
            symbol=window[2],
            baseline_ms=baseline["finished_at_ms"],
            final_ms=final["finished_at_ms"],
            fill_start=window[0],
            fill_end=window[1],
            cash=cash,
        )
        external_amount = sum((Decimal(row["amount"]) for row in external), Decimal(0))
        attributed_wallet_delta = final_wallet - start_wallet - external_amount
        if attributed_wallet_delta != provisional:
            raise ValueError("WALLET_CASH_MISMATCH")
        cash_saved, cash_version = self._state("directional-cash-audit-v1", episode)
        if cash_saved != cash:
            raise ValueError("CASH_OBSERVATION_SUPERSEDED")
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            conn.execute(
                "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (episode,),
            )
            state = BusinessState(lambda: nullcontext(conn))
            baseline_current = state.read(
                StateKey(
                    **asdict(self.scope),
                    namespace="opening-readiness-v1",
                    key=baseline_order,
                )
            )
            final_current = state.read(
                StateKey(
                    **asdict(self.scope),
                    namespace="account-inventory-v1",
                    key=final["observation_id"],
                )
            )
            if (
                baseline_current is None
                or baseline_current.version != baseline_version
                or json.loads(baseline_current.payload_json) != baseline_proof
                or final_current is None
                or json.loads(final_current.payload_json) != final
            ):
                raise ValueError("ACCOUNT_OBSERVATION_SUPERSEDED")
            current = state.read(
                StateKey(
                    **asdict(self.scope),
                    namespace="directional-cash-audit-v1",
                    key=episode,
                )
            )
            if (
                current is None
                or current.version != cash_version
                or json.loads(current.payload_json) != cash
            ):
                raise ValueError("CASH_OBSERVATION_SUPERSEDED")
            if self.coverage.facts(connection=conn) != after:
                raise ValueError("SETTLEMENT_FACTS_CHANGED")
            journal = IncomeJournal(lambda: nullcontext(conn))
            revision = cash["ledger_revision"]
            for funding in cash["funding_candidates"]:
                journal.assign_funding(
                    funding["income_id"],
                    episode,
                    expected_revision=revision,
                    evidence={
                        "source": "directional-settlement-v1",
                        "fills_complete": True,
                        "cash_audit_digest": digest(canonical(cash)),
                        "baseline_digest": digest(canonical(baseline_proof)),
                        "final_inventory": final["observation_id"],
                    },
                    connection=conn,
                )
                revision += 1
            ledger = Ledger(lambda: nullcontext(conn))
            report = ledger.report(episode, settlement_currency="USDT", connection=conn)
            if (
                report["accounting_revision"] != revision
                or report["accounting_status"] != "CALCULATED"
                or Decimal(report["net_pnl"]) != provisional
            ):
                raise ValueError("FINAL_LEDGER_CASH_MISMATCH")
            proof = {
                "source": "directional-settlement-v1",
                "observed_at_ms": final["finished_at_ms"],
                "ledger_revision": revision,
                "exchange_flat": True,
                "orders_terminal": True,
                "fills_complete": True,
                "cash_complete": True,
                "account_scope": asdict(self.scope),
                "baseline_inventory": baseline["observation_id"],
                "baseline_digest": digest(canonical(baseline_proof)),
                "final_inventory": final["observation_id"],
                "final_inventory_digest": digest(canonical(final)),
                "cash_audit_digest": digest(canonical(cash)),
                "wallet_delta": str(final_wallet - start_wallet),
                "attributed_wallet_delta": str(attributed_wallet_delta),
                "external_income_run": income_run,
                "external_income": [
                    {
                        "income_id": row["income_id"],
                        "symbol": row["symbol"],
                        "amount": row["amount"],
                    }
                    for row in external
                ],
                "net_pnl": report["net_pnl"],
                "funding_income_ids": [
                    f["income_id"] for f in cash["funding_candidates"]
                ],
            }
            ledger.settle(episode, currency="USDT", evidence=proof)
        return {
            "status": "SETTLED",
            "episode": episode,
            "net_pnl": report["net_pnl"],
            "final_inventory": final["observation_id"],
            "settlement_authorized": False,
        }

    def run_once(self):
        with self.connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("directional-settlement-stage:" + self.scope.key,),
            ).fetchone()[0]:
                return {"status": "BLOCKED", "reason": "BUSY"}
            guard.commit()
            with self.connect() as conn:
                pending = conn.execute(
                    """SELECT i.intent_id::text FROM v2_trade_intents i
                    JOIN v2_episodes e ON e.episode_id=i.intent_id
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND i.producer IN ('s6','s8')
                    AND i.strategy_version='directional-admission-v2-1'
                    AND e.status='SETTLED' AND NOT EXISTS (
                        SELECT 1 FROM v2_directional_outcomes d WHERE d.episode_id=i.intent_id)
                    ORDER BY i.intent_id LIMIT %s""",
                    (*asdict(self.scope).values(), self.limit),
                ).fetchall()
            outcomes = {}
            for (episode,) in pending:
                try:
                    outcomes[episode] = self.outcomes.record(episode)
                except Exception as exc:  # noqa: BLE001 - isolate corrupt analysis evidence
                    outcomes[episode] = {
                        "status": "BLOCKED",
                        "error_code": type(exc).__name__,
                    }
            with self.connect() as conn:
                rows = conn.execute(
                    """SELECT i.intent_id::text FROM v2_trade_intents i JOIN v2_episodes e
                    ON e.episode_id=i.intent_id JOIN v2_orders o ON o.episode_id=i.intent_id
                    JOIN v2_fills f USING(order_id)
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND i.producer IN ('s6','s8') AND i.strategy_version='directional-admission-v2-1'
                    AND e.status<>'SETTLED' GROUP BY i.intent_id
                    HAVING sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)=0
                    AND sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE 0 END)>0
                    ORDER BY i.intent_id LIMIT %s""",
                    (*asdict(self.scope).values(), self.limit),
                ).fetchall()
            results = {}
            for (episode,) in rows:
                try:
                    results[episode] = self.settle(episode)
                except Exception as exc:  # noqa: BLE001 - isolate and sanitize one episode
                    results[episode] = {
                        "status": "BLOCKED",
                        "error_code": type(exc).__name__,
                    }
            unresolved = any(
                r.get("status") != "SETTLED" for r in results.values()
            ) or any(r.get("status") == "BLOCKED" for r in outcomes.values())
            return {
                "status": "BLOCKED" if unresolved else "CLEAR",
                "settlements": results,
                "outcomes": outcomes,
                "settlement_authorized": False,
            }
