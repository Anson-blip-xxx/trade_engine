"""Synchronous, account-scoped structural qualification before admission.

Never use asynchronously recorded shadow verdicts. Only closed archived bars
available during THIS read qualify. Missing/old/oversized history cannot unlock
an entry. Terminal ignores consume the old signal; a fresh signal can requalify.
No venue writes, new tables, strategy thresholds chosen from today's PnL, or
time-based automatic unlocks. Activation is an explicit runtime policy change.
"""

from copy import deepcopy
from dataclasses import asdict

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import aggregate_minutes
from v2_core.reentry import ReentryPolicy, assess_reentry
from v2_core.runtime_policy import PolicyStore


def structure_policy(settings):
    return ReentryPolicy(
        **{
            key: settings["entry.reentry_" + key]
            for key in asdict(ReentryPolicy())
            if key != "timeframe_minutes"
        }
    )


def prior_close(conn, scope, target, now):
    """Latest filled episode, not a bounded slice mistaken for no history.

    A fully closed but unsettled/nonterminal episode is UNKNOWN, not an entry
    opportunity. Previous trade profit and future bars are never consulted.
    Terminal-timestamp fills of ONE closing order use quantity-weighted price;
    tied distinct closing orders remain ambiguous. Price is rounded to 18 dp.
    """
    row = conn.execute(
        """SELECT i.intent_id::text,i.payload->>'side',ep.status,f.oq,f.cq,
          f.closed_ms,f.known_ms,terminal.price,terminal.orders,
          CASE WHEN cl.request_evidence->>'origin'='BINANCE_ALGO_CHILD'
            THEN 'VENUE_PROTECTION_CHILD' ELSE cl.request_evidence->>'reason' END,
          EXISTS(SELECT 1 FROM v2_orders o WHERE o.episode_id=i.intent_id
            AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED'))
        FROM v2_trade_intents i JOIN v2_episodes ep ON ep.episode_id=i.intent_id
        JOIN LATERAL (
          SELECT sum(x.quantity) FILTER(WHERE o.leg='OPEN') oq,
            coalesce(sum(x.quantity) FILTER(WHERE o.leg='CLOSE'),0) cq,
            max(x.occurred_at_ms) FILTER(WHERE o.leg='CLOSE') closed_ms,
            (extract(epoch FROM max(greatest(x.created_at,o.updated_at)) FILTER(WHERE o.leg='CLOSE'))*1000)::bigint known_ms,
            max(x.occurred_at_ms) last_fill
          FROM v2_orders o JOIN v2_fills x USING(order_id) WHERE o.episode_id=i.intent_id
        ) f ON f.oq>0
        LEFT JOIN LATERAL (
          SELECT round(sum(x.price*x.quantity)/nullif(sum(x.quantity),0),18)::text price,
            count(DISTINCT o.order_id) orders
          FROM v2_orders o JOIN v2_fills x USING(order_id)
          WHERE o.episode_id=i.intent_id AND o.leg='CLOSE' AND x.occurred_at_ms=f.closed_ms
        ) terminal ON true
        LEFT JOIN LATERAL (
          SELECT request_evidence FROM v2_orders o
          JOIN LATERAL (SELECT max(x.occurred_at_ms) latest_fill FROM v2_fills x
            WHERE x.order_id=o.order_id) cf ON cf.latest_fill IS NOT NULL
          WHERE o.episode_id=i.intent_id AND leg='CLOSE'
          ORDER BY cf.latest_fill DESC,o.order_id DESC LIMIT 1
        ) cl ON true
        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
          AND i.payload->>'symbol'=%s AND i.producer IN ('s6','s8')
        ORDER BY f.last_fill DESC,i.intent_id DESC LIMIT 1""",
        (*asdict(scope).values(), target),
    ).fetchone()
    if row is None:
        return None, "NO_PREVIOUS_FILLED_EPISODE"
    (
        identity,
        side,
        status,
        oq,
        cq,
        closed,
        known,
        price,
        price_count,
        reason,
        pending,
    ) = row
    if (
        status != "SETTLED"
        or oq != cq
        or pending
        or not closed
        or not known
        or max(closed, known) > now
    ):
        return None, "PREVIOUS_EPISODE_UNRESOLVED"
    if price_count != 1 or not price:
        return None, "PREVIOUS_CLOSE_PRICE_AMBIGUOUS"
    return {
        "previous_episode": identity,
        "side": side,
        "exit_reason": reason,
        "exit_known_ms": max(closed, known),
        "exit_price": price,
    }, None


def assess(
    signal,
    *,
    previous,
    missing,
    side,
    now,
    read_bars,
    settings,
    checkpoints=None,
    cutoff_ms=None,
):
    if missing:
        return {
            "status": "NOT_APPLICABLE"
            if missing == "NO_PREVIOUS_FILLED_EPISODE"
            else "UNKNOWN",
            "reason": missing,
        }
    if previous["side"] not in {"BUY", "SELL"}:
        return {"status": "UNKNOWN", "reason": "PREVIOUS_SIDE_INVALID"}
    if previous["side"] != side:
        return {
            "status": "NOT_APPLICABLE",
            "reason": "PREVIOUS_OPPOSITE_SIDE",
            "previous_episode": previous["previous_episode"],
        }
    policy = structure_policy(settings)
    if checkpoints is not None:
        return checkpoints.evaluate(
            signal,
            previous=previous,
            policy=policy,
            now=now,
            page_bars=settings["entry.reentry_page_bars"],
            cutoff_ms=cutoff_ms,
            max_bar_age_ms=settings["entry.reentry_max_bar_age_ms"],
        )
    width = policy.timeframe_minutes * 60000
    warmup = max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period) + 2
    end = now // width * width
    start = max(0, (previous["exit_known_ms"] // width - warmup) * width)
    if end <= start or end - start > settings["entry.reentry_max_minutes"] * 60000:
        return {"status": "UNKNOWN", "reason": "REENTRY_HISTORY_BUDGET_EXCEEDED"}
    try:
        raw = read_bars(signal["symbol"], start, end)
        if len(raw) != (end - start) // 60000:
            raise ValueError("incomplete bars")
        bars = aggregate_minutes(
            list(reversed(raw)),
            timeframe_minutes=policy.timeframe_minutes,
            closed_at_ms=end,
        )
        return assess_reentry(
            bars=bars,
            side=side,
            signal_observed_ms=signal["observed_at"],
            as_of_ms=now,
            policy=policy,
            **{k: v for k, v in previous.items() if k != "side"},
        )
    except Exception:  # noqa: BLE001 - sanitized fail-closed evidence
        return {"status": "UNKNOWN", "reason": "REENTRY_ARCHIVE_UNAVAILABLE_OR_INVALID"}


class StructuralReentryContext:
    def __init__(self, connect, delegate, *, scope, strategy, read_bars, clock_ms):
        if scope.environment != "SANDBOX" or strategy not in {"S6", "S8"}:
            raise ValueError("REENTRY_SCOPE_REQUIRED")
        (
            self.connect,
            self.delegate,
            self.scope,
            self.strategy,
            self.read_bars,
            self.clock,
        ) = connect, delegate, scope, strategy, read_bars, clock_ms
        from services.v2_reentry_checkpoints import ReentryCheckpoints

        self.checkpoints = ReentryCheckpoints(
            connect, read_bars, scope=scope, clock_ms=clock_ms
        )

    def __call__(self, signal):
        context = deepcopy(self.delegate(signal))
        profile = PolicyStore(self.connect, self.scope).read()
        if (
            not profile.values["entry.reentry_enabled"]
            or context.get("market_only")
            or context.get("universe_admission", {}).get("eligible") is False
        ):
            return context
        now = self.clock()
        market_time = context.get("sources", {}).get("s3", {}).get("observed_at", now)
        if type(market_time) is not int or not 0 <= market_time <= now:
            raise ValueError("REENTRY_MARKET_CLOCK")
        cutoff = market_time // 60000 * 60000
        if (
            not context["assembled_at"]
            <= now
            < min(context["valid_until_ms"], signal["expires_at_ms"])
        ):
            raise ValueError("REENTRY_CONTEXT_EXPIRED")
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL statement_timeout='2s'")
            previous, missing = prior_close(conn, self.scope, signal["symbol"], now)
        side = "BUY" if self.strategy == "S6" else "SELL"
        result = assess(
            signal,
            previous=previous,
            missing=missing,
            side=side,
            now=now,
            read_bars=self.read_bars,
            settings=profile.values,
            checkpoints=self.checkpoints,
            cutoff_ms=cutoff,
        )
        finished = self.clock()
        deadline = min(
            context["valid_until_ms"],
            signal["expires_at_ms"],
            now + profile.values["entry.reentry_max_age_ms"],
            cutoff + profile.values["entry.reentry_max_bar_age_ms"],
        )
        if not now <= finished < deadline:
            raise ValueError("REENTRY_CONTEXT_EXPIRED")
        proof = {
            "source": "synchronous-reentry-v1",
            "account_scope": asdict(self.scope),
            "symbol": signal["symbol"],
            "side": side,
            "signal_observed_ms": signal["observed_at"],
            "observed_at_ms": now,
            "finished_at_ms": finished,
            "market_cutoff_ms": cutoff,
            "valid_until_ms": deadline,
            "policy": structure_policy(profile.values).__dict__,
            "assessment": result,
            "previous_episode": previous["previous_episode"] if previous else None,
            "previous_exit": previous,
            "execution_authorized": False,
        }
        proof["digest"] = digest(canonical(proof))
        context.update(reentry=proof, assembled_at=finished, valid_until_ms=deadline)
        return context


def validate(proof, *, scope, target, side, observed, now):
    if (
        not isinstance(proof, dict)
        or proof.get("source") != "synchronous-reentry-v1"
        or proof.get("account_scope") != scope
        or proof.get("symbol") != target
        or proof.get("side") != side
        or proof.get("signal_observed_ms") != observed
        or proof.get("execution_authorized") is not False
        or any(
            type(proof.get(k)) is not int
            for k in ("observed_at_ms", "finished_at_ms", "valid_until_ms")
        )
        or not proof["observed_at_ms"]
        <= proof["finished_at_ms"]
        <= now
        < proof["valid_until_ms"]
        or digest(canonical({k: v for k, v in proof.items() if k != "digest"}))
        != proof.get("digest")
    ):
        raise ValueError("REENTRY_PROOF_INVALID_OR_STALE")
    result = proof.get("assessment", {})
    if (
        result.get("status") == "READY"
        and result.get("opportunity_token")
        and proof.get("previous_episode")
    ):
        return None
    if result.get("status") == "NOT_APPLICABLE" and result.get("reason") in {
        "NO_PREVIOUS_FILLED_EPISODE",
        "PREVIOUS_OPPOSITE_SIDE",
    }:
        return None
    return "REENTRY_" + str(result.get("status", "UNKNOWN"))
