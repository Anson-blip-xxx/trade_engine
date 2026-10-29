"""Archive-backed software Chandelier exit; NEVER replaces the venue hard stop.

Only closed bars after entry contribute extrema. Frozen entry parameters choose
the ATR timeframe/multiplier. PG exit state carries monotonic stops across restarts.
"""

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import VERSION, aggregate_minutes, decimal, encode
from v2_core.quant_registry import feature_frame, parameters, trailing_stop, warmup


class QuantExitMarket:
    def __init__(self, archive):
        if archive.environment != "SANDBOX":
            raise ValueError("QUANT_EXIT_SANDBOX_ONLY")
        self.archive = archive

    def __call__(
        self,
        *,
        entry_proof,
        symbol,
        side,
        opened_at_ms,
        entry_price,
        hard_stop,
        mark_price,
        previous,
        now,
    ):
        if (
            entry_proof["environment"] != "SANDBOX"
            or entry_proof["symbol"] != symbol
            or entry_proof["frame"]["version"] != VERSION
            or type(now) is not int
            or type(opened_at_ms) is not int
            or now < opened_at_ms
        ):
            raise ValueError("QUANT_EXIT_EVIDENCE_SCOPE")
        frozen = entry_proof["frame"]
        p = parameters(frozen["parameters"])
        tf = frozen["timeframe_minutes"]
        closed = now // (tf * 60000) * tf * 60000
        rows = self.archive.read(symbol, closed - warmup(p) * tf * 60000, closed)
        bars = aggregate_minutes(
            list(reversed(rows)), timeframe_minutes=tf, closed_at_ms=closed
        )
        frame = feature_frame(bars, timeframe_minutes=tf, params=p)
        if frame["status"] != "READY" or decimal(frame["current"]["atr"]) <= 0:
            raise ValueError("QUANT_EXIT_HISTORY_UNAVAILABLE")
        reference = digest(canonical(entry_proof))
        previous = previous or {}
        if previous and previous.get("entry_proof_digest") != reference:
            raise ValueError("QUANT_EXIT_STATE_SCOPE")
        if (
            previous.get("closed_at_ms", 0) > closed
            or previous.get("observed_at_ms", 0) > now
        ):
            raise ValueError("QUANT_EXIT_TIME_REGRESSION")
        entry, mark, stop = map(decimal, (entry_price, mark_price, hard_stop))
        since = [
            b
            for b in bars
            if b.t >= opened_at_ms and b.t >= previous.get("closed_at_ms", 0)
        ]
        high = max(
            [entry, mark, decimal(previous.get("high_water", entry_price))]
            + [b.h for b in since]
        )
        low = min(
            [entry, mark, decimal(previous.get("low_water", entry_price))]
            + [b.l for b in since]
        )
        prior_stop = decimal(previous.get("trailing_stop", hard_stop))
        # A resumed, already-protected episode never loosens its original stop.
        prior_stop = max(stop, prior_stop) if side == "BUY" else min(stop, prior_stop)
        trail = trailing_stop(
            side=side,
            prior_stop=prior_stop,
            high_water=high,
            low_water=low,
            atr=frame["current"]["atr"],
            multiplier=p["trail_atr"],
        )
        crossed = mark <= decimal(trail) if side == "BUY" else mark >= decimal(trail)
        return {
            "status": "CLOSE" if crossed else "WAIT",
            "mode": "SOFTWARE_REDUCE_ONLY_WITH_NATIVE_HARD_STOP",
            "entry_proof_digest": reference,
            "observed_at_ms": now,
            "closed_at_ms": closed,
            "timeframe_minutes": tf,
            "feature_version": VERSION,
            "bars_digest": frame["bars_digest"],
            "atr": frame["current"]["atr"],
            "multiplier": p["trail_atr"],
            "high_water": encode(high),
            "low_water": encode(low),
            "trailing_stop": trail,
        }
