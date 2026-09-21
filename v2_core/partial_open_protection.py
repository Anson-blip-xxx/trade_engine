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
        self, connect, request, *, scope, reference, clock_ms, allow_writes=False
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
        facts = self.stop.audit.facts()
        owner = next(e for e in facts["episodes"] if e["episode"] == spec.episode)
        if amount(owner["remaining"]) == 0:
            return {"status": "NO_LEDGER_EXPOSURE"}
        return self.stop.ensure(spec)
