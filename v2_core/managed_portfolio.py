"""Owned multi-symbol Testnet exposure and fresh aggregate margin admission.

All callers use the account protocol advisory lock through GuardedOpeningSubmit.
Unknown sibling submissions block; pending reservations are counted before POST.
"""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from fractions import Fraction

from v2_core.account_coverage import AccountCoverageAudit, coverage_from_facts
from v2_core.capital_model import current_health, guard_risk
from v2_core.ledger import amount
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_capital import (
    STRATEGY_CAPITAL_RECORD_SQL,
    read_strategy_capital,
)


def _pool_fraction(settings):
    fraction = amount(settings["capital.pool_fraction"], positive=True)
    if fraction > Decimal("0.70"):
        raise ValueError("CAPITAL_RESERVE_FLOOR_REQUIRED")
    return fraction


def _margin(notional, leverage, buffer):
    """Round a derived margin upward, independent of ambient Decimal precision."""
    exact = Fraction(notional) / Fraction(leverage) * Fraction(buffer)
    units = -(-(exact.numerator * 10**18) // exact.denominator)
    integer, fraction = divmod(units, 10**18)
    return amount(f"{integer}.{fraction:018d}", positive=True)


def _reserved_margin(value):
    if value is None:
        return Decimal(0)
    # Older reservations serialized an unquantized division. Preserve their
    # original floor without requiring those historical values to fit scale 18.
    if type(value) is not str or len(value) > 200:
        raise ValueError("CAPITAL_RESERVED_MARGIN_INVALID")
    try:
        original = Decimal(value)
        if (
            not original.is_finite()
            or original <= 0
            or original >= Decimal("1e20")
            or original.as_tuple().exponent < -140
        ):
            raise ValueError("invalid original margin")
        return _margin(original, 1, 1)
    except (ValueError, ArithmeticError):
        raise ValueError("CAPITAL_RESERVED_MARGIN_INVALID") from None


@dataclass(frozen=True)
class PendingMargin:
    amount: Decimal
    partial_open_reservations: int
    snapshot_overlap_reservations: int
    incomplete_open_reservations: int

    def evidence(self):
        return {
            "pending_margin": str(self.amount),
            "pending_partial_open_reservations": self.partial_open_reservations,
            "pending_snapshot_overlap_reservations": self.snapshot_overlap_reservations,
            "pending_incomplete_open_reservations": self.incomplete_open_reservations,
        }


def _original_capital(capital, projection):
    if projection is not None:
        if (
            type(capital) is not dict
            or capital.get("strategy_plan_digest") != projection.plan.plan_digest
            or capital.get("reserved_margin") is None
        ):
            raise ValueError("STRATEGY_CAPITAL_RESERVATION_UNVERIFIED")
        try:
            margin = amount(capital["reserved_margin"], positive=True)
            risk = amount(capital["proposed_stop_risk"], positive=True)
        except (ValueError, KeyError, TypeError):
            raise ValueError("STRATEGY_CAPITAL_RESERVATION_UNVERIFIED") from None
        return margin, risk
    if capital is not None and type(capital) is not dict:
        raise ValueError("CAPITAL_RESERVED_MARGIN_INVALID")
    return _reserved_margin((capital or {}).get("reserved_margin")), None


@dataclass(frozen=True)
class OpeningCapital:
    episode: str
    reserved_margin: Decimal
    reserved_stop_risk: Decimal | None


def _pending_margin(conn, scope, *, episode, buffer, snapshot_started=None):
    """Batch-read every held sibling; absence/partial facts are not free capital.

    A complete opening known before the snapshot is covered by venue margin.
    A complete opening on either snapshot boundary conservatively retains its
    reservation as well. Unknown/unfilled openings always retain the full
    reservation. Partial openings retain their FULL reservation until a precise
    allocation adapter exists. This can overlap venue margin but never treats
    uncertainty as free capital or arbitrarily halts unrelated candidates.
    """
    rows = conn.execute(
        """SELECT r.notional::text,
        e.snapshot->'features'->'evaluation'->'market_plan'->>'leverage',
        e.evidence_ref,i.payload->>'quantity',"""
        + STRATEGY_CAPITAL_RECORD_SQL
        + """,r.reference->'capital',
        floor(extract(epoch FROM r.created_at)*1000)::bigint,
        fills.quantity::text,fills.latest_ms,fills.open_orders,fills.unresolved,
        jsonb_build_object('exchange',i.exchange,'account_id',i.account_id,
            'environment',i.environment,'product',i.product)
        FROM v2_risk_reservations r
        LEFT JOIN v2_trade_intents i ON i.intent_id=r.episode_id
        LEFT JOIN v2_decision_evidence e USING(evidence_ref)
        LEFT JOIN LATERAL (
            SELECT sum(f.quantity) AS quantity,
                max(greatest(f.occurred_at_ms,
                    floor(extract(epoch FROM f.created_at)*1000)::bigint)) AS latest_ms,
                count(DISTINCT o.order_id) AS open_orders,
                bool_or(o.status NOT IN ('FILLED','CANCELLED','REJECTED')) AS unresolved
            FROM v2_orders o LEFT JOIN v2_fills f USING(order_id)
            WHERE o.episode_id=r.episode_id AND o.leg='OPEN'
        ) fills ON TRUE
        WHERE r.scope=%s AND r.status='HELD' AND r.episode_id<>%s""",
        (scope.key, episode),
    ).fetchall()
    pending = Fraction(0)
    partial_count = overlap_count = incomplete_count = 0
    for (
        notional,
        legacy_leverage,
        evidence_ref,
        quantity,
        record,
        original_capital,
        created_ms,
        filled,
        latest_ms,
        open_orders,
        unresolved,
        row_scope,
    ) in rows:
        if evidence_ref is None or row_scope != asdict(scope):
            raise ValueError("CAPITAL_PENDING_EVIDENCE_REQUIRED")
        projection = read_strategy_capital(record, scope=scope)
        leverage = (
            Decimal(projection.plan.leverage)
            if projection is not None
            else amount(legacy_leverage, positive=True)
        )
        if leverage != leverage.to_integral_value() or not 1 <= leverage <= 125:
            raise ValueError("CAPITAL_PENDING_LEVERAGE_INVALID")
        notional = amount(notional, positive=True)
        full_quantity = amount(quantity, positive=True)
        filled_quantity = amount(filled) if filled is not None else Decimal(0)
        if not 0 <= filled_quantity <= full_quantity:
            raise ValueError("CAPITAL_PENDING_FILL_INVALID")
        frozen_margin, _ = _original_capital(original_capital, projection)
        proposed = max(_margin(notional, leverage, buffer), frozen_margin)
        partial = 0 < filled_quantity < full_quantity
        incomplete = filled_quantity == 0 or unresolved or open_orders != 1
        snapshot_overlap = False
        if snapshot_started is not None:
            snapshot_overlap = filled_quantity > 0 and (
                created_ms >= snapshot_started
                or (latest_ms is not None and latest_ms >= snapshot_started)
            )
        pending_required = partial or incomplete or snapshot_overlap
        if pending_required:
            pending += Fraction(proposed)
            partial_count += int(partial)
            overlap_count += int(snapshot_overlap)
            incomplete_count += int(bool(incomplete))
    return PendingMargin(
        _margin(pending, 1, 1) if pending else Decimal(0),
        partial_count,
        overlap_count,
        incomplete_count,
    )


def _opening_episode(conn, scope, *, order_id, proof, settings):
    row = conn.execute(
        "SELECT o.episode_id,o.quantity::text,o.leg,"
        + STRATEGY_CAPITAL_RECORD_SQL
        + """,e.evidence_ref,
        EXISTS(SELECT 1 FROM v2_orders sibling JOIN v2_fills f USING(order_id)
            WHERE sibling.episode_id=o.episode_id AND sibling.leg='OPEN'),
        EXISTS(SELECT 1 FROM v2_orders sibling
            WHERE sibling.episode_id=o.episode_id AND sibling.leg='OPEN'
                AND sibling.order_id<>o.order_id),
        r.reference->'capital'
        FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
        LEFT JOIN v2_decision_evidence e USING(evidence_ref)
        LEFT JOIN v2_risk_reservations r ON r.episode_id=o.episode_id
            AND r.status='HELD' AND r.scope=%s
        WHERE o.order_id=%s
          AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)""",
        (scope.key, order_id, *asdict(scope).values()),
    ).fetchone()
    if row is None or row[2] != "OPEN" or row[4] is None:
        raise ValueError("CAPITAL_OPENING_EVIDENCE_REQUIRED")
    (
        episode,
        order_quantity,
        _leg,
        record,
        _evidence_ref,
        has_fills,
        siblings,
        original_capital,
    ) = row
    projection = read_strategy_capital(record, scope=scope)
    if projection is not None:
        if not settings["portfolio_risk.enabled"]:
            raise ValueError("STRATEGY_CAPITAL_POLICY_REQUIRED")
        quantity = amount(projection.plan.quantity, positive=True)
        if (
            amount(proof["quantity"], positive=True) != quantity
            or amount(order_quantity, positive=True) != quantity
            or has_fills
            or siblings
        ):
            raise ValueError("STRATEGY_CAPITAL_SINGLE_OPEN_REQUIRED")
        if (
            type(proof["leverage"]) is not int
            or proof["leverage"] != projection.plan.leverage
        ):
            raise ValueError("STRATEGY_CAPITAL_LEVERAGE_MISMATCH")
    margin, risk = _original_capital(original_capital, projection)
    return OpeningCapital(str(episode), margin, risk)


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
    fraction = _pool_fraction(policy.values)
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
        pending_budget = _pending_margin(
            conn,
            scope,
            episode=episode,
            buffer=buffer,
            snapshot_started=snapshot["started_at_ms"],
        )
        pending = pending_budget.amount
        capital = current_health(connect, scope, account, policy.values)
        base = Decimal(capital["base"])
        limit = base * fraction
        proposed = _margin(notional, leverage, buffer)
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
            **pending_budget.evidence(),
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
        fraction = _pool_fraction(profile.values)
        opening = _opening_episode(
            conn, self.scope, order_id=order_id, proof=proof, settings=profile.values
        )
        episode, original_margin = opening.episode, opening.reserved_margin
        started = proof.get("started_at_ms")
        if type(started) is not int or started < 0:
            raise ValueError("CAPITAL_SNAPSHOT_TIME_REQUIRED")
        account = proof["inventory"]["responses"]["account"]
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
            buffer = Decimal(profile.values["capital.margin_buffer"])
            pending_budget = _pending_margin(
                conn,
                self.scope,
                episode=episode,
                buffer=buffer,
                snapshot_started=started,
            )
            pending = pending_budget.amount
            proposed = _margin(
                amount(proof["quantity"], positive=True)
                * amount(proof["reference"]["mark_price"], positive=True),
                proof["leverage"],
                buffer,
            )
            proposed = max(proposed, original_margin)
            limit = base * fraction
            if used + pending + proposed > limit or pending + proposed > available:
                raise ValueError("ACCOUNT_MARGIN_BUDGET_EXCEEDED")
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
            if opening.reserved_stop_risk is not None:
                try:
                    proposed_risk = amount(risk["proposed_stop_risk"], positive=True)
                except (ValueError, KeyError, TypeError):
                    raise ValueError(
                        "STRATEGY_CAPITAL_RESERVATION_UNVERIFIED"
                    ) from None
                # No atomic reservation top-up exists in this first adapter.
                # Revalidating a larger budget does not change the immutable
                # reservation other candidates subsequently account against.
                if (
                    proposed > original_margin
                    or proposed_risk > opening.reserved_stop_risk
                ):
                    raise ValueError("STRATEGY_CAPITAL_RESERVATION_EXPANSION_REQUIRED")
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
                **pending_budget.evidence(),
                "proposed_margin": str(proposed),
                "remaining_after": str(limit - used - pending - proposed),
            }
