"""Incremental PG checkpoints. No account keys or order ports.

Cold history is prepared in bounded pages by an independent caller. A signal
may advance at most one page, and MUST NOT use a checkpoint behind its cutoff.
No PG transaction/lock is held across archive I/O. CAS prevents lost updates.
"""

from dataclasses import asdict

from psycopg.types.json import Jsonb

from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol
from v2_core.quant_indicators import aggregate_minutes
from v2_core.reentry_checkpoint import advance, assess, identity, seed, verify


class ReentryCheckpoints:
    def __init__(self, connect, read_bars, *, scope, clock_ms):
        if scope.environment != "SANDBOX":
            raise ValueError("REENTRY_SANDBOX_REQUIRED")
        self.connect, self.read_bars, self.scope, self.clock = (
            connect,
            read_bars,
            scope,
            clock_ms,
        )

    def _transaction(self, conn):
        conn.execute("SET LOCAL statement_timeout='2s'")
        conn.execute("SET LOCAL lock_timeout='500ms'")

    def prepare(
        self, *, target, previous, policy, now, page_bars, max_pages=1, cutoff_ms=None
    ):
        symbol(target)
        if (
            type(page_bars) is not int
            or not 1 <= page_bars <= 1000
            or type(max_pages) is not int
            or not 1 <= max_pages <= 20
        ):
            raise ValueError("REENTRY_PAGE_BUDGET_INVALID")
        if type(now) is not int or not 0 < now <= self.clock():
            raise ValueError("REENTRY_CLOCK")
        scope = asdict(self.scope)
        bound = identity(previous, policy)
        key = digest(canonical({"scope": scope, "symbol": target, "identity": bound}))
        width = policy.timeframe_minutes * 60000
        latest = now // width * width
        cutoff = latest if cutoff_ms is None else cutoff_ms
        if type(cutoff) is not int or cutoff < 0 or cutoff > latest or cutoff % width:
            raise ValueError("REENTRY_BAR_CLOCK")
        with self.connect() as c:
            self._transaction(c)
            row = c.execute(
                "SELECT scope,identity,version,payload FROM v2_reentry_checkpoints WHERE checkpoint_id=%s",
                (key,),
            ).fetchone()
        checkpoint = None
        version = 0
        if row:
            saved_scope, saved_identity, version, checkpoint = row
            verify(checkpoint)
            if (
                saved_scope != scope
                or saved_identity != bound
                or checkpoint["identity"] != bound
            ):
                raise ValueError("REENTRY_CHECKPOINT_SCOPE_MISMATCH")
            if checkpoint["through_ms"] > latest:
                raise ValueError("REENTRY_CHECKPOINT_FUTURE")
            # A preparer may already know a newer closed bar; never rewind it.
            cutoff = max(cutoff, checkpoint["through_ms"])
        for _ in range(max_pages):
            if checkpoint and checkpoint["through_ms"] == cutoff:
                break
            exited = previous["exit_known_ms"]
            seed_end = (exited + width - 1) // width * width
            if seed_end > cutoff:
                return {"status": "UNKNOWN", "reason": "NO_POST_EXIT_CLOSED_BAR"}, None
            warmup = (
                max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period) + 2
            )
            start = (
                checkpoint["through_ms"]
                if checkpoint
                else max(0, (exited // width - warmup) * width)
            )
            end = min(
                cutoff,
                (checkpoint["through_ms"] if checkpoint else seed_end)
                + page_bars * width,
            )
            raw = self.read_bars(target, start, end)
            if len(raw) != (end - start) // 60000:
                raise ValueError("REENTRY_CHECKPOINT_HISTORY_GAP")
            bars = aggregate_minutes(
                list(reversed(raw)),
                timeframe_minutes=policy.timeframe_minutes,
                closed_at_ms=end,
            )
            if checkpoint is None:
                checkpoint = seed(
                    previous, [b for b in bars if b.t < seed_end], policy=policy
                )
            checkpoint = advance(
                checkpoint,
                [b for b in bars if b.t >= checkpoint["through_ms"]],
                through_ms=end,
            )
            with self.connect() as c:
                self._transaction(c)
                if version == 0:
                    saved = c.execute(
                        "INSERT INTO v2_reentry_checkpoints(checkpoint_id,scope,identity,version,through_ms,payload) VALUES(%s,%s,%s,1,%s,%s) ON CONFLICT DO NOTHING RETURNING version",
                        (key, Jsonb(scope), Jsonb(bound), end, Jsonb(checkpoint)),
                    ).fetchone()
                else:
                    saved = c.execute(
                        "UPDATE v2_reentry_checkpoints SET version=version+1,through_ms=%s,payload=%s,updated_at=clock_timestamp() WHERE checkpoint_id=%s AND version=%s AND scope=%s AND identity=%s AND through_ms<%s RETURNING version",
                        (
                            end,
                            Jsonb(checkpoint),
                            key,
                            version,
                            Jsonb(scope),
                            Jsonb(bound),
                            end,
                        ),
                    ).fetchone()
                if not saved:
                    return {
                        "status": "UNKNOWN",
                        "reason": "REENTRY_CHECKPOINT_CONCURRENT_UPDATE",
                    }, None
                version = saved[0]
        if checkpoint is None or checkpoint["through_ms"] != cutoff:
            return {
                "status": "UNKNOWN",
                "reason": "REENTRY_CHECKPOINT_CATCHING_UP",
            }, checkpoint
        return {
            "status": "CURRENT",
            "checkpoint_id": key,
            "checkpoint_version": version,
        }, checkpoint

    def evaluate(
        self,
        signal,
        *,
        previous,
        policy,
        now,
        page_bars,
        cutoff_ms=None,
        max_bar_age_ms=90000,
    ):
        try:
            status, checkpoint = self.prepare(
                target=signal["symbol"],
                previous=previous,
                policy=policy,
                now=now,
                page_bars=page_bars,
                cutoff_ms=cutoff_ms,
            )
            if status["status"] != "CURRENT":
                return {**status, "execution_authorized": False}
            result = assess(
                checkpoint,
                signal_observed_ms=signal["observed_at"],
                as_of_ms=now,
                bar_cutoff_ms=checkpoint["through_ms"],
                max_bar_age_ms=max_bar_age_ms,
            )
            return {
                **result,
                "checkpoint_id": status["checkpoint_id"],
                "checkpoint_version": status["checkpoint_version"],
            }
        except Exception:  # noqa: BLE001 - terminal sanitized UNKNOWN, never retry old signals forever
            return {
                "status": "UNKNOWN",
                "reason": "REENTRY_CHECKPOINT_UNAVAILABLE_OR_INVALID",
                "execution_authorized": False,
            }
