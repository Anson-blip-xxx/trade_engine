"""Owned multi-symbol Testnet exposure and fresh aggregate margin admission.

All callers use the account protocol advisory lock through GuardedOpeningSubmit.
Unknown sibling submissions block; pending reservations are counted before POST.
"""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, localcontext

from v2_core.account_coverage import AccountCoverageAudit, coverage_from_facts
from v2_core.capital_model import current_health, guard_risk
from v2_core.ledger import amount
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey


def publish_capital_snapshot(
    connect, scope, *, account, started, deadline, observation_id
):
    store = BusinessState(connect)
    key = StateKey(**asdict(scope), namespace="capital-snapshot-v1", key="latest")
    old = store.read(key)
    if old and json.loads(old.payload_json)["started_at_ms"] > started:
        raise ValueError("CAPITAL_SNAPSHOT_SUPERSEDED")
    profile = PolicyStore(connect, scope).read()
    payload = {
        "capital_health": current_health(
            connect, scope, account, profile.values, now_ms=started
        ),
        "account_scope": asdict(scope),
        "started_at_ms": started,
        "valid_until_ms": deadline,
        "observation_id": observation_id,
        "account": {
            name: str(amount(account[name]))
            for name in (
                "totalInitialMargin",
                "availableBalance",
                "totalWalletBalance",
                "totalMarginBalance",
            )
        },
    }
    result = store.change(
        key,
        expected_version=old.version if old else 0,
        request_key=observation_id,
        payload=payload,
        reason="VERIFIED_CAPITAL_SNAPSHOT",
    )
    if result.code != "APPLIED":
        raise ValueError("CAPITAL_SNAPSHOT_CONFLICT")


def reserve_capital(conn, scope, *, episode, notional, leverage, now):
    """Runs under account-risk transaction lock; concurrent admissions serialize."""
    connect = lambda: nullcontext(conn)
    policy = PolicyStore(connect, scope).read()
    if not policy.values["capital.enabled"] or scope.environment != "SANDBOX":
        raise ValueError("CAPITAL_POLICY_NOT_ACTIVE")
    key = StateKey(**asdict(scope), namespace="capital-snapshot-v1", key="latest")
    state = BusinessState(connect).read(key)
    if state is None or state.deleted:
        raise ValueError("CAPITAL_SNAPSHOT_MISSING")
    snapshot = json.loads(state.payload_json)
    if (
        snapshot["account_scope"] != asdict(scope)
        or not snapshot["started_at_ms"] <= now < snapshot["valid_until_ms"]
    ):
        raise ValueError("CAPITAL_SNAPSHOT_STALE")
    if type(leverage) is not int or leverage not in {1, 2, 3, 4, 5, 8}:
        raise ValueError("CAPITAL_LEVERAGE_UNVERIFIED")
    rows = conn.execute(
        """SELECT r.notional::text,e.snapshot->'features'->'evaluation'->'market_plan'->>'leverage'
        FROM v2_risk_reservations r JOIN v2_trade_intents i ON i.intent_id=r.episode_id
        JOIN v2_decision_evidence e USING(evidence_ref)
        WHERE r.scope=%s AND r.status='HELD' AND r.episode_id<>%s
        AND (r.created_at>=to_timestamp(%s/1000.0) OR EXISTS(
            SELECT 1 FROM v2_orders o JOIN v2_fills f USING(order_id)
            WHERE o.episode_id=r.episode_id AND f.occurred_at_ms>=%s)
        OR NOT EXISTS(
            SELECT 1 FROM v2_orders o JOIN v2_fills f USING(order_id) WHERE o.episode_id=r.episode_id))""",
        (scope.key, episode, snapshot["started_at_ms"], snapshot["started_at_ms"]),
    ).fetchall()
    with localcontext() as context:
        context.prec = 100
        account = snapshot["account"]
        used, available, wallet, equity = (
            amount(account[name])
            for name in (
                "totalInitialMargin",
                "availableBalance",
                "totalWalletBalance",
                "totalMarginBalance",
            )
        )
        if min(used, available, wallet, equity) < 0:
            raise ValueError("INVALID_CAPITAL_ACCOUNT_VALUES")
        buffer = Decimal(policy.values["capital.margin_buffer"])
        pending = sum(
            (
                amount(n, positive=True) / amount(l, positive=True) * buffer
                for n, l in rows
            ),
            Decimal(0),
        )
        capital = current_health(connect, scope, account, policy.values)
        base = Decimal(capital["base"])
        limit = base * Decimal(policy.values["capital.pool_fraction"])
        proposed = notional / leverage * buffer
        if used + pending + proposed > limit or pending + proposed > available:
            raise ValueError("ACCOUNT_MARGIN_BUDGET_EXCEEDED")
        risk = guard_risk(
            conn,
            scope,
            episode=episode,
            notional=notional,
            leverage=leverage,
            settings=policy.values,
            capital=capital,
        )
        return {
            **risk,
            "policy_version": policy.version,
            "policy_digest": policy.digest,
            "snapshot_id": key.identity,
            "snapshot_version": state.version,
            "capital_base": str(base),
            "limit": str(limit),
            "venue_used_margin": str(used),
            "pending_margin": str(pending),
            "reserved_margin": str(proposed),
        }


class ManagedPortfolio:
    def __init__(self, connect, request, *, scope, clock_ms, exclusions=()):
        self.connect, self.scope = connect, scope
        self.policy = PolicyStore(connect, scope)
        self.audit = AccountCoverageAudit(
            connect,
            request,
            scope=scope,
            clock_ms=clock_ms,
            excluded_position_symbols=exclusions,
        )

    def enabled(self):
        return self.policy.read().values["capital.enabled"]

    def facts(self):
        return self.audit.facts()

    def blockers(self, inventory, target, *, current_order=None, before=None):
        facts = self.facts()
        blockers = []
        if before is not None and before != facts:
            blockers.append("LOCAL_FACTS_CHANGED_DURING_INVENTORY")
        if any(
            item["symbol"] == target and amount(item["quantity"]) != 0
            for item in inventory["summary"]["positions"]
        ):
            blockers.append("EXISTING_SYMBOL_POSITION")
        # Our own root-locked pre-POST CAS is expected. It must not yet have
        # venue identity or any fill; every other unresolved order still blocks.
        audited = deepcopy(facts)
        for order in audited["orders"]:
            if order["order_id"] == current_order and order["status"] == "SUBMITTING":
                if order["exchange_order_id"] is not None or order["has_fills"]:
                    blockers.append("OPENING_ALREADY_HAS_VENUE_EVIDENCE")
                else:
                    order["status"] = "PREPARED"
        blockers.extend(
            coverage_from_facts(
                self.scope,
                audited,
                inventory,
                excluded_position_symbols=self.audit.excluded_position_symbols,
            )
        )
        return sorted(set(blockers))

    def budget(self, conn, *, order_id, proof):
        profile = self.policy.read()
        if not profile.values["capital.enabled"]:
            raise ValueError("CAPITAL_POLICY_DISABLED_DURING_ADMISSION")
        account = proof["inventory"]["responses"]["account"]
        rows = conn.execute(
            """SELECT r.notional::text,e.snapshot->'features'->'evaluation'->'market_plan'->>'leverage'
            FROM v2_risk_reservations r JOIN v2_trade_intents i ON i.intent_id=r.episode_id
            JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE r.scope=%s AND r.status='HELD'
              AND r.episode_id<>(SELECT episode_id FROM v2_orders WHERE order_id=%s)
              AND NOT EXISTS(SELECT 1 FROM v2_orders o JOIN v2_fills f USING(order_id)
                  WHERE o.episode_id=r.episode_id)
              AND EXISTS(SELECT 1 FROM v2_orders o WHERE o.episode_id=r.episode_id
                  AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED'))""",
            (self.scope.key, order_id),
        ).fetchall()
        with localcontext() as context:
            context.prec = 100
            used = amount(account["totalInitialMargin"])
            available = amount(account["availableBalance"])
            wallet = amount(account["totalWalletBalance"])
            equity = amount(account["totalMarginBalance"])
            if min(used, available, wallet, equity) < 0:
                raise ValueError("INVALID_CAPITAL_ACCOUNT_VALUES")
            # totalInitialMargin already includes open-order initial margin.
            capital = current_health(
                lambda: nullcontext(conn), self.scope, account, profile.values
            )
            base = Decimal(capital["base"])
            fraction = Decimal(profile.values["capital.pool_fraction"])
            buffer = Decimal(profile.values["capital.margin_buffer"])
            pending = sum(
                (
                    amount(n, positive=True) / amount(l, positive=True) * buffer
                    for n, l in rows
                ),
                Decimal(0),
            )
            proposed = (
                amount(proof["quantity"], positive=True)
                * amount(proof["reference"]["mark_price"], positive=True)
                / proof["leverage"]
                * buffer
            )
            limit = base * fraction
            if used + pending + proposed > limit or pending + proposed > available:
                raise ValueError("ACCOUNT_MARGIN_BUDGET_EXCEEDED")
            episode = conn.execute(
                "SELECT episode_id FROM v2_orders WHERE order_id=%s", (order_id,)
            ).fetchone()[0]
            risk = guard_risk(
                conn,
                self.scope,
                episode=episode,
                notional=amount(proof["quantity"], positive=True)
                * amount(proof["reference"]["mark_price"], positive=True),
                leverage=proof["leverage"],
                settings=profile.values,
                capital=capital,
            )
            return {
                **risk,
                "policy_version": profile.version,
                "policy_digest": profile.digest,
                "account_scope": asdict(self.scope),
                "basis": "RECONSTRUCTED_AVAILABLE_CAPITAL",
                "capital_base": str(base),
                "fraction": str(fraction),
                "limit": str(limit),
                "venue_used_margin": str(used),
                "pending_margin": str(pending),
                "proposed_margin": str(proposed),
                "remaining_after": str(limit - used - pending - proposed),
            }
