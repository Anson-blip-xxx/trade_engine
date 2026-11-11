"""Bounded synchronous Testnet structural evidence; no order or state writes.

Only the live archive read is causal. The incremental archive has no ingestion
timestamps: read completion bounds availability, and the proof explicitly is
NOT suitable for a historical as-of replay. Cold history exceeding the explicit
budget is UNKNOWN, never a long catch-up operation on the execution thread.
"""

from copy import deepcopy
from dataclasses import asdict
from time import time_ns

# Keep the historical service import path available to callers and replay tools.
from v2_core.entry_history import prior_close
from v2_core.entry_opportunities import SOURCE, seal
from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import aggregate_minutes
from v2_core.reentry import ReentryPolicy
from v2_core.reentry_checkpoint import advance, assess, seed


def assessment(*, signal, previous, missing, side, cutoff, as_of, bars, settings):
    if missing:
        return {
            "status": "NOT_APPLICABLE"
            if missing == "NO_PREVIOUS_FILLED_EPISODE"
            else "UNKNOWN",
            "reason": missing,
        }
    if previous["side"] not in {"BUY", "SELL"}:
        return {"status": "UNKNOWN", "reason": "PREVIOUS_SIDE_INVALID"}
    if settings.get("reentry.gate_scope", "CHASE_ONLY") == "TRADE_CYCLE":
        from v2_core.trade_cycle import CyclePolicy, assess_trade_cycle

        try:
            ordered = aggregate_minutes(
                list(reversed(bars)),
                timeframe_minutes=settings["reentry.policy"]["timeframe_minutes"],
                closed_at_ms=cutoff,
            )
            return assess_trade_cycle(
                bars=ordered,
                previous=previous,
                side=side,
                signal_observed_ms=signal["observed_at"],
                as_of_ms=as_of,
                bar_cutoff_ms=cutoff,
                policy=ReentryPolicy(**settings["reentry.policy"]),
                cycle_policy=CyclePolicy(**settings["reentry.cycle_policy"]),
            )
        except (ValueError, TypeError, KeyError, ArithmeticError):
            return {
                "status": "UNKNOWN",
                "reason": "REENTRY_ARCHIVE_UNAVAILABLE_OR_INVALID",
            }
    if previous["side"] != side:
        return {"status": "NOT_APPLICABLE", "reason": "PREVIOUS_OPPOSITE_SIDE"}
    policy = ReentryPolicy(**settings["reentry.policy"])
    width = policy.timeframe_minutes * 60000
    seed_end = (previous["exit_known_ms"] + width - 1) // width * width
    if seed_end > cutoff:
        return {"status": "WAIT", "reason": "NO_POST_EXIT_CLOSED_BAR"}
    try:
        ordered = aggregate_minutes(
            list(reversed(bars)),
            timeframe_minutes=policy.timeframe_minutes,
            closed_at_ms=cutoff,
        )
        state = seed(previous, [b for b in ordered if b.t < seed_end], policy=policy)
        state = advance(
            state, [b for b in ordered if b.t >= seed_end], through_ms=cutoff
        )
        return assess(
            state,
            signal_observed_ms=signal["observed_at"],
            as_of_ms=as_of,
            bar_cutoff_ms=cutoff,
            # Raw market freshness is separate from a 5m/15m closed candle.
            max_bar_age_ms=width + settings["reentry.max_bar_age_ms"],
        )
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return {"status": "UNKNOWN", "reason": "REENTRY_ARCHIVE_UNAVAILABLE_OR_INVALID"}


class EntryStructureContext:
    def __init__(
        self,
        connect,
        delegate,
        *,
        scope,
        strategy,
        read_bars,
        clock_ms,
        read_controls=None,
        archive_clock_ms=None,
    ):
        if scope.environment != "SANDBOX" or strategy not in {"S6", "S8"}:
            raise ValueError("ENTRY_STRUCTURE_SCOPE_REQUIRED")
        if read_controls is None:
            from v2_core.entry_controls import read

            read_controls = read
        self.connect, self.delegate, self.scope = connect, delegate, scope
        self.strategy, self.read_bars, self.clock = strategy, read_bars, clock_ms
        self.read_controls = read_controls
        # An injected historical decision clock must not backdate a real,
        # present-day archive read. Offline synthetic QA supplies BOTH clocks.
        self.archive_clock = archive_clock_ms or (lambda: time_ns() // 1_000_000)

    def __call__(self, signal):
        context = deepcopy(self.delegate(signal))
        # A definitive cheap market rejection needs no account research or
        # configuration query. Preserve the existing fast-consumption path.
        if (
            context.get("market_only")
            or context.get("universe_admission", {}).get("eligible") is False
        ):
            return context
        profile = self.read_controls(self.connect, self.scope)
        snapshot = profile.snapshot()
        for evidence in (
            context.get("account_evidence", {}),
            context.get("directional", {}).get("evidence", {}),
        ):
            if "entry_controls" in evidence and evidence["entry_controls"] != snapshot:
                raise ValueError("ENTRY_CONTROLS_CHANGED_REPROPOSE")
        context["entry_controls"] = snapshot
        settings = profile.values
        mode = settings["reentry.mode"]
        if (
            mode == "OFF"
            or context.get("market_only")
            or context.get("universe_admission", {}).get("eligible") is False
        ):
            return context
        if mode not in {"SHADOW", "ENFORCE"}:
            raise ValueError("ENTRY_STRUCTURE_MODE_INVALID")
        now = self.clock()
        market_time = context.get("sources", {}).get("s3", {}).get("observed_at")
        if type(market_time) is not int or not 0 <= market_time <= now:
            raise ValueError("ENTRY_STRUCTURE_MARKET_CLOCK")
        policy = ReentryPolicy(**settings["reentry.policy"])
        width = policy.timeframe_minutes * 60000
        cutoff = market_time // width * width
        deadline = min(
            context["valid_until_ms"],
            signal["expires_at_ms"],
            now + settings["reentry.max_age_ms"],
        )
        if not context["assembled_at"] <= now < deadline:
            raise ValueError("ENTRY_STRUCTURE_CONTEXT_EXPIRED")
        previous, missing = None, None
        try:
            with self.connect() as conn:
                conn.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                conn.execute("SET LOCAL statement_timeout='500ms'")
                previous, missing = prior_close(conn, self.scope, signal["symbol"], now)
        except Exception:  # noqa: BLE001 - no private database errors in evidence
            missing = "PREVIOUS_EPISODE_READ_UNAVAILABLE"
        side = "BUY" if self.strategy == "S6" else "SELL"
        bars = []
        archive_read_started = archive_read_finished = None
        read_issue = (
            "REENTRY_MARKET_STALE"
            if now - market_time >= settings["reentry.max_bar_age_ms"]
            else None
        )
        full_cycle = settings.get("reentry.gate_scope", "CHASE_ONLY") == "TRADE_CYCLE"
        if previous and (full_cycle or previous["side"] == side) and read_issue is None:
            warmup = (
                max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period) + 2
            )
            start = max(0, (previous["exit_known_ms"] // width - warmup) * width)
            if full_cycle:
                # Old exits never cause an unbounded catch-up or time-based
                # unlock. The pure rule explicitly builds a new closed-bar base.
                budget = settings["reentry.max_minutes"] * 60000 // width * width
                start = max(start, cutoff - budget)
            if (
                cutoff <= start
                or cutoff - start > settings["reentry.max_minutes"] * 60000
            ):
                read_issue = "REENTRY_HISTORY_BUDGET_EXCEEDED"
            else:
                archive_read_started = self.archive_clock()
                try:
                    bars = self.read_bars(signal["symbol"], start, cutoff)
                    if (
                        not isinstance(bars, list)
                        or len(bars) != (cutoff - start) // 60000
                    ):
                        raise ValueError("REENTRY_HISTORY_GAP")
                    for bar in bars:
                        if not isinstance(bar, dict) or type(bar.get("t")) is not int:
                            raise ValueError("REENTRY_ARCHIVE_BAR_INVALID")
                        canonical(bar)
                except Exception:  # noqa: BLE001 - unknown, never a fabricated READY
                    bars, read_issue = [], "REENTRY_ARCHIVE_UNAVAILABLE_OR_INVALID"
                archive_read_finished = self.archive_clock()
        as_of = self.clock()
        if archive_read_started is not None and not (
            type(archive_read_started) is int
            and type(archive_read_finished) is int
            and now <= archive_read_started <= archive_read_finished <= as_of
        ):
            read_issue = "REENTRY_ARCHIVE_AVAILABILITY_UNPROVEN"
        if as_of - market_time >= settings["reentry.max_bar_age_ms"]:
            read_issue = "REENTRY_MARKET_STALE"
        result = (
            {"status": "UNKNOWN", "reason": read_issue}
            if read_issue
            else assessment(
                signal=signal,
                previous=previous,
                missing=missing,
                side=side,
                cutoff=cutoff,
                as_of=as_of,
                bars=bars,
                settings=settings,
            )
        )
        finished = self.clock()
        if not now <= as_of <= finished < deadline:
            raise ValueError("ENTRY_STRUCTURE_CONTEXT_EXPIRED")
        proof = seal(
            {
                "source": SOURCE,
                "account_scope": asdict(self.scope),
                "symbol": signal["symbol"],
                "side": side,
                "strategy": self.strategy,
                "signal_observed_ms": signal["observed_at"],
                "observed_at_ms": now,
                "finished_at_ms": finished,
                "as_of_ms": as_of,
                "market_cutoff_ms": cutoff,
                "market_observed_at_ms": market_time,
                "valid_until_ms": deadline,
                "control_version": profile.version,
                "control_digest": profile.digest,
                "mode": mode,
                "enforce_reasons": settings["reentry.enforce_reasons"],
                "enforce_sides": settings["reentry.enforce_sides"],
                "policy": asdict(policy),
                "gate_scope": settings.get("reentry.gate_scope", "CHASE_ONLY"),
                "cycle_policy": settings.get("reentry.cycle_policy"),
                "assessment": result,
                "previous_episode": previous["previous_episode"] if previous else None,
                "previous_exit": previous,
                "execution_authorized": False,
                "archive_read_started_ms": archive_read_started,
                "archive_read_finished_ms": archive_read_finished,
                "archive_availability_method": "ONLINE_READ_COMPLETED_BEFORE_ASOF",
                "historical_replay_compatible": False,
                "archive_bar_digests": [
                    {"open_ms": bar["t"], "content_digest": digest(canonical(bar))}
                    for bar in bars
                ],
            }
        )
        context.update(
            entry_structure=proof, assembled_at=finished, valid_until_ms=deadline
        )
        return context
