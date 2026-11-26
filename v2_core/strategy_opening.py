"""Typed-plan adapter for the existing guarded Testnet opening transport.

This adapter does not dispatch, create a submission permit or recover orders.
Only the initial CAS winner calls it, under the protocol account lock. All DB
locks finish before settings/network I/O; ambiguous requests use query recovery.
"""

from contextlib import nullcontext
from dataclasses import asdict
from fractions import Fraction

from v2_core.entry_controls import validate_submission
from v2_core.entry_price import validate_entry_price
from v2_core.ledger import amount, lock_order_episode
from v2_core.liquidity_submission import validate_opening_liquidity
from v2_core.opening_halt import require_opening_allowed
from v2_core.runtime_policy import PolicyStore
from v2_core.strategy_capital import read_strategy_capital_for_episode
from v2_core.strategy_execution_context import read_execution_context


class StrategyOpeningGate:
    def __init__(self, connect, *, scope, clock_ms):
        self.connect, self.scope, self.clock = connect, scope, clock_ms

    def validate_reduction(self, order):
        """Bind a reduction to its committed request, independent of entry state.

        Both typed and legacy positions use this check. No frozen entry plan,
        current deployment, price, control snapshot or entry deadline is read.
        """
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='2s'")
            conn.execute("SET LOCAL lock_timeout='2s'")
            lock_order_episode(conn, order["order_id"])
            row = conn.execute(
                "SELECT o.order_id::text,o.episode_id::text,o.status,o.version,"
                "o.client_order_id,o.quantity::text,o.leg,o.order_type,"
                "o.limit_price::text,o.time_in_force,o.request_evidence,"
                "o.exchange_order_id,i.payload FROM v2_orders o "
                "JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
                "WHERE o.order_id=%s AND "
                "(i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)",
                (order["order_id"], *asdict(self.scope).values()),
            ).fetchone()
            if row is None:
                raise ValueError("STRATEGY_REDUCTION_ORDER_MISSING")
            expected = {
                **asdict(self.scope),
                "order_id": row[0],
                "client_order_id": row[4],
                "leg": "CLOSE",
                "order_type": row[7],
                "limit_price": row[8],
                "time_in_force": row[9],
                "request_evidence": row[10],
                "exchange_order_id": row[11],
                "intent": row[12],
                "symbol": row[12]["symbol"],
                "side": "SELL" if row[12]["side"] == "BUY" else "BUY",
                "reduce_only": True,
            }
            if (
                row[2] != "SUBMITTING"
                or row[6] != "CLOSE"
                or order.get("status") != "PREPARED"
                or type(order.get("version")) is not int
                or order["version"] != row[3] - 1
                or order.get("reduce_only") is not True
                or any(
                    key not in order or order[key] != value
                    for key, value in expected.items()
                )
                or amount(order["quantity"], positive=True)
                != amount(row[5], positive=True)
                or ("episode_id" in order and order["episode_id"] != row[1])
            ):
                raise ValueError("STRATEGY_REDUCTION_ORDER_MISMATCH")

    def _read(self, order):
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='2s'")
            conn.execute("SET LOCAL lock_timeout='2s'")
            typed = read_execution_context(conn, self.scope, order["order_id"])
            if typed is None:
                return None
            plan, context = typed
            lock_order_episode(conn, order["order_id"])
            row = conn.execute(
                "SELECT o.episode_id::text,o.status,o.version,o.client_order_id,"
                "o.quantity::text,o.leg,o.order_type,o.limit_price::text,"
                "o.time_in_force,o.request_evidence,o.exchange_order_id,i.payload "
                "FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id "
                "WHERE o.order_id=%s",
                (order["order_id"],),
            ).fetchone()
            if row is None:
                raise ValueError("STRATEGY_OPEN_ORDER_MISSING")
            expected = {
                **asdict(self.scope),
                "client_order_id": row[3],
                "leg": "OPEN",
                "order_type": "MARKET",
                "limit_price": None,
                "time_in_force": None,
                "request_evidence": row[9],
                "exchange_order_id": None,
                "intent": row[11],
                "symbol": plan.symbol,
                "side": plan.side,
                "reduce_only": False,
            }
            if (
                row[1] != "SUBMITTING"
                or row[5:9] != ("OPEN", "MARKET", None, None)
                or row[10] is not None
                or any(
                    key not in order or order[key] != value
                    for key, value in expected.items()
                )
                or order.get("reduce_only") is not False
                # Runner passes its original pre-CAS snapshot to submit.
                or order.get("status") != "PREPARED"
                or type(order.get("version")) is not int
                or order["version"] != row[2] - 1
                or amount(order["quantity"], positive=True)
                != amount(row[4], positive=True)
                or amount(row[4], positive=True) != amount(plan.quantity, positive=True)
                or ("episode_id" in order and order["episode_id"] != row[0])
            ):
                raise ValueError("STRATEGY_OPEN_ORDER_MISMATCH")
            # Recheck before an initial POST only. Never apply these checks to
            # query recovery or reject a possibly accepted exchange submission.
            require_opening_allowed(conn, row[0])
            capital = read_strategy_capital_for_episode(conn, row[0], scope=self.scope)
            if capital is None or capital.plan != plan:
                raise ValueError("STRATEGY_OPEN_CAPITAL_REQUIRED")
            current = PolicyStore(lambda: nullcontext(conn), self.scope).read()
            if not all(
                current.values[key]
                for key in ("capital.enabled", "portfolio_risk.enabled")
            ):
                raise ValueError("STRATEGY_OPEN_CAPITAL_GUARDS_REQUIRED")
            tick = self.clock()
            if type(tick) is not int or not plan.entry_valid_at(tick):
                raise ValueError("STRATEGY_OPEN_EXPIRED")
            validate_submission(conn, self.scope, order["order_id"], now_ms=tick)
            validate_opening_liquidity(conn, self.scope, order, now_ms=tick)
            return plan, context, capital, current

    def terms(self, order):
        saved = self._read(order)
        if saved is None:
            return None
        plan = saved[0]
        return {"leverage": plan.leverage, "margin_type": plan.margin_mode}

    def validate_reference(self, order, reference):
        saved = self._read(order)
        if saved is None:
            return None
        plan, context, capital, current = saved
        tick = self.clock()
        if (
            type(reference) is not dict
            or reference.get("symbol") != plan.symbol
            or reference.get("environment") != plan.scope.environment
            or type(reference.get("observed_at_ms")) is not int
            or type(tick) is not int
            or not 0
            <= reference["observed_at_ms"]
            <= tick
            < reference["observed_at_ms"] + 10000
        ):
            raise ValueError("STRATEGY_OPEN_REFERENCE_INVALID")
        mark = amount(reference.get("mark_price"), positive=True)
        original = amount(plan.entry_price, positive=True)
        stop = amount(plan.stop_price, positive=True)
        if (plan.side == "BUY" and mark <= stop) or (
            plan.side == "SELL" and mark >= stop
        ):
            raise ValueError("STRATEGY_OPEN_STOP_INVALID")
        # This frozen two-sided price window is mandatory, even if optional
        # shared adverse-price guards are off. Fraction avoids context rounding.
        if abs(Fraction(mark) - Fraction(original)) / Fraction(original) > Fraction(
            capital.price_drift_fraction
        ):
            raise ValueError("STRATEGY_OPEN_PRICE_MOVED")
        price_guard = validate_entry_price(
            side=plan.side,
            original=plan.entry_price,
            mark=reference["mark_price"],
            stop=plan.stop_price,
            frozen=context["policy"],
            current=current.values,
        )
        return {
            "source": "strategy-pre-submit-price-v1",
            "plan_digest": plan.plan_digest,
            "reference_price": plan.entry_price,
            "mark_price": str(mark),
            "max_absolute_drift_fraction": str(capital.price_drift_fraction),
            "shared_price_guard": price_guard,
            "policy_version": current.version,
            "execution_authorized": False,
        }
