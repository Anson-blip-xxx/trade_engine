"""Stop a growing opening order before protecting its reconciled residual.

This bounded recovery workflow is not continuous PM supervision. During an
unconfirmed cancellation it cannot promise protection; it must surface a block.
"""

from v2_core.ledger import amount
from v2_core.opening_cancel import FINAL, TestnetOpeningCancel
from v2_core.opening_halt import halt_opening
from v2_core.protection_install import GuardedStopInstaller


class PartialOpenProtection:
    def __init__(
        self,
        connect,
        request,
        *,
        scope,
        reference,
        clock_ms,
        allow_writes=False,
        excluded_position_symbols=(),
    ):
        self.cancel = TestnetOpeningCancel(
            connect, request, scope=scope, allow_cancel=allow_writes
        )
        self.stop = GuardedStopInstaller(
            connect,
            request,
            scope=scope,
            reference=reference,
            clock_ms=clock_ms,
            allow_create=allow_writes,
            excluded_position_symbols=excluded_position_symbols,
        )

    def ensure(self, order_id, spec):
        order = self.cancel.snapshot(order_id)
        if (
            order["episode_id"] != spec.episode
            or order["symbol"] != spec.symbol
            or spec.kind != "STOP_MARKET"
            or spec.side != ("SELL" if order["side"] == "BUY" else "BUY")
        ):
            raise ValueError("PARTIAL_PROTECTION_OWNERSHIP_MISMATCH")
        if self.cancel.allow_cancel:
            halt_opening(self.cancel.connect, spec.episode, scope=self.cancel.scope)
        outcome = self.cancel.cancel_once(order_id)
        if outcome not in FINAL:
            return {"status": "OPENING_NOT_FINAL", "opening_status": outcome}
        # Only this episode's residual is needed here. The installer retains
        # its full before/after coverage audit before any new protection write.
        # Re-reading all historical orders/protections once per open position
        # made routine protection supervision grow with account lifetime.
        with self.cancel.connect() as conn:
            remaining = conn.execute(
                "SELECT COALESCE(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0)::text "
                "FROM v2_orders o JOIN v2_fills f USING(order_id) WHERE o.episode_id=%s",
                (spec.episode,),
            ).fetchone()[0]
        if amount(remaining) == 0:
            return {"status": "NO_LEDGER_EXPOSURE"}
        return self.stop.ensure(spec)
