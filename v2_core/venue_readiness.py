"""Testnet-only, scoped-account first-entry check. Never changes venue settings.

REST observations are not atomic; this is not a multi-position margin engine.
Reference and intended leverage/margin terms are explicit trusted dependencies.
"""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

from v2_core.account_inventory import AccountInventory
from v2_core.errors import SubmissionNotSent
from v2_core.ledger import amount, lock_order_episode
from v2_core.state import BusinessState, StateKey


class TestnetVenueReadiness:
    __test__ = False

    def __init__(
        self, request, *, scope, clock_ms, excluded_position_symbols=(), portfolio=None
    ):
        if (scope.exchange, scope.environment, scope.product) != (
            "BINANCE",
            "SANDBOX",
            "FUTURES",
        ):
            raise ValueError("testnet futures readiness only")
        self.request, self.scope, self.clock = request, scope, clock_ms
        self.portfolio = portfolio
        self.inventory = AccountInventory(
            request,
            scope=scope,
            clock_ms=clock_ms,
            max_duration_ms=10000,
            excluded_position_symbols=excluded_position_symbols,
        )

    def inspect(
        self, *, symbol, quantity, leverage, margin_type, reference, order_id=None
    ):
        if (
            type(leverage) is not int
            or leverage not in {1, 2, 3, 4, 5, 8}
            or margin_type not in {"ISOLATED", "CROSSED"}
        ):
            raise ValueError("bounded explicit intended account settings required")
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            raise ValueError("USDT settlement only")
        if symbol in self.inventory.excluded_position_symbols:
            raise ValueError("cannot trade an externally excluded position symbol")
        qty = amount(quantity, positive=True)
        ref = deepcopy(reference)
        started = self.clock()
        if (
            type(started) is not int
            or type(ref.get("observed_at_ms")) is not int
            or not 0 <= ref["observed_at_ms"] <= started < ref["observed_at_ms"] + 10000
            or ref.get("symbol") != symbol
            or ref.get("environment") != "SANDBOX"
        ):
            raise ValueError("fresh testnet mark reference required")
        mark = amount(ref["mark_price"], positive=True)
        managed = self.portfolio is not None and self.portfolio.enabled()
        before = self.portfolio.facts() if managed else None
        inventory = self.inventory.collect(str(uuid4()))
        blockers = set(
            self.portfolio.blockers(
                inventory, symbol, current_order=order_id, before=before
            )
            if managed
            else inventory["blockers"]
        )
        responses = {}
        try:
            responses["account_config"] = self.request(
                "GET", "/fapi/v1/accountConfig", {}
            )
            responses["symbol_config"] = self.request(
                "GET", "/fapi/v1/symbolConfig", {"symbol": symbol}
            )
        except Exception:  # noqa: BLE001 - never expose transport error strings
            blockers.add("VENUE_SETTINGS_UNAVAILABLE")
        required = None
        try:
            account = responses["account_config"]
            if (
                account.get("canTrade") is not True
                or account.get("dualSidePosition") is not False
                or account.get("multiAssetsMargin") is not False
            ):
                blockers.add("VENUE_PERMISSION_OR_MODE")
            rows = responses["symbol_config"]
            if (
                not isinstance(rows, list)
                or len(rows) != 1
                or rows[0]["symbol"] != symbol
            ):
                raise ValueError("ambiguous symbol settings")
            cfg = rows[0]
            if (
                type(cfg["leverage"]) is not int
                or cfg["leverage"] != leverage
                or cfg["marginType"] != margin_type
                or cfg["isAutoAddMargin"] is not False
            ):
                blockers.add("VENUE_SETTINGS_MISMATCH")
            with localcontext() as ctx:
                ctx.prec = 100
                notional = qty * mark
                # Conservative reference buffer, NOT a guarantee against gaps or fees.
                margin_buffer = (
                    self.portfolio.policy.read().values["capital.margin_buffer"]
                    if managed
                    else "1.10"
                )
                required = notional / Decimal(leverage) * Decimal(margin_buffer)
                balance = inventory["summary"]["balance"]
                available = amount(balance["availableBalance"])
                assets = inventory["responses"]["account"]["assets"]
                if not isinstance(assets, list):
                    raise TypeError("asset balances required")
                collateral = [asset for asset in assets if asset["asset"] == "USDT"]
                if len(collateral) != 1:
                    raise ValueError("unique USDT collateral required")
                usdt = collateral[0]
                if (
                    min(
                        amount(balance["totalWalletBalance"]),
                        amount(balance["totalMarginBalance"]),
                        available,
                        amount(usdt["availableBalance"]),
                        amount(usdt["walletBalance"]),
                        amount(usdt["marginBalance"]),
                    )
                    < required
                ):
                    blockers.add("INSUFFICIENT_VENUE_MARGIN")
                if notional > amount(cfg["maxNotionalValue"], positive=True):
                    blockers.add("VENUE_NOTIONAL_LIMIT")
        except (KeyError, ValueError, TypeError, AttributeError, IndexError):
            blockers.add("INVALID_VENUE_READINESS_RESPONSE")
        finished = self.clock()
        deadline = min(started + 10000, ref["observed_at_ms"] + 10000)
        if type(finished) is not int or not started <= finished < deadline:
            blockers.add("VENUE_READINESS_EXPIRED")
        return {
            "account_scope": asdict(self.scope),
            "symbol": symbol,
            "quantity": str(qty),
            "leverage": leverage,
            "margin_type": margin_type,
            "reference": ref,
            "inventory": inventory,
            "settings": responses,
            "blockers": sorted(blockers),
            "required_margin": None if required is None else str(required),
            "started_at_ms": started,
            "finished_at_ms": finished,
            "deadline_ms": deadline,
            "status": "BLOCKED" if blockers else "READY_FOR_GUARDED_SUBMISSION",
            "execution_authorized": False,
        }


class GuardedOpeningSubmit:
    """After order CAS, recheck venue and persist proof before calling submit.

    Only for cooperating account writers. All known unresolved sibling orders block.
    No retries here; downstream exceptions remain ambiguous to ExecutionRunner.
    """

    def __init__(
        self,
        connect,
        *,
        readiness,
        reference,
        plan,
        submit,
        enabled=False,
        configure=None,
        reduce_only_enabled=False,
    ):
        if (
            type(enabled) is not bool
            or type(reduce_only_enabled) is not bool
            or not all(callable(p) for p in (reference, plan, submit))
            or (configure is not None and not callable(configure))
        ):
            raise ValueError("explicit guarded submit ports required")
        self.connect, self.readiness = connect, readiness
        self.configure = configure
        (
            self.reference,
            self.plan,
            self.submit,
            self.enabled,
            self.reduce_only_enabled,
        ) = (
            reference,
            plan,
            submit,
            enabled,
            reduce_only_enabled,
        )

    def __call__(self, order):
        scope = self.readiness.scope
        if any(order.get(k) != v for k, v in asdict(scope).items()):
            raise SubmissionNotSent("VENUE_READINESS_BLOCKED")
        if order["leg"] == "CLOSE" and order.get("reduce_only") is True:
            if not self.reduce_only_enabled:
                raise SubmissionNotSent("ENDPOINT_OR_WRITE_DISABLED")
            return self.submit(order)
        if not self.enabled:
            raise SubmissionNotSent("ENDPOINT_OR_WRITE_DISABLED")
        if order["leg"] != "OPEN" or order.get("reduce_only") is not False:
            raise SubmissionNotSent("VENUE_READINESS_BLOCKED")
        with self.connect() as guard:
            try:
                if not guard.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                    ("testnet-protocol-account:" + scope.key,),
                ).fetchone()[0]:
                    raise ValueError("account busy")
                guard.commit()
                terms = self.plan(deepcopy(order))
                settings = (
                    None
                    if self.configure is None
                    else self.configure(deepcopy(order), deepcopy(terms))
                )
                if settings is not None and (
                    not isinstance(settings, dict)
                    or settings.get("status") != "VERIFIED"
                ):
                    raise ValueError("venue symbol settings unverified")
                proof = self.readiness.inspect(
                    symbol=order["symbol"],
                    quantity=order["quantity"],
                    leverage=terms["leverage"],
                    margin_type=terms["margin_type"],
                    reference=self.reference(order["symbol"]),
                    **(
                        {"order_id": order["order_id"]}
                        if getattr(self.readiness, "portfolio", None)
                        else {}
                    ),
                )
                if settings is not None:
                    proof["symbol_settings"] = settings
                key = StateKey(
                    **asdict(scope),
                    namespace="opening-readiness-v1",
                    key=str(UUID(order["order_id"])),
                )
                with self.connect() as conn:
                    lock_order_episode(conn, order["order_id"])
                    row = conn.execute(
                        """SELECT o.status,o.client_order_id,o.quantity::text,i.payload->>'symbol',i.payload->>'side',o.leg,
                        i.exchange,i.account_id,i.environment,i.product,e.snapshot->'expires_at_ms' FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                        JOIN v2_decision_evidence e USING(evidence_ref) WHERE o.order_id=%s""",
                        (order["order_id"],),
                    ).fetchone()
                    if (
                        row is None
                        or row[0] != "SUBMITTING"
                        or row[1] != order["client_order_id"]
                        or amount(row[2]) != amount(order["quantity"])
                        or row[3:6] != (order["symbol"], order["side"], "OPEN")
                        or row[6:10] != tuple(asdict(scope).values())
                        or type(row[10]) is not int
                    ):
                        raise ValueError("order not registered for submission")
                    proof["deadline_ms"] = min(proof["deadline_ms"], row[10])
                    if proof["finished_at_ms"] >= row[10]:
                        proof["blockers"].append("ENTRY_DECISION_EXPIRED")
                        proof["status"] = "BLOCKED"
                    exposure = conn.execute(
                        """SELECT i.intent_id FROM v2_trade_intents i JOIN v2_orders o ON i.intent_id=o.episode_id
                        JOIN v2_fills f USING(order_id) WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                        GROUP BY i.intent_id HAVING sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END)<>0 LIMIT 1""",
                        tuple(asdict(scope).values()),
                    ).fetchone()
                    portfolio = getattr(self.readiness, "portfolio", None)
                    managed = portfolio is not None and portfolio.enabled()
                    if exposure and not managed:
                        proof["blockers"].append("LOCAL_EXPOSURE_REQUIRES_RECOVERY")
                        proof["status"] = "BLOCKED"
                    siblings = conn.execute(
                        """SELECT 1 FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                        AND o.order_id<>%s AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED','PREPARED') LIMIT 1""",
                        (*asdict(scope).values(), order["order_id"]),
                    ).fetchone()
                    if siblings:
                        proof["blockers"].append("UNRESOLVED_LOCAL_ORDER")
                        proof["status"] = "BLOCKED"
                    if managed:
                        try:
                            proof["capital_budget"] = portfolio.budget(
                                conn, order_id=order["order_id"], proof=proof
                            )
                        except ValueError as exc:
                            from v2_core.scheduling import diagnostic_code

                            proof["blockers"].append(diagnostic_code(exc))
                            proof["status"] = "BLOCKED"
                    from contextlib import nullcontext

                    written = BusinessState(lambda: nullcontext(conn)).change(
                        key,
                        expected_version=0,
                        request_key="pre-submit",
                        payload=proof,
                        reason="OPENING_READINESS",
                    )
                    if written.code != "APPLIED":
                        raise ValueError("readiness proof already consumed")
                now = self.readiness.clock()
                if (
                    proof["blockers"]
                    or type(now) is not int
                    or not proof["finished_at_ms"] <= now < proof["deadline_ms"]
                ):
                    raise ValueError("readiness blocked or expired")
            except Exception:  # noqa: BLE001 - no call to submit has occurred
                raise SubmissionNotSent("VENUE_READINESS_BLOCKED") from None
            # Never catch this exception as definitely not sent.
            return self.submit(order)
