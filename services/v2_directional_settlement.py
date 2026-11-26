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
from v2_core.income_ownership import income_owner
from v2_core.ledger import Ledger, amount
from v2_core.precision import money_context
from v2_core.runtime_policy import PolicyStore
from v2_core.scheduling import diagnostic_code
from v2_core.session_lock import session_advisory_lock
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_lifecycle import (
    LIFECYCLE_EPISODE_SQL,
    cash_namespace,
    plan_provenance,
    settlement_source,
)
from v2_core.strategy_outcomes import StrategyOutcomeJournal
from v2_core.strategy_plan_store import read_trade_plan


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
        self.strategy_outcomes = StrategyOutcomeJournal(connect, scope=scope)
        # Scheduling hint only: no financial fact or completed-work proof lives
        # here. Restart safely rescans from the beginning. A poison analysis
        # record must not monopolize every bounded batch or require a cursor DB
        # write whose failure could interfere with actual financial settlement.
        self._outcome_after = ""
        self._settlement_after = ""

    def _plan(self, episode):
        with self.connect() as conn:
            plan = read_trade_plan(conn, episode)
        if plan is not None and plan.scope != self.scope:
            raise ValueError("CASH_SCOPE_MISMATCH")
        return plan

    def _record_outcome(self, episode):
        journal = (
            self.strategy_outcomes if self._plan(episode) is not None else self.outcomes
        )
        return journal.record(episode)

    def _state(self, namespace, key):
        value = BusinessState(self.connect).read(
            StateKey(**asdict(self.scope), namespace=namespace, key=key)
        )
        if value is None or value.deleted:
            raise ValueError("REQUIRED_SETTLEMENT_EVIDENCE_MISSING")
        return json.loads(value.payload_json), value.version

    def entry_clearance(self, result, protection_coverage):
        from services.v2_settlement_entry import entry_clearance

        return entry_clearance(self, result, protection_coverage)

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
        inventory_blockers = (
            set(inventory.get("blockers", ())) if isinstance(inventory, dict) else set()
        )
        if proof.get("capital_budget", {}).get("account_scope") == asdict(self.scope):
            inventory_blockers -= {
                "EXISTING_POSITION_REQUIRES_RECOVERY",
                "EXISTING_ORDINARY_ORDERS",
                "EXISTING_CONDITIONAL_ORDERS",
            }
        if (
            proof.get("status") != "READY_FOR_GUARDED_SUBMISSION"
            or proof.get("blockers")
            or not isinstance(inventory, dict)
            or inventory.get("account_scope") != asdict(self.scope)
            or inventory_blockers
            or inventory.get("failures")
        ):
            raise ValueError("VALID_OPENING_BASELINE_REQUIRED")
        return rows[0][0], proof, inventory, version

    def settle(self, episode):
        episode = str(UUID(episode))
        # A known local blocker already makes final account settlement
        # impossible. Do not repeatedly spend income + seven-route inventory
        # quota on it. This is a denial-only prefilter, not a cached clearance:
        # once it clears, all existing fresh cash/inventory/fact fences still run.
        with self.connect() as conn:
            pending = conn.execute(
                """SELECT 1 FROM v2_orders o JOIN v2_trade_intents i
                ON i.intent_id=o.episode_id
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED')
                AND NOT (o.status='PREPARED' AND o.leg='OPEN'
                    AND o.exchange_order_id IS NULL AND NOT EXISTS
                    (SELECT 1 FROM v2_fills f WHERE f.order_id=o.order_id))
                LIMIT 1""",
                tuple(asdict(self.scope).values()),
            ).fetchone()
        if pending:
            return {
                "status": "BLOCKED",
                "reason": "LOCAL_ORDERS_NOT_FINAL",
                "network_deferred": True,
                "settlement_authorized": False,
            }
        cash = self.cash.audit(episode)
        if cash.get("status") != "CASH_MATCHED":
            return {
                "status": "BLOCKED",
                "reason": cash.get("status"),
                "settlement_authorized": False,
            }
        with session_advisory_lock(
            self.connect, "testnet-protocol-account:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {
                    "status": "BLOCKED",
                    "reason": "ACCOUNT_BUSY",
                    "settlement_authorized": False,
                }
            result = self._settle_locked(episode, cash)
        if result.get("status") == "SETTLED":
            # Settlement and risk release have already committed. A derived
            # analysis failure cannot turn that financial fact back into a
            # blocked settlement; run_once retries missing outcomes separately.
            try:
                result["outcome"] = self._record_outcome(episode)
            except Exception as exc:  # noqa: BLE001 - retain safe analysis diagnostics
                result["outcome"] = {
                    "status": "BLOCKED",
                    "error_code": diagnostic_code(exc),
                }
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
        managed = PolicyStore(self.connect, self.scope).read().values["capital.enabled"]
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
            elif (
                managed
                and row["symbol"] != symbol
                and row["occurred_at_ms"] > baseline_ms
            ):
                with self.connect() as conn:
                    income_owner(conn, self.scope, row)
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
        plan = self._plan(episode)
        venue_basis = cash.get("cash_basis_version", 1) == 2
        if plan is not None and (
            not venue_basis
            or cash.get("source") != cash_namespace(plan)
            or cash.get("strategy_plan") != plan_provenance(plan)
        ):
            raise ValueError("STRATEGY_CASH_PROVENANCE_MISMATCH")
        if venue_basis and cash["venue_cash_analysis"]["anomaly"]["status"] not in {
            "EXACT",
            "WITHIN_POLICY",
        }:
            # Material/unexplained economics still require review. Small venue
            # reporting differences no longer require a guessed rounding model.
            raise ValueError("VENUE_CASH_REVIEW_REQUIRED")
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
        with money_context():
            external_amount = sum(
                (Decimal(row["amount"]) for row in external), Decimal(0)
            )
            wallet_delta = final_wallet - start_wallet
            attributed_wallet_delta = wallet_delta - external_amount
        if attributed_wallet_delta != provisional:
            raise ValueError("WALLET_CASH_MISMATCH")
        cash_saved, cash_version = self._state(cash_namespace(plan), episode)
        if cash_saved != cash:
            raise ValueError("CASH_OBSERVATION_SUPERSEDED")
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            conn.execute(
                "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (episode,),
            )
            if read_trade_plan(conn, episode) != plan:
                raise ValueError("STRATEGY_LIFECYCLE_PLAN_MISMATCH")
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
                    namespace=cash_namespace(plan),
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
            if venue_basis:
                policy = PolicyStore(lambda: nullcontext(conn), self.scope).read()
                captured = cash["cash_policy_snapshot"]
                if (
                    policy.version != captured["version"]
                    or policy.digest != captured["digest"]
                ):
                    raise ValueError("SETTLEMENT_POLICY_SUPERSEDED")
            journal = IncomeJournal(lambda: nullcontext(conn))
            revision = cash["ledger_revision"]
            for funding in cash["funding_candidates"]:
                journal.assign_funding(
                    funding["income_id"],
                    episode,
                    expected_revision=revision,
                    evidence={
                        "source": settlement_source(plan),
                        **(
                            {"strategy_plan": plan_provenance(plan)}
                            if plan is not None
                            else {}
                        ),
                        "fills_complete": True,
                        "cash_audit_digest": digest(canonical(cash)),
                        "baseline_digest": digest(canonical(baseline_proof)),
                        "final_inventory": final["observation_id"],
                    },
                    connection=conn,
                )
                revision += 1
            for insurance in cash.get("insurance_candidates", []):
                inserted = journal.assign_insurance_clear(
                    insurance["income_id"],
                    episode,
                    expected_revision=revision,
                    evidence={
                        "source": settlement_source(plan),
                        **(
                            {"strategy_plan": plan_provenance(plan)}
                            if plan is not None
                            else {}
                        ),
                        "fills_complete": True,
                        "cash_audit_digest": digest(canonical(cash)),
                        "baseline_digest": digest(canonical(baseline_proof)),
                        "final_inventory": final["observation_id"],
                    },
                    connection=conn,
                )
                revision += int(inserted)
            ledger = Ledger(lambda: nullcontext(conn))
            precision = cash.get("precision_reconciliation")
            if not venue_basis and amount(precision["amount"]) != 0:
                inserted = ledger.adjustment(
                    episode_id=episode,
                    source_id=f"{precision['model']}:{episode}",
                    amount_text=precision["amount"],
                    currency="USDT",
                    kind="CORRECTION",
                    occurred_at_ms=window[1],
                    evidence={
                        "source": "directional-settlement-precision-v1",
                        "precision": precision,
                        "cash_audit_digest": digest(canonical(cash)),
                        "income_digest": cash["income_digest"],
                        "final_inventory": final["observation_id"],
                    },
                )
                revision += int(inserted)
            report = ledger.report(episode, settlement_currency="USDT", connection=conn)
            cash_basis = None
            if venue_basis:
                cash_basis = ledger.cash_basis_candidate(
                    episode,
                    report,
                    analysis=cash["venue_cash_analysis"],
                    income_digest=cash["income_digest"],
                    connection=conn,
                )
                net_pnl = cash_basis["net_pnl"]
            else:
                net_pnl = report["net_pnl"]
            if (
                report["accounting_revision"] != revision
                or report["accounting_status"] != "CALCULATED"
                or Decimal(net_pnl) != provisional
            ):
                raise ValueError("FINAL_LEDGER_CASH_MISMATCH")
            proof = {
                "source": settlement_source(plan),
                **(
                    {"strategy_plan": plan_provenance(plan)} if plan is not None else {}
                ),
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
                "wallet_delta": str(wallet_delta),
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
                "net_pnl": net_pnl,
                "funding_income_ids": [
                    f["income_id"] for f in cash["funding_candidates"]
                ],
                "insurance_clear_income_ids": [
                    f["income_id"] for f in cash.get("insurance_candidates", [])
                ],
            }
            if venue_basis:
                proof.update(
                    cash_basis=cash_basis,
                    income_digest=cash["income_digest"],
                    cash_policy_snapshot=cash["cash_policy_snapshot"],
                )
            else:
                proof["precision_reconciliation"] = precision
            ledger.settle(episode, currency="USDT", evidence=proof)
        return {
            "status": "SETTLED",
            "episode": episode,
            "net_pnl": net_pnl,
            "final_inventory": final["observation_id"],
            "settlement_authorized": False,
        }

    def _pending_episodes(self, *, after, limit):
        """Select current financial obligations; the cursor only rotates work."""
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT i.intent_id::text FROM v2_trade_intents i JOIN v2_episodes e
                ON e.episode_id=i.intent_id JOIN v2_orders o ON o.episode_id=i.intent_id
                JOIN v2_fills f USING(order_id)
                WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                AND {LIFECYCLE_EPISODE_SQL}
                AND e.status<>'SETTLED' GROUP BY i.intent_id
                HAVING sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)=0
                AND sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE 0 END)>0
                ORDER BY (i.intent_id::text<=%s),i.intent_id LIMIT %s""",
                (*asdict(self.scope).values(), after, limit),
            ).fetchall()

    def run_once(self):
        with session_advisory_lock(
            self.connect, "directional-settlement-stage:" + self.scope.key
        ) as acquired:
            if not acquired:
                return {"status": "BLOCKED", "reason": "BUSY"}
            with self.connect() as conn:
                pending = conn.execute(
                    f"""SELECT i.intent_id::text FROM v2_trade_intents i
                    JOIN v2_episodes e ON e.episode_id=i.intent_id
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                    AND {LIFECYCLE_EPISODE_SQL}
                    AND e.status='SETTLED' AND CASE WHEN EXISTS (
                        SELECT 1 FROM v2_decision_evidence de WHERE de.evidence_ref=i.evidence_ref
                        AND de.snapshot ? 'strategy_plan') THEN NOT EXISTS (
                          SELECT 1 FROM v2_business_state outcome
                          JOIN v2_state_history outcome_original
                            ON outcome_original.state_id=outcome.state_id
                            AND outcome_original.version=1
                          JOIN v2_decision_evidence outcome_decision
                            ON outcome_decision.evidence_ref=i.evidence_ref
                          JOIN v2_settlements outcome_settlement
                            ON outcome_settlement.episode_id=i.intent_id
                            AND outcome_settlement.revision=e.accounting_revision
                          WHERE outcome.scope->>'namespace'='strategy-outcome-v1'
                          AND outcome.scope->>'key'=i.intent_id::text
                          AND (outcome.scope->>'exchange',outcome.scope->>'account_id',
                               outcome.scope->>'environment',outcome.scope->>'product')
                            =(i.exchange,i.account_id,i.environment,i.product)
                          AND outcome.version=1 AND NOT outcome.deleted
                          AND NOT outcome_original.deleted
                          AND outcome_original.reason='VERIFIED_STRATEGY_OUTCOME'
                          AND outcome_original.payload=outcome.payload
                          AND outcome.payload->>'source'='strategy-outcome-v1'
                          AND outcome.payload->>'outcome_schema'='strategy-outcome-v1'
                          AND outcome.payload->>'episode_id'=i.intent_id::text
                          AND outcome.payload->>'settlement_revision'=e.accounting_revision::text
                          AND outcome_settlement.currency='USDT'
                          AND outcome_settlement.evidence->>'source'='strategy-settlement-v1'
                          AND outcome_settlement.evidence->'strategy_plan'
                            =outcome.payload->'strategy_plan'
                          AND outcome.payload->>'plan_digest' ~ '^[0-9a-f]{{64}}$'
                          AND outcome.payload->'strategy_plan'->>'plan_digest'
                            =outcome.payload->>'plan_digest'
                          AND outcome.payload->'strategy_plan'->>'schema'='strategy-plan-provenance-v1'
                          AND outcome.payload->'strategy_plan'->>'instance_id'=i.producer
                          AND outcome.payload->'strategy_plan'->>'code_version'=i.strategy_version
                          AND outcome.payload->'strategy_plan'->>'config_digest'=i.config_digest
                          AND outcome.payload->'strategy_plan'->'deployment_generation'
                            =outcome_decision.snapshot->'strategy_plan'->'deployment_generation'
                          AND outcome.payload->'strategy_plan'->'strategy_id'
                            =outcome_decision.snapshot->'strategy_plan'->'strategy_id')
                        ELSE NOT EXISTS (SELECT 1 FROM v2_directional_outcomes d
                          WHERE d.episode_id=i.intent_id) END
                    ORDER BY (i.intent_id::text<=%s),i.intent_id LIMIT %s""",
                    (*asdict(self.scope).values(), self._outcome_after, self.limit),
                ).fetchall()
            outcomes = {}
            for (episode,) in pending:
                try:
                    outcomes[episode] = self._record_outcome(episode)
                except Exception as exc:  # noqa: BLE001 - isolate corrupt analysis evidence
                    outcomes[episode] = {
                        "status": "BLOCKED",
                        "error_code": diagnostic_code(exc),
                    }
                finally:
                    self._outcome_after = episode
            rows = self._pending_episodes(
                after=self._settlement_after, limit=self.limit
            )
            results = {}
            for (episode,) in rows:
                try:
                    results[episode] = self.settle(episode)
                    if (
                        results[episode].get("status") == "SETTLED"
                        and "outcome" in results[episode]
                    ):
                        outcomes[episode] = results[episode].pop("outcome")
                except Exception as exc:  # noqa: BLE001 - isolate and sanitize one episode
                    results[episode] = {
                        "status": "BLOCKED",
                        "error_code": diagnostic_code(exc),
                    }
                finally:
                    # Failure/BUSY must not strand every later obligation. This
                    # hint is never financial completion and resets on restart.
                    self._settlement_after = episode
            unresolved = any(r.get("status") != "SETTLED" for r in results.values())
            # A successful rotated page is not account-wide clearance. Recheck
            # the same scoped financial predicate, independently of either hint.
            remaining = not unresolved and bool(
                self._pending_episodes(after="", limit=1)
            )
            return {
                "status": "BLOCKED" if unresolved or remaining else "CLEAR",
                "settlements": results,
                "outcomes": outcomes,
                "settlement_authorized": False,
            }
