"""Opt-in stop installation for a reconciled exclusive Testnet position.

Not a general PM: requires final opening orders and cooperating account writers.
Never cancels/replaces protection, adopts external positions or opens exposure.
"""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from decimal import localcontext
from uuid import uuid4

from v2_core.account_coverage import AccountCoverageAudit, coverage_from_facts
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.protection import TestnetProtection
from v2_core.state import BusinessState


class GuardedStopInstaller:
    def __init__(
        self,
        connect,
        request,
        *,
        scope,
        reference,
        clock_ms,
        allow_create=False,
        excluded_position_symbols=(),
    ):
        if (
            type(allow_create) is not bool
            or not callable(reference)
            or not callable(clock_ms)
        ):
            raise ValueError("explicit creation gate, reference and clock required")
        self.connect, self.request, self.scope = connect, request, scope
        self.reference, self.clock, self.allow_create = (
            reference,
            clock_ms,
            allow_create,
        )
        self.audit = AccountCoverageAudit(
            connect,
            request,
            scope=scope,
            clock_ms=clock_ms,
            excluded_position_symbols=excluded_position_symbols,
        )
        self.protection = TestnetProtection(
            BusinessState(connect), request, scope=scope
        )

    def ensure(self, spec):
        if spec.kind != "STOP_MARKET":
            raise ValueError("STOP_ONLY")
        existing, _ = self.protection.read(spec)
        if existing is not None:
            # Recovery must not depend on new market data or creation permission.
            return self.protection.submit_once(spec)
        if not self.allow_create:
            return {"status": "CREATION_DISABLED"}
        with self.connect() as guard:
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + self.scope.key,),
            ).fetchone()[0]:
                return {"status": "BUSY"}
            guard.commit()
            # Another installer could have completed while we acquired the lock.
            existing, _ = self.protection.read(spec)
            if existing is not None:
                return self.protection.submit_once(spec)
            before = self.audit.facts()
            observation = self.audit.inventory.collect(str(uuid4()))
            after = self.audit.facts()
            if before != after:
                raise ValueError("LOCAL_FACTS_CHANGED")
            issues = coverage_from_facts(self.scope, after, observation)
            if set(issues) - {"MISSING_CONFIRMED_STOP"}:
                raise ValueError("ACCOUNT_NOT_RECONCILED")
            owner = next(
                (e for e in after["episodes"] if e["episode"] == spec.episode), None
            )
            if (
                owner is None
                or owner["symbol"] != spec.symbol
                or amount(owner["remaining"]) <= 0
                or spec.side != ("SELL" if owner["side"] == "BUY" else "BUY")
            ):
                raise ValueError("STOP_EPISODE_OWNERSHIP_MISMATCH")
            reference = deepcopy(self.reference(spec.symbol))
            timestamp = self.clock()
            if (
                reference.get("symbol") != spec.symbol
                or reference.get("environment") != "SANDBOX"
                or type(reference.get("observed_at_ms")) is not int
                or reference["observed_at_ms"] < 0
                or type(timestamp) is not int
                or not 0 <= timestamp - reference["observed_at_ms"] < 10000
            ):
                raise ValueError("STALE_OR_WRONG_REFERENCE")
            mark = amount(reference["mark_price"], positive=True)
            tick = amount(reference["tick_size"], positive=True)
            price = amount(spec.trigger_price, positive=True)
            with localcontext() as ctx:
                ctx.prec = 100
                if price % tick or not (
                    price < mark if spec.side == "SELL" else price > mark
                ):
                    raise ValueError("INVALID_STOP_PRICE")
            proof = {
                "source": "exclusive-testnet-stop-install-v1",
                "account_scope": asdict(self.scope),
                "spec": asdict(spec),
                "inventory": observation,
                "facts_digest": digest(canonical(after)),
                "reference": reference,
                "deadline_ms": min(
                    reference["observed_at_ms"], observation["finished_at_ms"]
                )
                + 10000,
            }
            installer = self

            def fresh():
                now = installer.clock()
                if type(now) is not int or not timestamp <= now < proof["deadline_ms"]:
                    raise ValueError("PROTECTION_PROOF_EXPIRED")

            class CreationStore(BusinessState):
                def change(self, key, **kwargs):
                    if kwargs["expected_version"] != 0:
                        return super().change(key, **kwargs)
                    if key != installer.protection.key(spec):
                        raise ValueError("WRONG_CREATION_KEY")
                    with installer.connect() as conn:
                        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                        conn.execute(
                            "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                            (spec.episode,),
                        )
                        if installer.audit.facts(connection=conn) != after:
                            raise ValueError("FACTS_CHANGED_BEFORE_REGISTRATION")
                        fresh()
                        return BusinessState(lambda: nullcontext(conn)).change(
                            key,
                            **{
                                **kwargs,
                                "payload": {
                                    **kwargs["payload"],
                                    "creation_proof": proof,
                                },
                            },
                        )

            def protected_request(method, path, params):
                if method == "POST":
                    if (
                        path != "/fapi/v1/algoOrder"
                        or params != installer.protection.params(spec)
                    ):
                        raise ValueError("STOP_REQUEST_MISMATCH")
                    fresh()
                elif method != "GET":
                    raise ValueError("CANCELLATION_DISABLED")
                return installer.request(method, path, params)

            protected_request.account_id = self.scope.account_id
            protected_request.environment = self.scope.environment
            worker = TestnetProtection(
                CreationStore(self.connect), protected_request, scope=self.scope
            )
            return worker.submit_once(spec)
